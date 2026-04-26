"""wizard.py — AI Generator Wizard for PeaceGrappler.

Autonomous Instagram Reels generator that uses Claude to research best
practices, make creative decisions (scenes, music, transitions, pacing),
and generate engagement-optimized videos.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

from db import (
    get_db, get_all_scenes, get_scene_grades, save_generated_video,
    update_video_caption,
)
from video import (
    ASSETS_DIR, AUDIO_EXTENSIONS, OUTPUT_DIR, TRANSITIONS, XFADE_DUR,
    add_text_overlay, concatenate_clips, detect_beats, extract_subclip,
    extract_wide_split, find_asset, get_video_duration, normalize_clip,
    overlay_music,
)

wizard_bp = Blueprint("wizard", __name__)

CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"

MODELS = [
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6 (most capable)"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (balanced)"},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5 (fastest)"},
]

RESEARCH_TTL_DAYS = 7

progress = queue.Queue()


def emit(msg):
    progress.put(msg)


# ── DB helpers ───────────────────────────────────────────────────────────────

def _get_cached_research():
    """Return cached research if fresh enough, else None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM wizard_research WHERE topic='instagram_reels' "
            "ORDER BY researched_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        researched = datetime.fromisoformat(row["researched_at"])
        if datetime.utcnow() - researched > timedelta(days=RESEARCH_TTL_DAYS):
            return None
        return json.loads(row["result_json"])
    except Exception:
        return None
    finally:
        conn.close()


def _save_research(result):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO wizard_research (topic, result_json) VALUES (?, ?)",
            ("instagram_reels", json.dumps(result)),
        )
        conn.commit()
    finally:
        conn.close()


def _save_feedback(video_id, feedback_text):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO wizard_feedback (generated_video_id, feedback) VALUES (?, ?)",
            (video_id, feedback_text),
        )
        conn.commit()
    finally:
        conn.close()


def _get_all_feedback():
    """Return ALL feedback entries, newest first."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT wf.feedback, wf.created_at, gv.path, gv.duration "
            "FROM wizard_feedback wf "
            "JOIN generated_videos gv ON gv.id = wf.generated_video_id "
            "ORDER BY wf.created_at DESC",
        ).fetchall()
        return [{"feedback": r["feedback"], "created_at": r["created_at"],
                 "video": Path(r["path"]).name, "duration": r["duration"]}
                for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _get_wizard_history(limit=20):
    """Return recent generated videos with any wizard feedback."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT gv.id, gv.path, gv.duration, gv.generated_at, "
            "gv.timeline_json, gv.caption "
            "FROM generated_videos gv ORDER BY gv.generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            fb_rows = conn.execute(
                "SELECT feedback, created_at FROM wizard_feedback "
                "WHERE generated_video_id=? ORDER BY created_at DESC",
                (r["id"],),
            ).fetchall()
            # Extract scene IDs and info from timeline
            scenes_used = []
            try:
                timeline = json.loads(r["timeline_json"])
                for item in timeline:
                    if item.get("type") == "clip" and item.get("id"):
                        sid = item["id"]
                        tags = conn.execute(
                            "SELECT tag FROM scene_tags WHERE scene_id=? "
                            "ORDER BY tag LIMIT 3", (sid,),
                        ).fetchall()
                        scene_row = conn.execute(
                            "SELECT excluded FROM scenes WHERE id=?", (sid,),
                        ).fetchone()
                        is_excluded = bool(scene_row["excluded"]) if scene_row else False
                        grade_row = conn.execute(
                            "SELECT SUM(score) as total, COUNT(*) as cnt "
                            "FROM grades WHERE scene_id=?", (sid,),
                        ).fetchone()
                        if is_excluded:
                            vote_status = "down"
                        elif grade_row and grade_row["cnt"] and grade_row["cnt"] > 0:
                            avg = grade_row["total"] / grade_row["cnt"]
                            vote_status = "up" if avg >= 3 else "down"
                        else:
                            vote_status = "unrated"
                        scenes_used.append({
                            "scene_id": sid,
                            "tags": [t["tag"] for t in tags],
                            "excluded": is_excluded,
                            "status": vote_status,
                        })
            except Exception:
                pass

            result.append({
                "id": r["id"],
                "path": r["path"],
                "filename": Path(r["path"]).name,
                "duration": r["duration"],
                "generated_at": r["generated_at"],
                "exists": Path(r["path"]).exists(),
                "caption": r["caption"] or "",
                "feedback": [{"text": f["feedback"], "at": f["created_at"]}
                             for f in fb_rows],
                "scenes": scenes_used,
            })
        return result
    except Exception:
        return []
    finally:
        conn.close()


# ── Claude CLI helpers ───────────────────────────────────────────────────────

def _call_claude(prompt_text, model, timeout=300):
    """Call Claude CLI and return the text response."""
    message = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": prompt_text},
    })

    result = subprocess.run(
        [CLAUDE_BIN, "--print",
         "--input-format", "stream-json",
         "--output-format", "stream-json",
         "--verbose",
         "--model", model],
        input=message, capture_output=True, text=True, timeout=timeout,
    )

    raw = result.stdout.strip()
    text_content = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("type") == "assistant":
                c = msg.get("message", {}).get("content", "")
                if isinstance(c, list):
                    t = " ".join(
                        b.get("text", "") for b in c
                        if b.get("type") == "text" and b.get("text", "").strip()
                    )
                    if t.strip():
                        text_content = t
                elif isinstance(c, str) and c.strip():
                    text_content = c
        except json.JSONDecodeError:
            continue

    return text_content


def _parse_json(raw):
    """Extract JSON object from Claude's text response."""
    raw = re.sub(r"^\s*```[a-z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return json.loads(raw)


# ── Music helpers ────────────────────────────────────────────────────────────

def _find_music_files():
    music = []
    music_dir = ASSETS_DIR / "music"
    dirs = [music_dir, ASSETS_DIR] if music_dir.exists() else [ASSETS_DIR]
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in AUDIO_EXTENSIONS:
                music.append({"name": f.stem, "path": str(f)})
    return music


# ── Research phase ───────────────────────────────────────────────────────────

RESEARCH_PROMPT = """\
You are an expert social media strategist specializing in Instagram Reels \
for combat sports and MMA content.

Based on your knowledge of the current Instagram Reels algorithm and best \
practices (2025-2026), provide detailed, actionable recommendations for \
creating MMA highlight reels that MAXIMIZE engagement (views, likes, \
shares, saves, and follows).

Consider: optimal video duration, pacing, hook strategy (first 1-3s), \
content structure, music usage, transition style, and what makes MMA \
content go viral on Reels.

Return a JSON object with EXACTLY this structure:
{
  "ideal_duration_range": {"min": <seconds>, "max": <seconds>},
  "optimal_duration": <seconds>,
  "aspect_ratio": "9:16",
  "hook_strategy": "<detailed strategy for first 1-3 seconds>",
  "pacing_cuts_per_minute": <number>,
  "content_structure": ["<phase1>", "<phase2>", ...],
  "music_strategy": "<how to use music for maximum engagement>",
  "transition_strategy": "<recommended transition approach for MMA content>",
  "engagement_tips": ["<tip1>", "<tip2>", ...],
  "avoid": ["<thing to avoid 1>", ...],
  "opening_types": ["<best hook types for MMA>", ...],
  "closing_strategy": "<how to end for max engagement>"
}

Return ONLY the JSON object. No explanation, no markdown fences.
"""


def _run_research(model):
    """Run the research phase — returns cached or fresh research."""
    cached = _get_cached_research()
    if cached:
        emit("Using cached Instagram Reels research (less than 7 days old)")
        return cached

    emit("Researching Instagram Reels best practices with Claude...")
    try:
        raw = _call_claude(RESEARCH_PROMPT, model, timeout=120)
        if not raw:
            emit("Research returned empty — using defaults")
            return _default_research()
        result = _parse_json(raw)
        _save_research(result)
        emit("Research complete — cached for future runs")
        return result
    except Exception as e:
        emit(f"Research failed ({e}) — using defaults")
        return _default_research()


def _default_research():
    """Sensible defaults if research fails."""
    return {
        "ideal_duration_range": {"min": 15, "max": 30},
        "optimal_duration": 22,
        "aspect_ratio": "9:16",
        "hook_strategy": "Open with the most explosive moment — a knockout, "
                         "a dramatic takedown, or a crowd reaction",
        "pacing_cuts_per_minute": 20,
        "content_structure": ["hook (0-2s)", "build (2-10s)",
                              "climax (10-20s)", "strong ending (last 2-3s)"],
        "music_strategy": "Use high-energy music, sync major hits to beat drops",
        "transition_strategy": "Fast cuts with minimal transitions, "
                               "occasional fade for dramatic effect",
        "engagement_tips": [
            "First frame must be visually striking",
            "Keep total duration under 30 seconds",
            "End with something unexpected or satisfying",
        ],
        "avoid": ["Long static shots", "Slow transitions",
                  "Starting with low-energy content"],
        "opening_types": ["Knockout moment", "Near-submission",
                          "Explosive takedown", "Crowd eruption"],
        "closing_strategy": "End on a decisive moment or celebration",
    }


# ── Creative planning phase ─────────────────────────────────────────────────

CREATIVE_PROMPT = """\
You are an expert video editor creating an Instagram Reel for a combat \
sports / MMA channel called PeaceGrappler. Your ONLY goal: MAXIMIZE \
ENGAGEMENT (views, likes, shares, saves).

## Instagram Reels Research
{research}

## Available Scenes
{scenes}

## Available Music
{music}

## Available Transitions
{transitions}

## Music Beat Analysis
{beat_info}

## User Feedback History (CRITICAL — read every entry)
{feedback}

{variation_info}

## Instructions
Create a video plan optimized for maximum Instagram Reel engagement.

FEEDBACK IS YOUR MOST IMPORTANT INPUT. The user's feedback above represents \
hard-learned lessons from previous generations. You MUST:
- Identify recurring themes in the feedback (e.g. "too long", "bad transitions")
- Treat repeated feedback as hard constraints — never repeat a criticized mistake
- Amplify what the user praised — if they liked something, do more of it
- Recent feedback takes priority over older feedback if they conflict
- In your "rationale" field, explicitly mention which feedback items shaped your decisions

KEY PRINCIPLES:
1. HOOK — First 1-2 seconds must grab attention (most explosive/dramatic moment)
2. PACING — Tight cuts, no dead time. Target ~{cuts_per_min} cuts per minute
3. ARC — Even a 20-second video needs rising action
4. MUSIC — Choose music that amplifies energy. SYNC cuts to beat positions when possible.
5. ENDING — Strong close that makes viewers replay or share
6. DURATION — Target {target_dur}s (within {dur_min}-{dur_max}s range)
7. BEATS — If beat positions are provided, align clip start/end times to land on or \
near beat positions. Viewers subconsciously feel beat-synced cuts as more professional.

For each clip, specify a sub-range within the scene. Keep clips tight (1.5-5s each).
Prefer scenes with high-energy tags (striking, takedown, submission, knockout, etc.)

Output a JSON object with EXACTLY this structure:
{{
  "target_duration": <seconds>,
  "rationale": "<brief creative strategy explanation>",
  "music": {{"name": "<music name from list, or null>", "volume": <1-5>}},
  "clips": [
    {{
      "scene_id": <id>,
      "start": <start seconds>,
      "end": <end seconds>,
      "wide_split": <true if this WIDE scene should use split-screen>,
      "text_overlay": "<optional text to overlay on this clip, or null>",
      "reason": "<why this clip, why this position>"
    }}
  ],
  "transitions": ["<transition name>", ...]
}}

RULES:
- "transitions" array must have exactly len(clips) - 1 elements
- clip start/end must be within the scene's time range
- each clip duration should be 1.5-5 seconds
- total clip duration should approximate target_duration
- only use scene IDs from the list above
- only use music names from the list above (or null)
- only use transition names from the list above
- For WIDE scenes: set "wide_split": true to display as split-screen \
(top + bottom halves, filling the full 9:16 frame with no black bars)
- "text_overlay": only include if text overlays are enabled (see below). \
Use short punchy text (max 6 words) for impact moments, fighter names, \
or engagement hooks. null if no text needed for this clip.
{text_overlay_instruction}
- Return ONLY the JSON object
"""


def _plan_video(model, research, scenes, music_files, feedback,
                used_scene_ids=None, variation_ctx=None,
                enable_text_overlays=False):
    """Ask Claude to plan a single video.

    variation_ctx: optional dict {"num": 2, "total": 3,
                                  "prev_rationales": ["...", "..."]}
    """
    if used_scene_ids is None:
        used_scene_ids = set()

    # Format scenes compactly
    grades = get_scene_grades()
    scene_lines = []
    for s in scenes:
        if s["id"] in used_scene_ids:
            continue
        dur = round(s["end_time"] - s["start_time"], 1)
        if dur < 1.5:
            continue
        grade_info = ""
        if s["id"] in grades:
            g = grades[s["id"]]
            avg = round(g["total_score"] / g["times_graded"], 1)
            grade_info = f" grade:{avg}/5"
        tags_str = ",".join(s["tags"][:8])
        wide = " WIDE" if s["wide"] else ""
        scene_lines.append(
            f"#{s['id']}: {s['video_filename']} "
            f"[{s['start_time']:.1f}-{s['end_time']:.1f}] "
            f"{dur}s tags:{tags_str}{wide}{grade_info}"
        )

    if not scene_lines:
        return None

    # Format music
    music_names = [m["name"] for m in music_files]
    music_str = ", ".join(music_names) if music_names else "No music available"

    # Format transitions
    trans_str = ", ".join(TRANSITIONS)

    # Format feedback — include ALL entries so Claude can learn patterns
    if feedback:
        fb_lines = []
        for i, fb in enumerate(feedback):
            recency = "most recent" if i == 0 else (
                "recent" if i < 5 else "older")
            fb_lines.append(
                f'- [{recency}] "{fb["feedback"]}" '
                f'(video: {fb["video"]}, {fb["duration"]}s, '
                f'{fb["created_at"]})')
        feedback_str = (
            f"{len(feedback)} feedback entries from the user:\n"
            + "\n".join(fb_lines)
        )
    else:
        feedback_str = "No feedback yet — this is the first generation."

    # Beat detection for music
    beat_str = "No music selected — no beat data."
    music_lookup = {m["name"]: m["path"] for m in music_files}
    # Try to detect beats for all music (Claude will pick one)
    beat_cache = {}
    for m in music_files:
        bd = detect_beats(m["path"])
        if bd["beats"]:
            beat_cache[m["name"]] = bd
            # Show first 40 beats to keep prompt size reasonable
            beat_positions = ", ".join(f"{b:.2f}" for b in bd["beats"][:40])
            beat_str = (
                f"Music '{m['name']}': BPM={bd['bpm']}, "
                f"beats at: [{beat_positions}] "
                f"({len(bd['beats'])} total beats detected)"
            )
            break  # Just show first music's beats; Claude picks

    if len(music_files) > 1 and beat_cache:
        lines = []
        for name, bd in beat_cache.items():
            bp = ", ".join(f"{b:.2f}" for b in bd["beats"][:30])
            lines.append(f"  '{name}': BPM={bd['bpm']}, beats=[{bp}]")
        beat_str = "\n".join(lines)
    elif not beat_cache and music_files:
        beat_str = "Beat detection found no clear beats. Use your judgment for cut timing."

    # Variation context
    if variation_ctx and variation_ctx["total"] > 1:
        var_num = variation_ctx["num"]
        var_total = variation_ctx["total"]
        prev = variation_ctx.get("prev_rationales", [])
        variation_str = (
            f"## VARIATION MODE\n"
            f"This is variation {var_num} of {var_total}. "
            f"You MUST create a DIFFERENT creative approach than previous variations.\n"
        )
        if prev:
            variation_str += "Previous variation strategies (DO NOT repeat these):\n"
            for i, r in enumerate(prev):
                variation_str += f"  Variation {i+1}: {r}\n"
            variation_str += (
                "Use a DIFFERENT hook, different scene selection, different pacing, "
                "and/or different music. Be creative and distinct."
            )
    else:
        variation_str = ""

    # Research values
    dur_range = research.get("ideal_duration_range", {"min": 15, "max": 30})
    optimal = research.get("optimal_duration", 22)
    cuts = research.get("pacing_cuts_per_minute", 20)

    if enable_text_overlays:
        text_instruction = (
            "- TEXT OVERLAYS ARE ENABLED. Add \"text_overlay\" to clips where "
            "short punchy text would boost engagement (impact moments, fighter "
            "names, stats, hooks like 'WATCH THIS'). Max 6 words per overlay. "
            "Don't add text to every clip — only where it adds value."
        )
    else:
        text_instruction = (
            "- Text overlays are DISABLED. Set \"text_overlay\" to null for all clips."
        )

    prompt = CREATIVE_PROMPT.format(
        research=json.dumps(research, indent=2),
        scenes="\n".join(scene_lines),
        music=music_str,
        transitions=trans_str,
        beat_info=beat_str,
        feedback=feedback_str,
        variation_info=variation_str,
        text_overlay_instruction=text_instruction,
        cuts_per_min=cuts,
        target_dur=optimal,
        dur_min=dur_range.get("min", 15),
        dur_max=dur_range.get("max", 30),
    )

    label = "Claude is planning the video"
    if variation_ctx and variation_ctx["total"] > 1:
        label += f" (variation {variation_ctx['num']}/{variation_ctx['total']})"
    emit(f"{label}...")
    raw = _call_claude(prompt, model, timeout=300)
    if not raw:
        return None

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        emit(f"Failed to parse Claude's plan: {e}")
        return None

    # Validate plan
    plan = _validate_plan(plan, scenes, music_files)
    return plan


def _validate_plan(plan, scenes, music_files):
    """Validate and fix Claude's plan against actual data."""
    scene_map = {s["id"]: s for s in scenes}
    music_names = {m["name"] for m in music_files}

    # Validate music
    music = plan.get("music") or {}
    if music.get("name") and music["name"] not in music_names:
        music["name"] = None
    plan["music"] = music

    # Validate clips
    valid_clips = []
    for clip in plan.get("clips", []):
        sid = clip.get("scene_id")
        if sid not in scene_map:
            continue
        scene = scene_map[sid]
        # Clamp start/end to scene boundaries
        start = max(scene["start_time"], clip.get("start", scene["start_time"]))
        end = min(scene["end_time"], clip.get("end", scene["end_time"]))
        if end - start < 0.5:
            start = scene["start_time"]
            end = min(scene["end_time"], start + 3.0)
        if end - start >= 0.5:
            valid_clips.append({
                "scene_id": sid,
                "start": round(start, 2),
                "end": round(end, 2),
                "reason": clip.get("reason", ""),
            })
    plan["clips"] = valid_clips

    # Validate transitions
    needed = max(0, len(valid_clips) - 1)
    trans = plan.get("transitions", [])
    valid_trans = []
    trans_set = set(TRANSITIONS)
    for t in trans[:needed]:
        valid_trans.append(t if t in trans_set else "fade")
    while len(valid_trans) < needed:
        valid_trans.append("fade")
    plan["transitions"] = valid_trans

    return plan


# ── Video assembly ───────────────────────────────────────────────────────────

def _assemble_video(plan, music_files, video_num, total,
                    mute_source=False):
    """Assemble a video from Claude's plan. Returns output path or None."""
    clips = plan.get("clips", [])
    if len(clips) < 2:
        emit(f"Video {video_num}/{total}: not enough clips in plan")
        return None

    music_choice = plan.get("music") or {}
    music_name = music_choice.get("name")
    music_lookup = {m["name"]: m["path"] for m in music_files}
    music_path = music_lookup.get(music_name)

    intro_file = find_asset("intro")
    outro_file = find_asset("outro")

    today = datetime.now().strftime("%Y-%m-%d")
    date_dir = OUTPUT_DIR / today
    date_dir.mkdir(parents=True, exist_ok=True)

    # Determine output filename
    counter = 1
    if date_dir.exists():
        for f in date_dir.iterdir():
            if f.suffix == ".mp4":
                parts = f.stem.split("-")
                if len(parts) >= 3:
                    try:
                        c = int(parts[-1])
                        if c >= counter:
                            counter = c + 1
                    except ValueError:
                        pass

    total_dur = int(sum(c["end"] - c["start"] for c in clips))
    out_file = date_dir / f"wiz-{total_dur}-{counter}.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        clip_paths = []
        intro_count = 0

        # Intro
        if intro_file:
            emit(f"Video {video_num}/{total}: normalizing intro...")
            intro_norm = os.path.join(tmp, "intro_norm.mp4")
            if normalize_clip(str(intro_file), intro_norm):
                clip_paths.append(intro_norm)
                intro_count = 1

        # Extract each clip
        scene_map = {s["id"]: s for s in get_all_scenes(include_ignored=True)}
        for i, clip in enumerate(clips):
            scene = scene_map.get(clip["scene_id"])
            if not scene:
                continue
            clip_out = os.path.join(tmp, f"clip_{i:03d}.mp4")
            start = clip["start"]
            duration = round(clip["end"] - clip["start"], 2)
            src = Path(scene["video_path"]).name
            is_wide = scene.get("wide", False)
            use_split = clip.get("wide_split", False) and is_wide

            if use_split:
                emit(f"Video {video_num}/{total}: clip {i+1}/{len(clips)} "
                     f"[{start:.1f}s +{duration:.1f}s] from {src} (split-screen)")
                ok = extract_wide_split(scene["video_path"], start, duration, clip_out)
            else:
                emit(f"Video {video_num}/{total}: clip {i+1}/{len(clips)} "
                     f"[{start:.1f}s +{duration:.1f}s] from {src}")
                ok = extract_subclip(scene["video_path"], start, duration, clip_out)

            if ok:
                # Apply text overlay if specified
                overlay_text = clip.get("text_overlay")
                if overlay_text and overlay_text.strip():
                    text_out = os.path.join(tmp, f"clip_{i:03d}_txt.mp4")
                    if add_text_overlay(clip_out, overlay_text.strip(), text_out):
                        clip_out = text_out
                    else:
                        emit(f"  Text overlay failed, using clip without text")

                # Mute source audio if requested
                if mute_source:
                    muted_out = os.path.join(tmp, f"clip_{i:03d}_mute.mp4")
                    try:
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", clip_out,
                             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-map", "0:v", "-map", "1:a",
                             "-c:v", "copy", "-c:a", "aac", "-shortest",
                             muted_out],
                            capture_output=True, timeout=30,
                        )
                        if os.path.exists(muted_out):
                            clip_out = muted_out
                    except Exception:
                        pass

                clip_paths.append(clip_out)

        # Outro
        outro_added = False
        if outro_file:
            emit(f"Video {video_num}/{total}: normalizing outro...")
            outro_norm = os.path.join(tmp, "outro_norm.mp4")
            if normalize_clip(str(outro_file), outro_norm):
                clip_paths.append(outro_norm)
                outro_added = True

        if len(clip_paths) < 2:
            emit(f"Video {video_num}/{total}: not enough clips extracted")
            return None

        # Build transitions list
        plan_transitions = plan.get("transitions", [])
        n_paths = len(clip_paths)
        all_transitions = [None] * (n_paths - 1)
        if intro_count:
            all_transitions[0] = "fade"
        for j in range(len(plan_transitions)):
            idx = j + intro_count
            if idx < n_paths - 1:
                all_transitions[idx] = plan_transitions[j]
        if outro_added:
            all_transitions[-1] = "fade"

        # Concatenate
        emit(f"Video {video_num}/{total}: assembling {len(clip_paths)} segments...")
        assembled = os.path.join(tmp, "assembled.mp4")
        if not concatenate_clips(clip_paths, assembled, all_transitions):
            emit(f"Video {video_num}/{total}: assembly failed")
            return None

        # Music overlay
        if music_path:
            emit(f"Video {video_num}/{total}: adding music ({music_name})...")
            if not overlay_music(assembled, music_path, str(out_file)):
                shutil.copy2(assembled, str(out_file))
        else:
            shutil.copy2(assembled, str(out_file))

    # Build timeline JSON compatible with the builder's load format
    timeline = []
    if music_name:
        timeline.append({
            "type": "music",
            "name": music_name,
            "volume": music_choice.get("volume", 3),
        })
    for i, clip in enumerate(clips):
        if i > 0 and i - 1 < len(plan_transitions):
            timeline.append({
                "type": "transition",
                "name": plan_transitions[i - 1],
            })
        scene = scene_map.get(clip["scene_id"])
        timeline.append({
            "type": "clip",
            "id": clip["scene_id"],
            "video_file": scene["video_path"] if scene else "",
            "start": clip["start"],
            "end": clip["end"],
        })

    final_dur = get_video_duration(str(out_file))
    save_generated_video(str(out_file), round(final_dur, 1), timeline)

    return {
        "path": str(out_file),
        "filename": out_file.name,
        "duration": round(final_dur, 1),
        "rationale": plan.get("rationale", ""),
    }


# ── Caption generation ──────────────────────────────────────────────────────

CAPTION_PROMPT = """\
You are a social media expert for a combat sports / MMA Instagram channel \
called PeaceGrappler (@peacegrappler).

Generate an Instagram Reel caption + hashtags for a video with these details:
- Duration: {duration}s
- Creative strategy: {rationale}
- Tags/content: {tags}
- Music: {music}

Requirements:
- Caption should be 1-3 punchy lines that drive engagement (likes, comments, saves, shares)
- Include a hook or question to encourage comments
- Add 5-10 relevant hashtags (mix of broad MMA + niche + trending)
- Format: caption text first, then hashtags on a new line
- Keep it authentic to MMA/combat sports culture
- Do NOT use emojis excessively (max 2-3)

Return ONLY the caption text + hashtags, nothing else.
"""


def _generate_caption(model, plan, duration):
    """Generate an Instagram caption for a video."""
    tags_used = set()
    scene_map = {s["id"]: s for s in get_all_scenes(include_ignored=True)}
    for clip in plan.get("clips", []):
        scene = scene_map.get(clip["scene_id"])
        if scene:
            tags_used.update(scene.get("tags", []))

    music_name = (plan.get("music") or {}).get("name", "none")

    prompt = CAPTION_PROMPT.format(
        duration=duration,
        rationale=plan.get("rationale", "MMA highlight reel"),
        tags=", ".join(sorted(tags_used)),
        music=music_name,
    )

    try:
        raw = _call_claude(prompt, model, timeout=60)
        return raw.strip() if raw else ""
    except Exception:
        return ""


# ── Main generation flow ────────────────────────────────────────────────────

def _filter_scenes_by_folders(scenes, folders, video_dir):
    """Filter scenes to only those whose source video is in selected folders."""
    filtered = []
    for s in scenes:
        vpath = Path(s["video_path"])
        try:
            rel = str(vpath.parent.relative_to(video_dir))
        except ValueError:
            rel = "(root)"
        if rel == ".":
            rel = "(root)"
        if rel in folders:
            filtered.append(s)
    return filtered


def _auto_analyze_folders(model, folders):
    """Scan and analyze any unanalyzed videos in the selected folders."""
    from video import VIDEO_DIR, VIDEO_EXTENSIONS
    from db import register_video, get_all_videos, get_analyzed_tags
    from analyzer import analyze_full, ALL_TAG_SET

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    # Find video files in selected folders
    target_files = []
    for folder in folders:
        folder_path = VIDEO_DIR if folder == "(root)" else VIDEO_DIR / folder
        if not folder_path.exists():
            continue
        for f in sorted(folder_path.iterdir()):
            if f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith("."):
                target_files.append(f)

    if not target_files:
        return

    # Register all files
    for f in target_files:
        register_video(f)

    # Find unanalyzed ones
    all_videos = get_all_videos()
    pending = []
    for v in all_videos:
        vpath = Path(v["path"])
        if vpath not in target_files:
            continue
        analyzed_tags = get_analyzed_tags(v["id"])
        if not analyzed_tags or (ALL_TAG_SET - analyzed_tags):
            pending.append(v)

    if not pending:
        return

    emit(f"Auto-analyzing {len(pending)} unanalyzed video(s) in selected folders...")
    for i, v in enumerate(pending):
        emit(f"  Analyzing {i+1}/{len(pending)}: {v['filename']}...")
        if v["duration"] <= 0:
            emit(f"  Skipping {v['filename']} (no duration)")
            continue
        try:
            result = analyze_full(v["path"], v["duration"])
            if result:
                from db import save_analysis
                save_analysis(v["id"], result["tags"],
                              result["moments"], list(ALL_TAG_SET))
                emit(f"  Got {len(result['tags'])} tags")
            else:
                emit(f"  Analysis failed for {v['filename']}")
        except Exception as e:
            emit(f"  Error: {e}")
    emit("Auto-analysis complete")


def _generate(model, num_videos, num_variations=1, folders=None,
              mute_source=False, enable_text_overlays=False,
              music_folders=None):
    """Full wizard generation flow. Runs in a background thread."""
    try:
        # 0. Auto-analyze unanalyzed videos in selected folders
        if folders:
            _auto_analyze_folders(model, folders)

        # 1. Research
        emit("Phase 1: Instagram Reels research...")
        research = _run_research(model)
        dur_range = research.get("ideal_duration_range", {"min": 15, "max": 30})
        optimal = research.get("optimal_duration", 22)
        emit(f"Target: {optimal}s (range {dur_range.get('min')}-{dur_range.get('max')}s)")

        # 2. Load data
        emit("Loading scenes and music...")
        scenes = get_all_scenes()

        # Filter by folders if specified
        if folders:
            from video import VIDEO_DIR
            scenes = _filter_scenes_by_folders(scenes, folders, VIDEO_DIR)
            emit(f"Filtered to {len(scenes)} scenes from selected folders")

        if not scenes:
            emit("No analyzed scenes found for the selected folders.")
            emit("DONE:error")
            return

        music_files = _find_music_files()
        # Filter music by selected folders if specified
        if music_folders:
            music_dir = ASSETS_DIR / "music"
            filtered_music = []
            for m in music_files:
                mpath = Path(m["path"])
                try:
                    rel = str(mpath.parent.relative_to(music_dir))
                except ValueError:
                    rel = "(root)"
                if rel == ".":
                    rel = "(root)"
                if rel in music_folders:
                    filtered_music.append(m)
            music_files = filtered_music

        feedback = _get_all_feedback()

        emit(f"Found {len(scenes)} scenes, {len(music_files)} music tracks, "
             f"{len(feedback)} feedback entries")
        if mute_source:
            emit("Source audio will be muted (music only)")
        if enable_text_overlays:
            emit("Text overlays enabled")

        if num_variations > 1:
            emit(f"Generating {num_variations} A/B variations per video")

        # 3. Generate each video (with variations)
        used_scene_ids = set()
        results = []

        for vid_num in range(1, num_videos + 1):
            prev_rationales = []

            for var_num in range(1, num_variations + 1):
                var_label = (f"Video {vid_num}/{num_videos}"
                             if num_variations == 1
                             else f"Video {vid_num} variation {var_num}/{num_variations}")

                emit(f"\nPhase 2: Planning {var_label}...")

                variation_ctx = None
                if num_variations > 1:
                    variation_ctx = {
                        "num": var_num,
                        "total": num_variations,
                        "prev_rationales": prev_rationales[:],
                    }

                plan = _plan_video(model, research, scenes, music_files,
                                   feedback, used_scene_ids,
                                   variation_ctx=variation_ctx,
                                   enable_text_overlays=enable_text_overlays)
                if not plan or not plan.get("clips"):
                    emit(f"{var_label}: Claude couldn't create a plan")
                    continue

                n_clips = len(plan["clips"])
                target = plan.get("target_duration", optimal)
                music_name = (plan.get("music") or {}).get("name", "none")
                rationale = plan.get("rationale", "")
                prev_rationales.append(rationale)

                emit(f"Plan: {n_clips} clips, ~{target}s, music: {music_name}")
                if rationale:
                    emit(f"Strategy: {rationale}")

                # Track used scenes across variations
                for clip in plan["clips"]:
                    used_scene_ids.add(clip["scene_id"])

                emit(f"\nPhase 3: Assembling {var_label}...")
                result = _assemble_video(plan, music_files, vid_num, num_videos,
                                        mute_source=mute_source)
                if result:
                    # Generate caption
                    emit("Generating Instagram caption...")
                    caption = _generate_caption(model, plan, result["duration"])
                    if caption:
                        # Update the DB record with caption
                        from db import get_all_generated_videos
                        vids = get_all_generated_videos()
                        match = next(
                            (v for v in vids
                             if Path(v["path"]).name == result["filename"]),
                            None,
                        )
                        if match:
                            update_video_caption(match["id"], caption)
                        result["caption"] = caption
                        emit("Caption generated!")

                    results.append(result)
                    emit(f"VIDEO:{result['filename']}:{result['duration']}")
                    emit(f"{var_label} complete! {result['duration']}s -> "
                         f"{result['filename']}")

        if results:
            emit(f"\nAll done! Generated {len(results)} video(s)")
            emit("DONE:ok")
        else:
            emit("No videos were generated")
            emit("DONE:error")

    except Exception as e:
        emit(f"Error: {e}")
        emit("DONE:error")


# ── Auto-pipeline ───────────────────────────────────────────────────────────

pipeline_progress = queue.Queue()
_pipeline_stop = threading.Event()


def _pipeline_emit(msg):
    pipeline_progress.put(msg)


def _run_pipeline(model):
    """Full auto-pipeline: scan → analyze → generate."""
    from analyzer import (
        analyze_full, ALL_TAG_SET, emit_progress as analyzer_emit,
    )
    from db import register_video, get_all_videos, get_analyzed_tags
    from video import VIDEO_DIR, VIDEO_EXTENSIONS

    _pipeline_stop.clear()

    try:
        # Step 1: Scan for new videos
        _pipeline_emit("Step 1: Scanning for new videos...")
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        registered = 0
        for root, _, files in os.walk(VIDEO_DIR):
            if _pipeline_stop.is_set():
                _pipeline_emit("Pipeline stopped by user")
                _pipeline_emit("DONE:stopped")
                return
            for name in sorted(files):
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS and not name.startswith("."):
                    path = Path(root) / name
                    register_video(path)
                    registered += 1
        _pipeline_emit(f"Registered {registered} video files")

        # Step 2: Analyze unanalyzed videos
        videos = get_all_videos()
        pending = []
        for v in videos:
            analyzed_tags = get_analyzed_tags(v["id"])
            if not analyzed_tags or (ALL_TAG_SET - analyzed_tags):
                pending.append(v)

        if pending:
            _pipeline_emit(f"Step 2: Analyzing {len(pending)} videos...")
            for i, v in enumerate(pending):
                if _pipeline_stop.is_set():
                    _pipeline_emit("Pipeline stopped by user")
                    _pipeline_emit("DONE:stopped")
                    return

                _pipeline_emit(f"Analyzing {i+1}/{len(pending)}: {v['filename']}...")

                if v["duration"] <= 0:
                    _pipeline_emit(f"  Skipping {v['filename']} (no duration)")
                    continue

                analyzed_tags = get_analyzed_tags(v["id"])
                force = not bool(analyzed_tags)

                try:
                    if force:
                        result = analyze_full(v["path"], v["duration"])
                        if result:
                            from db import save_analysis
                            save_analysis(v["id"], result["tags"],
                                          result["moments"], list(ALL_TAG_SET))
                            _pipeline_emit(f"  Got {len(result['tags'])} tags")
                        else:
                            _pipeline_emit(f"  Analysis failed for {v['filename']}")
                    else:
                        from analyzer import analyze_incremental
                        new_tags = ALL_TAG_SET - analyzed_tags
                        new_results = analyze_incremental(v["path"], v["duration"],
                                                          new_tags)
                        from db import save_analysis
                        save_analysis(v["id"], new_results, [], list(new_tags))
                        _pipeline_emit(f"  Got {len(new_results)} new tags")
                except Exception as e:
                    _pipeline_emit(f"  Error analyzing {v['filename']}: {e}")
        else:
            _pipeline_emit("Step 2: All videos already analyzed")

        # Step 3: Generate video with wizard
        if _pipeline_stop.is_set():
            _pipeline_emit("Pipeline stopped by user")
            _pipeline_emit("DONE:stopped")
            return

        _pipeline_emit("Step 3: Generating video with AI Wizard...")
        scenes = get_all_scenes()
        if not scenes:
            _pipeline_emit("No scenes available after analysis")
            _pipeline_emit("DONE:error")
            return

        research = _run_research(model)
        music_files = _find_music_files()
        feedback = _get_all_feedback()

        plan = _plan_video(model, research, scenes, music_files, feedback)
        if not plan or not plan.get("clips"):
            _pipeline_emit("Claude couldn't create a plan")
            _pipeline_emit("DONE:error")
            return

        rationale = plan.get("rationale", "")
        if rationale:
            _pipeline_emit(f"Strategy: {rationale}")

        result = _assemble_video(plan, music_files, 1, 1)
        if result:
            # Generate caption
            caption = _generate_caption(model, plan, result["duration"])
            if caption:
                from db import get_all_generated_videos
                vids = get_all_generated_videos()
                match = next(
                    (v for v in vids
                     if Path(v["path"]).name == result["filename"]),
                    None,
                )
                if match:
                    update_video_caption(match["id"], caption)

            _pipeline_emit(f"Video complete: {result['filename']} ({result['duration']}s)")
            _pipeline_emit("DONE:ok")
        else:
            _pipeline_emit("Video assembly failed")
            _pipeline_emit("DONE:error")

    except Exception as e:
        _pipeline_emit(f"Pipeline error: {e}")
        _pipeline_emit("DONE:error")


# ── Routes ───────────────────────────────────────────────────────────────────

@wizard_bp.route("/wizard")
def wizard_page():
    return WIZARD_HTML


@wizard_bp.route("/wizard/api/models")
def api_models():
    return jsonify(MODELS)


@wizard_bp.route("/wizard/api/folders")
def api_folders():
    """List all leaf folders under videos/."""
    from video import VIDEO_DIR
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    folders = set()
    for root, dirs, files in os.walk(VIDEO_DIR):
        # Only include folders that contain video files
        from video import VIDEO_EXTENSIONS
        has_videos = any(
            Path(f).suffix.lower() in VIDEO_EXTENSIONS
            for f in files if not f.startswith(".")
        )
        if has_videos:
            rel = os.path.relpath(root, VIDEO_DIR)
            folders.add(rel if rel != "." else "(root)")
    return jsonify(sorted(folders))


@wizard_bp.route("/wizard/api/generate", methods=["POST"])
def api_generate():
    data = request.json or {}
    model = data.get("model", "claude-opus-4-6")
    num_videos = max(1, min(10, data.get("num_videos", 1)))
    num_variations = max(1, min(3, data.get("variations", 1)))
    folders = data.get("folders", [])
    mute_source = data.get("mute_source", False)
    enable_text_overlays = data.get("text_overlays", False)
    music_folders = data.get("music_folders", [])

    valid_ids = {m["id"] for m in MODELS}
    if model not in valid_ids:
        model = "claude-opus-4-6"

    threading.Thread(target=_generate,
                     args=(model, num_videos, num_variations, folders,
                           mute_source, enable_text_overlays, music_folders),
                     daemon=True).start()
    return jsonify({"status": "started"})


@wizard_bp.route("/wizard/api/music-folders")
def api_music_folders():
    """List all folders under assets/music/ that contain audio files."""
    music_dir = ASSETS_DIR / "music"
    if not music_dir.exists():
        return jsonify(["(root)"])
    folders = set()
    for root, dirs, files in os.walk(music_dir):
        has_audio = any(
            Path(f).suffix.lower() in AUDIO_EXTENSIONS
            for f in files if not f.startswith(".")
        )
        if has_audio:
            rel = os.path.relpath(root, music_dir)
            folders.add(rel if rel != "." else "(root)")
    return jsonify(sorted(folders) if folders else ["(root)"])


@wizard_bp.route("/wizard/api/caption/<int:video_id>", methods=["POST"])
def api_regenerate_caption(video_id):
    """Regenerate caption for an existing video."""
    data = request.json or {}
    model = data.get("model", "claude-sonnet-4-6")

    from db import get_all_generated_videos
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    # Build a minimal plan from timeline
    timeline = video.get("timeline", [])
    plan = {"clips": [], "music": {}, "rationale": "MMA highlight reel"}
    for item in timeline:
        if item.get("type") == "clip":
            plan["clips"].append(item)
        elif item.get("type") == "music":
            plan["music"] = {"name": item.get("name")}

    caption = _generate_caption(model, plan, video["duration"])
    if caption:
        update_video_caption(video_id, caption)
        return jsonify({"status": "ok", "caption": caption})
    return jsonify({"error": "Caption generation failed"}), 500


@wizard_bp.route("/wizard/api/status")
def api_status():
    def generate():
        while True:
            try:
                msg = progress.get(timeout=30)
                yield f"data: {json.dumps({'message': msg})}\n\n"
                if msg.startswith("DONE:"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'message': 'waiting...'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@wizard_bp.route("/wizard/api/feedback", methods=["POST"])
def api_feedback():
    data = request.json or {}
    video_id = data.get("video_id")
    feedback_text = (data.get("feedback") or "").strip()
    if not video_id or not feedback_text:
        return jsonify({"error": "video_id and feedback required"}), 400
    _save_feedback(video_id, feedback_text)
    return jsonify({"status": "saved"})


@wizard_bp.route("/wizard/api/history")
def api_history():
    return jsonify(_get_wizard_history())


@wizard_bp.route("/wizard/api/video/<int:video_id>")
def api_video(video_id):
    from db import get_all_generated_videos
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return "", 404
    path = Path(video["path"])
    if not path.exists():
        return "", 404
    return send_file(str(path), mimetype="video/mp4")


@wizard_bp.route("/wizard/api/research")
def api_get_research():
    """Return the current cached research, or null."""
    cached = _get_cached_research()
    return jsonify({"research": cached})


@wizard_bp.route("/wizard/api/research", methods=["PUT"])
def api_save_research():
    """Save user-edited research."""
    data = request.json or {}
    research = data.get("research")
    if not research or not isinstance(research, dict):
        return jsonify({"error": "Invalid research data"}), 400
    # Replace cache with user edit
    conn = get_db()
    try:
        conn.execute("DELETE FROM wizard_research")
        conn.commit()
    finally:
        conn.close()
    _save_research(research)
    return jsonify({"status": "saved"})


@wizard_bp.route("/wizard/api/refresh-research", methods=["POST"])
def api_refresh_research():
    """Force refresh the cached research."""
    data = request.json or {}
    model = data.get("model", "claude-sonnet-4-6")
    # Delete old cache
    conn = get_db()
    try:
        conn.execute("DELETE FROM wizard_research")
        conn.commit()
    finally:
        conn.close()
    # Run fresh
    try:
        raw = _call_claude(RESEARCH_PROMPT, model, timeout=120)
        if raw:
            result = _parse_json(raw)
            _save_research(result)
            return jsonify({"status": "ok", "research": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "Empty response"}), 500


@wizard_bp.route("/wizard/api/email/<int:video_id>", methods=["POST"])
def api_email(video_id):
    """Open the system mail client with the video attached."""
    from db import get_all_generated_videos
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return jsonify({"error": "Video not found"}), 404
    path = Path(video["path"])
    if not path.exists():
        return jsonify({"error": "Video file not found"}), 404

    data = request.json or {}
    to = data.get("to", "")
    subject = data.get("subject", f"PeaceGrappler Video - {path.name}")
    body = data.get("body", "")

    # macOS: use AppleScript to open Mail with attachment
    import platform
    if platform.system() == "Darwin":
        script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:true}}
            tell newMsg
                if "{to}" is not "" then
                    make new to recipient at end of to recipients with properties {{address:"{to}"}}
                end if
                make new attachment with properties {{file name:POSIX file "{path}"}} at after the last paragraph
            end tell
            activate
        end tell
        '''
        try:
            subprocess.Popen(["osascript", "-e", script])
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Email with attachment is only supported on macOS"}), 400


@wizard_bp.route("/wizard/api/exclude", methods=["POST"])
def api_exclude():
    """Toggle the excluded flag on a scene."""
    from db import set_scene_excluded
    data = request.json or {}
    scene_id = data.get("scene_id")
    exclude = data.get("exclude", True)
    if not scene_id:
        return jsonify({"error": "scene_id required"}), 400
    set_scene_excluded(scene_id, exclude)
    return jsonify({"status": "ok"})


@wizard_bp.route("/wizard/api/excluded-scenes")
def api_excluded_scenes():
    """Return all excluded scenes with tags and video info."""
    from db import get_scene_by_id
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT s.id, s.video_id, s.start_time, s.end_time,
                   v.filename, v.path
            FROM scenes s
            JOIN videos v ON v.id = s.video_id
            WHERE s.excluded = 1
            ORDER BY v.filename, s.start_time
        """).fetchall()
        result = []
        for r in rows:
            tags = conn.execute(
                "SELECT tag FROM scene_tags WHERE scene_id=? ORDER BY tag",
                (r["id"],),
            ).fetchall()
            dur = round(r["end_time"] - r["start_time"], 1)
            result.append({
                "id": r["id"],
                "filename": r["filename"],
                "start": r["start_time"],
                "end": r["end_time"],
                "duration": dur,
                "tags": [t["tag"] for t in tags],
            })
        return jsonify(result)
    finally:
        conn.close()


@wizard_bp.route("/wizard/api/pipeline/start", methods=["POST"])
def api_pipeline_start():
    data = request.json or {}
    model = data.get("model", "claude-sonnet-4-6")
    threading.Thread(target=_run_pipeline, args=(model,), daemon=True).start()
    return jsonify({"status": "started"})


@wizard_bp.route("/wizard/api/pipeline/stop", methods=["POST"])
def api_pipeline_stop():
    _pipeline_stop.set()
    return jsonify({"status": "stopping"})


@wizard_bp.route("/wizard/api/pipeline/status")
def api_pipeline_status():
    def generate():
        while True:
            try:
                msg = pipeline_progress.get(timeout=30)
                yield f"data: {json.dumps({'message': msg})}\n\n"
                if msg.startswith("DONE:"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'message': 'waiting...'})}\n\n"
    return Response(generate(), mimetype="text/event-stream")


# ── HTML ─────────────────────────────────────────────────────────────────────

WIZARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - AI Wizard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:flex;flex-direction:column;min-height:100vh;
}

/* -- Header (consistent) -- */
header{
  background:#141414;border-bottom:1px solid #2a2a2a;
  padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;
}
header h1{font-size:18px;font-weight:600;color:#fff;white-space:nowrap}
header h1 span{color:#e53935}
nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
  border:1px solid #444;border-radius:6px}
nav a:hover{color:#fff;border-color:#888}
nav a.active{color:#e53935;border-color:#e53935}

/* -- Content -- */
.content{flex:1;padding:24px;max-width:900px;margin:0 auto;width:100%}
h2{font-size:22px;color:#fff;margin-bottom:4px}
.subtitle{font-size:13px;color:#888;margin-bottom:24px}

/* -- Config panel -- */
.config{
  background:#141414;border:1px solid #2a2a2a;border-radius:10px;
  padding:20px;margin-bottom:20px;
}
.config-row{display:flex;gap:20px;align-items:flex-end;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12px;color:#888;font-weight:600;text-transform:uppercase}
select,input[type="number"]{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 12px;font-size:14px;
}
select:focus,input:focus{outline:none;border-color:#e53935}
select{min-width:260px}
input[type="number"]{width:80px}

/* -- Folder picker -- */
.folder-picker{position:relative}
.folder-btn{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 12px;font-size:13px;cursor:pointer;min-width:180px;
  text-align:left;display:flex;align-items:center;justify-content:space-between;
}
.folder-btn:hover{border-color:#666}
.folder-btn .arrow{font-size:10px;color:#888}
.folder-dropdown{
  display:none;position:absolute;top:100%;left:0;right:0;
  background:#222;border:1px solid #444;border-radius:6px;
  margin-top:4px;max-height:250px;overflow-y:auto;z-index:20;
  padding:4px 0;min-width:220px;
}
.folder-dropdown.open{display:block}
.folder-item{
  display:flex;align-items:center;gap:8px;padding:6px 12px;
  cursor:pointer;font-size:12px;color:#ccc;
}
.folder-item:hover{background:#333}
.folder-item input{accent-color:#e53935}
.folder-item .fi-name{flex:1}

.btn-row{display:flex;gap:10px;align-items:flex-end}
button{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 16px;font-size:13px;cursor:pointer;
}
button:hover{border-color:#666}
button:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{
  background:#e53935;color:#fff;border-color:#e53935;font-weight:600;
  padding:10px 24px;font-size:14px;
}
.btn-primary:hover{background:#c62828}
.btn-secondary{font-size:12px;padding:8px 14px}

/* -- Research panel -- */
.research-panel{
  background:#141414;border:1px solid #2a2a2a;border-radius:10px;
  margin-bottom:20px;overflow:hidden;
}
.research-toggle{
  display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;cursor:pointer;user-select:none;
}
.research-toggle:hover{background:#1a1a1a}
.research-toggle h3{font-size:14px;color:#fff;font-weight:600}
.research-toggle .arrow{
  color:#888;font-size:12px;transition:transform .2s;
}
.research-toggle .arrow.open{transform:rotate(90deg)}
.research-toggle .research-status{
  font-size:11px;color:#666;margin-left:12px;font-weight:400;
}
.research-body{display:none;padding:0 20px 16px;border-top:1px solid #2a2a2a}
.research-body.open{display:block}
.research-fields{display:flex;flex-direction:column;gap:12px;margin-top:14px}
.research-field label{
  display:block;font-size:11px;color:#e53935;font-weight:600;
  text-transform:uppercase;margin-bottom:4px;
}
.research-field input,
.research-field textarea{
  width:100%;background:#222;color:#e0e0e0;border:1px solid #444;
  border-radius:6px;padding:8px 10px;font-size:13px;font-family:inherit;
}
.research-field input:focus,
.research-field textarea:focus{outline:none;border-color:#e53935}
.research-field textarea{
  resize:vertical;
}
.research-field .hint{font-size:10px;color:#555;margin-top:2px}
.research-actions{
  display:flex;gap:10px;margin-top:14px;align-items:center;
}
.research-actions .save-status{font-size:12px;color:#4caf50}
.research-empty{color:#555;font-size:13px;padding:14px 0}

/* -- Progress -- */
.progress{
  margin-top:20px;padding:16px;background:#1a1a1a;border:1px solid #2a2a2a;
  border-radius:10px;font-size:13px;max-height:350px;overflow-y:auto;display:none;
}
.progress.active{display:block}
.progress .line{padding:2px 0;color:#aaa}
.progress .line.phase{color:#e53935;font-weight:600;margin-top:6px}
.progress .line.strategy{color:#818cf8;font-style:italic}
.progress .line.video-ready{color:#4caf50;font-weight:600}
.progress .line.error{color:#ef5350}
.progress .line.done{color:#4caf50;font-weight:700;font-size:14px;margin-top:8px}

/* -- Caption -- */
.caption-box{
  margin-top:12px;background:#1a1a1a;border:1px solid #2a2a2a;
  border-radius:8px;padding:12px;position:relative;
}
.caption-box .cap-label{font-size:11px;color:#e53935;font-weight:600;text-transform:uppercase;margin-bottom:6px}
.caption-box .cap-text{font-size:13px;color:#ccc;white-space:pre-wrap;line-height:1.5}
.caption-box .cap-actions{display:flex;gap:8px;margin-top:8px}
.cap-btn{
  background:#222;color:#aaa;border:1px solid #444;border-radius:4px;
  padding:3px 10px;font-size:11px;cursor:pointer;
}
.cap-btn:hover{color:#fff;border-color:#666}
.cap-btn.copied{color:#4caf50;border-color:#4caf50}

/* -- Scenes used -- */
.scenes-used{margin-top:12px}
.scenes-used .su-label{font-size:11px;color:#888;font-weight:600;text-transform:uppercase;margin-bottom:6px}
.scene-chips{display:flex;gap:8px;flex-wrap:wrap}
.scene-chip{
  display:flex;align-items:center;gap:8px;
  background:#222;border:1px solid #333;border-radius:8px;
  padding:4px 8px 4px 4px;font-size:11px;color:#aaa;
  transition:opacity .2s;
}
.scene-chip img{
  width:36px;height:64px;object-fit:cover;border-radius:4px;background:#111;
}
.scene-chip .sc-info{display:flex;flex-direction:column;gap:1px}
.scene-chip .sc-name{color:#ccc;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.scene-chip .sc-tags{color:#666;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.scene-chip .sc-votes{display:flex;gap:4px;margin-left:auto}
.scene-chip .sv{
  width:22px;height:22px;border-radius:50%;border:1.5px solid #444;
  background:none;cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .12s;padding:0;
}
.scene-chip .sv svg{width:12px;height:12px;fill:#555}
.scene-chip .sv:hover{transform:scale(1.15)}
.scene-chip .sv.sv-down:hover{background:#ef5350;border-color:#ef5350}
.scene-chip .sv.sv-down:hover svg{fill:#fff}
.scene-chip .sv.sv-down.active{background:#ef5350;border-color:#ef5350}
.scene-chip .sv.sv-down.active svg{fill:#fff}
.scene-chip .sv.sv-up:hover{background:#4caf50;border-color:#4caf50}
.scene-chip .sv.sv-up:hover svg{fill:#fff}
.scene-chip .sv.sv-up.active{background:#4caf50;border-color:#4caf50}
.scene-chip .sv.sv-up.active svg{fill:#fff}
.scene-chip.excluded{opacity:.35}

/* -- Excluded scenes panel -- */
.excluded-panel{
  background:#141414;border:1px solid #2a2a2a;border-radius:10px;
  margin-bottom:20px;overflow:hidden;
}
.excluded-toggle{
  display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;cursor:pointer;user-select:none;
}
.excluded-toggle:hover{background:#1a1a1a}
.excluded-toggle h3{font-size:14px;color:#fff;font-weight:600}
.excluded-toggle .exc-count{font-size:11px;color:#e53935;margin-left:8px;font-weight:400}
.excluded-toggle .arrow{color:#888;font-size:12px;transition:transform .2s}
.excluded-toggle .arrow.open{transform:rotate(90deg)}
.excluded-body{display:none;padding:0 20px 16px;border-top:1px solid #2a2a2a}
.excluded-body.open{display:block}
.excluded-grid{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.excluded-empty{color:#555;font-size:13px;padding:14px 0}

/* -- Results -- */
.results{margin-top:24px;display:none}
.results.active{display:block}
.results h3{font-size:16px;color:#fff;margin-bottom:12px}
.result-card{
  background:#141414;border:1px solid #2a2a2a;border-radius:10px;
  padding:20px;margin-bottom:16px;
}
.result-card .result-header{
  display:flex;align-items:center;gap:12px;margin-bottom:12px;
}
.result-card .result-header .filename{font-weight:600;color:#fff}
.result-card .result-header .dur{color:#888;font-size:13px}
.result-card .result-header .rationale{
  font-size:12px;color:#818cf8;font-style:italic;margin-left:auto;
}
.result-card video{
  width:100%;max-height:500px;border-radius:8px;background:#000;
}
.feedback-form{margin-top:12px}
.feedback-form textarea{
  width:100%;background:#222;color:#e0e0e0;border:1px solid #444;
  border-radius:8px;padding:10px;font-size:13px;font-family:inherit;
  resize:vertical;min-height:60px;
}
.feedback-form textarea:focus{outline:none;border-color:#e53935}
.feedback-form .fb-row{
  display:flex;align-items:center;gap:10px;margin-top:8px;
}
.feedback-form .fb-status{font-size:12px;color:#4caf50}

/* -- History -- */
.history{margin-top:32px}
.history h3{font-size:16px;color:#fff;margin-bottom:12px}
.hist-card{
  background:#141414;border:1px solid #2a2a2a;border-radius:8px;
  margin-bottom:10px;overflow:hidden;
}
.hist-header{
  display:flex;align-items:center;gap:14px;padding:14px 16px;
  cursor:pointer;user-select:none;
}
.hist-header:hover{background:#1a1a1a}
.hist-card .hist-info{flex:1}
.hist-card .hist-name{font-size:13px;font-weight:600;color:#e0e0e0}
.hist-card .hist-meta{font-size:11px;color:#666;margin-top:2px}
.hist-card .hist-fb{
  font-size:12px;color:#818cf8;margin-top:4px;font-style:italic;
}
.hist-card .hist-no-fb{font-size:11px;color:#555;margin-top:4px}
.hist-expand-arrow{
  color:#666;font-size:11px;flex-shrink:0;transition:transform .2s;
}
.hist-expand-arrow.open{transform:rotate(90deg)}
.hist-body{display:none;padding:0 16px 16px;border-top:1px solid #2a2a2a}
.hist-body.open{display:block}
.hist-body video{
  width:100%;max-height:500px;border-radius:8px;background:#000;margin-top:12px;
}
.hist-body .feedback-form{margin-top:12px}
.hist-actions{
  display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap;
}
.btn-email{
  display:inline-flex;align-items:center;gap:6px;
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 14px;font-size:12px;cursor:pointer;
}
.btn-email:hover{border-color:#666;color:#fff}
.btn-email svg{width:14px;height:14px;fill:currentColor}

/* -- Pipeline -- */
.pipeline-section{
  background:#141414;border:1px solid #2a2a2a;border-radius:10px;
  padding:16px 20px;margin-bottom:20px;
}
.pipeline-section h3{font-size:14px;color:#fff;margin-bottom:8px}
.pipeline-section p{font-size:12px;color:#888;margin-bottom:12px}
.pipeline-btns{display:flex;gap:10px;align-items:center}
.pipeline-progress{
  margin-top:12px;padding:12px;background:#0d0d0d;border-radius:8px;
  font-size:12px;max-height:250px;overflow-y:auto;display:none;
}
.pipeline-progress.active{display:block}
.pipeline-progress .line{padding:2px 0;color:#aaa}
.pipeline-progress .line.done{color:#4caf50;font-weight:600}
.pipeline-progress .line.error{color:#ef5350}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/builder">Builder</a>
    <a href="/analyze">Analyze</a>
    <a href="/library">Library</a>
    <a href="/wizard" class="active">AI Wizard</a>
  </nav>
</header>

<div class="content">
  <h2>AI Generator Wizard</h2>
  <p class="subtitle">Autonomous Instagram Reels generator — AI researches best practices, picks scenes, music, and transitions to maximize engagement.</p>

  <div class="config">
    <div class="config-row">
      <div class="field">
        <label>AI Model</label>
        <select id="model-select"></select>
      </div>
      <div class="field">
        <label>Videos</label>
        <input type="number" id="num-videos" value="1" min="1" max="10">
      </div>
      <div class="field">
        <label>A/B Variations</label>
        <input type="number" id="num-variations" value="1" min="1" max="3">
      </div>
      <div class="field">
        <label>Video Source</label>
        <div class="folder-picker" id="folder-picker">
          <button class="folder-btn" type="button" onclick="toggleDropdown('folder-dropdown')">
            <span id="folder-btn-label">All Folders</span>
            <span class="arrow">&#9660;</span>
          </button>
          <div class="folder-dropdown" id="folder-dropdown"></div>
        </div>
      </div>
      <div class="field">
        <label>Music Source</label>
        <div class="folder-picker" id="music-picker">
          <button class="folder-btn" type="button" onclick="toggleDropdown('music-dropdown')">
            <span id="music-btn-label">All Music</span>
            <span class="arrow">&#9660;</span>
          </button>
          <div class="folder-dropdown" id="music-dropdown"></div>
        </div>
      </div>
    </div>
    <div class="config-row" style="margin-top:12px">
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer">
        <input type="checkbox" id="mute-source"> Mute source audio (music only)
      </label>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer">
        <input type="checkbox" id="text-overlays"> Enable text overlays
      </label>
      <div class="btn-row">
        <button class="btn-primary" id="gen-btn" onclick="startGeneration()">Generate</button>
      </div>
    </div>
  </div>

  <div class="research-panel">
    <div class="research-toggle" onclick="toggleResearch()">
      <h3>Research Data <span class="research-status" id="research-status"></span></h3>
      <span class="arrow" id="research-arrow">&#9654;</span>
    </div>
    <div class="research-body" id="research-body">
      <div id="research-content"></div>
      <div class="research-actions">
        <button onclick="saveResearch()">Save Changes</button>
        <button class="btn-secondary" onclick="refreshResearch()">Re-generate with AI</button>
        <span class="save-status" id="research-save-status"></span>
      </div>
    </div>
  </div>

  <!-- pipeline section hidden -->
  <div class="pipeline-section" style="display:none">
    <h3>Auto-Pipeline</h3>
    <p>Scan for new videos, analyze them with AI, and generate an optimized reel — all in one click.</p>
    <div class="pipeline-btns">
      <button class="btn-primary" id="pipeline-btn" onclick="startPipeline()">Run Pipeline</button>
      <button class="btn-secondary" id="pipeline-stop-btn" onclick="stopPipeline()" style="display:none">Stop</button>
    </div>
    <div class="pipeline-progress" id="pipeline-progress">
      <div id="pipeline-lines"></div>
    </div>
  </div>

  <div class="excluded-panel" id="excluded-panel" style="display:none">
    <div class="excluded-toggle" onclick="toggleExcluded()">
      <h3>Excluded Scenes <span class="exc-count" id="exc-count"></span></h3>
      <span class="arrow" id="exc-arrow">&#9654;</span>
    </div>
    <div class="excluded-body" id="exc-body">
      <div class="excluded-grid" id="exc-grid"></div>
    </div>
  </div>

  <div class="progress" id="progress">
    <div id="progress-lines"></div>
  </div>

  <div class="results" id="results">
    <h3>Generated Videos</h3>
    <div id="result-cards"></div>
  </div>

  <div class="history" id="history">
    <h3>Recent Videos</h3>
    <div id="history-cards"></div>
  </div>
</div>


<script>
var generating = false;
var generatedVideos = [];

var currentResearch = null;

async function init() {
  // Load models
  var models = await fetch('/wizard/api/models').then(function(r){return r.json()});
  var sel = document.getElementById('model-select');
  for (var i = 0; i < models.length; i++) {
    var o = document.createElement('option');
    o.value = models[i].id;
    o.textContent = models[i].name;
    if (i === 0) o.selected = true;  // default to most capable
    sel.appendChild(o);
  }

  loadResearch();
  loadExcluded();
  loadFolders();
  loadMusicFolders();
  loadHistory();
}

async function loadHistory() {
  var history = await fetch('/wizard/api/history').then(function(r){return r.json()});
  var container = document.getElementById('history-cards');
  if (!history.length) {
    container.innerHTML = '<p style="color:#555;font-size:13px">No videos generated yet.</p>';
    return;
  }

  var html = '';
  for (var i = 0; i < history.length; i++) {
    var v = history[i];
    if (!v.exists) continue;
    var fbSummary = '';
    if (v.feedback && v.feedback.length) {
      fbSummary = '<div class="hist-fb">"' + escHtml(v.feedback[0].text) + '"</div>';
    } else {
      fbSummary = '<div class="hist-no-fb">No feedback yet</div>';
    }

    var fbBodyHtml = '';
    if (v.feedback && v.feedback.length) {
      for (var j = 0; j < v.feedback.length; j++) {
        fbBodyHtml += '<div class="hist-fb">"' + escHtml(v.feedback[j].text) + '"</div>';
      }
    }

    var date = formatDate(v.generated_at);
    html += '<div class="hist-card" id="hc-' + v.id + '">'
      + '<div class="hist-header" onclick="toggleHistCard(' + v.id + ')">'
      + '<div class="hist-info">'
      + '<div class="hist-name">' + escHtml(v.filename) + '</div>'
      + '<div class="hist-meta">' + v.duration + 's &middot; ' + date + '</div>'
      + fbSummary
      + '</div>'
      + '<span class="hist-expand-arrow" id="hc-arrow-' + v.id + '">&#9654;</span>'
      + '</div>'
      + '<div class="hist-body" id="hc-body-' + v.id + '">'
      + '<video controls preload="none" src="/wizard/api/video/' + v.id + '"></video>'
      + buildCaptionBox(v.caption || '', v.id)
      + buildSceneChips(v.scenes || [])
      + fbBodyHtml
      + '<div class="feedback-form">'
      + '<textarea id="fb-' + v.id + '" placeholder="Add feedback for this video..."></textarea>'
      + '<div class="fb-row">'
      + '<button onclick="submitFeedback(' + v.id + ')">Submit Feedback</button>'
      + '<span class="fb-status" id="fb-status-' + v.id + '"></span>'
      + '</div></div>'
      + '<div class="hist-actions">'
      + '<button class="btn-email" onclick="emailVideo(' + v.id + ',\'' + escHtml(v.filename) + '\')">'
      + '<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'
      + 'Email Video</button>'
      + '</div>'
      + '</div>'
      + '</div>';
  }
  container.innerHTML = html;
}

function toggleHistCard(id) {
  var body = document.getElementById('hc-body-' + id);
  var arrow = document.getElementById('hc-arrow-' + id);
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

// ── Folder & Music pickers ──

function toggleDropdown(id) {
  // Close all other dropdowns first
  document.querySelectorAll('.folder-dropdown.open').forEach(function(el) {
    if (el.id !== id) el.classList.remove('open');
  });
  document.getElementById(id).classList.toggle('open');
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.folder-picker')) {
    document.querySelectorAll('.folder-dropdown.open').forEach(function(el) {
      el.classList.remove('open');
    });
  }
});

async function loadFolders() {
  var folders = await fetch('/wizard/api/folders').then(function(r){return r.json()});
  var dd = document.getElementById('folder-dropdown');
  var html = '';
  for (var i = 0; i < folders.length; i++) {
    html += '<label class="folder-item">'
      + '<input type="checkbox" value="' + folders[i] + '" checked onchange="updatePickerLabel(\'folder-dropdown\',\'folder-btn-label\',\'All Folders\')"/>'
      + '<span class="fi-name">' + folders[i] + '</span></label>';
  }
  dd.innerHTML = html;
  updatePickerLabel('folder-dropdown', 'folder-btn-label', 'All Folders');
}

async function loadMusicFolders() {
  var folders = await fetch('/wizard/api/music-folders').then(function(r){return r.json()});
  var dd = document.getElementById('music-dropdown');
  var html = '';
  for (var i = 0; i < folders.length; i++) {
    html += '<label class="folder-item">'
      + '<input type="checkbox" value="' + folders[i] + '" checked onchange="updatePickerLabel(\'music-dropdown\',\'music-btn-label\',\'All Music\')"/>'
      + '<span class="fi-name">' + folders[i] + '</span></label>';
  }
  dd.innerHTML = html;
  updatePickerLabel('music-dropdown', 'music-btn-label', 'All Music');
}

function updatePickerLabel(ddId, labelId, allText) {
  var checks = document.querySelectorAll('#' + ddId + ' input[type=checkbox]');
  var total = checks.length;
  var checked = 0;
  for (var i = 0; i < checks.length; i++) {
    if (checks[i].checked) checked++;
  }
  var label = document.getElementById(labelId);
  if (checked === 0 || checked === total) {
    label.textContent = allText;
  } else {
    label.textContent = checked + ' of ' + total;
  }
}

function getSelectedFromPicker(ddId) {
  var checks = document.querySelectorAll('#' + ddId + ' input[type=checkbox]');
  var total = checks.length;
  var selected = [];
  for (var i = 0; i < checks.length; i++) {
    if (checks[i].checked) selected.push(checks[i].value);
  }
  if (selected.length === total) return [];
  return selected;
}

function startGeneration() {
  if (generating) return;
  generating = true;

  var model = document.getElementById('model-select').value;
  var numVids = parseInt(document.getElementById('num-videos').value) || 1;
  var numVars = parseInt(document.getElementById('num-variations').value) || 1;

  document.getElementById('gen-btn').disabled = true;
  generatedVideos = [];

  var prog = document.getElementById('progress');
  var lines = document.getElementById('progress-lines');
  prog.classList.add('active');
  lines.innerHTML = '';

  var results = document.getElementById('results');
  results.classList.remove('active');
  document.getElementById('result-cards').innerHTML = '';

  addLine('Starting AI Wizard...', 'phase');

  fetch('/wizard/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      model: model,
      num_videos: numVids,
      variations: numVars,
      folders: getSelectedFromPicker('folder-dropdown'),
      music_folders: getSelectedFromPicker('music-dropdown'),
      mute_source: document.getElementById('mute-source').checked,
      text_overlays: document.getElementById('text-overlays').checked,
    }),
  }).then(function() {
    var es = new EventSource('/wizard/api/status');
    es.onmessage = function(e) {
      var data = JSON.parse(e.data);
      var msg = data.message;

      if (msg.startsWith('DONE:')) {
        es.close();
        generating = false;
        document.getElementById('gen-btn').disabled = false;
        addLine(
          msg === 'DONE:ok' ? 'Generation complete!' : 'Generation failed',
          msg === 'DONE:ok' ? 'done' : 'error'
        );
        if (generatedVideos.length) {
          showResults();
        }
        loadHistory();

      } else if (msg.startsWith('VIDEO:')) {
        var parts = msg.split(':');
        generatedVideos.push({filename: parts[1], duration: parseFloat(parts[2])});
        addLine('Video ready: ' + parts[1] + ' (' + parts[2] + 's)', 'video-ready');

      } else if (msg.startsWith('Phase ')) {
        addLine(msg, 'phase');
      } else if (msg.startsWith('Strategy:')) {
        addLine(msg, 'strategy');
      } else {
        addLine(msg);
      }
    };
    es.onerror = function() {
      es.close();
      generating = false;
      document.getElementById('gen-btn').disabled = false;
      addLine('Connection lost', 'error');
    };
  }).catch(function(e) {
    generating = false;
    document.getElementById('gen-btn').disabled = false;
    addLine('Error: ' + e.message, 'error');
  });
}

function showResults() {
  var results = document.getElementById('results');
  results.classList.add('active');
  // Fetch latest history to get video IDs
  fetch('/wizard/api/history').then(function(r){return r.json()}).then(function(history) {
    var cards = document.getElementById('result-cards');
    cards.innerHTML = '';
    // Match generated filenames to history entries
    for (var i = 0; i < generatedVideos.length; i++) {
      var gen = generatedVideos[i];
      var match = null;
      for (var j = 0; j < history.length; j++) {
        if (history[j].filename === gen.filename && history[j].exists) {
          match = history[j];
          break;
        }
      }
      if (!match) continue;

      var card = document.createElement('div');
      card.className = 'result-card';
      card.innerHTML = '<div class="result-header">'
        + '<span class="filename">' + escHtml(match.filename) + '</span>'
        + '<span class="dur">' + match.duration + 's</span>'
        + '</div>'
        + '<video controls src="/wizard/api/video/' + match.id + '"></video>'
        + buildCaptionBox(match.caption, match.id)
        + buildSceneChips(match.scenes || [])
        + '<div class="hist-actions">'
        + '<button class="btn-email" onclick="emailVideo(' + match.id + ',\'' + escHtml(match.filename) + '\')">'
        + '<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'
        + 'Email Video</button>'
        + '</div>'
        + '<div class="feedback-form">'
        + '<textarea id="fb-' + match.id + '" placeholder="How was this video? Your feedback helps the AI improve future generations..."></textarea>'
        + '<div class="fb-row">'
        + '<button onclick="submitFeedback(' + match.id + ')">Submit Feedback</button>'
        + '<span class="fb-status" id="fb-status-' + match.id + '"></span>'
        + '</div></div>';
      cards.appendChild(card);
    }
  });
}

async function submitFeedback(videoId) {
  var textarea = document.getElementById('fb-' + videoId);
  var status = document.getElementById('fb-status-' + videoId);
  var text = textarea.value.trim();
  if (!text) return;

  var res = await fetch('/wizard/api/feedback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({video_id: videoId, feedback: text}),
  });

  if (res.ok) {
    status.textContent = 'Feedback saved! This will inform future generations.';
    textarea.disabled = true;
    loadHistory();
  } else {
    status.textContent = 'Failed to save feedback';
    status.style.color = '#ef5350';
  }
}

// ── Caption ──

function buildCaptionBox(caption, videoId) {
  if (!caption) {
    return '<div class="caption-box"><div class="cap-label">Caption</div>'
      + '<div class="cap-text" style="color:#555">No caption generated</div>'
      + '<div class="cap-actions">'
      + '<button class="cap-btn" onclick="regenCaption(' + videoId + ')">Generate Caption</button>'
      + '</div></div>';
  }
  return '<div class="caption-box"><div class="cap-label">Instagram Caption</div>'
    + '<div class="cap-text" id="cap-text-' + videoId + '">' + escHtml(caption) + '</div>'
    + '<div class="cap-actions">'
    + '<button class="cap-btn" id="cap-copy-' + videoId + '" onclick="copyCaption(' + videoId + ')">Copy</button>'
    + '<button class="cap-btn" onclick="regenCaption(' + videoId + ')">Regenerate</button>'
    + '</div></div>';
}

function copyCaption(videoId) {
  var text = document.getElementById('cap-text-' + videoId).textContent;
  navigator.clipboard.writeText(text).then(function() {
    var btn = document.getElementById('cap-copy-' + videoId);
    btn.classList.add('copied');
    btn.textContent = 'Copied!';
    setTimeout(function() { btn.classList.remove('copied'); btn.textContent = 'Copy'; }, 2000);
  });
}

async function regenCaption(videoId) {
  var model = document.getElementById('model-select').value;
  var box = event.target.closest('.caption-box');
  var textEl = box.querySelector('.cap-text');
  textEl.textContent = 'Generating caption...';
  textEl.style.color = '#888';

  try {
    var res = await fetch('/wizard/api/caption/' + videoId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model}),
    });
    var data = await res.json();
    if (data.caption) {
      textEl.textContent = data.caption;
      textEl.style.color = '#ccc';
      textEl.id = 'cap-text-' + videoId;
      // Ensure copy button exists
      var actions = box.querySelector('.cap-actions');
      if (!actions.querySelector('#cap-copy-' + videoId)) {
        var copyBtn = document.createElement('button');
        copyBtn.className = 'cap-btn';
        copyBtn.id = 'cap-copy-' + videoId;
        copyBtn.textContent = 'Copy';
        copyBtn.onclick = function() { copyCaption(videoId); };
        actions.insertBefore(copyBtn, actions.firstChild);
      }
    } else {
      textEl.textContent = 'Failed to generate caption';
      textEl.style.color = '#ef5350';
    }
  } catch(e) {
    textEl.textContent = 'Error: ' + e.message;
    textEl.style.color = '#ef5350';
  }
}

// ── Scene exclusion ──

function buildSceneChips(scenes) {
  if (!scenes || !scenes.length) return '';
  var html = '<div class="scenes-used"><div class="su-label">Scenes Used</div><div class="scene-chips">';
  for (var i = 0; i < scenes.length; i++) {
    var s = scenes[i];
    var cls = s.excluded ? 'scene-chip excluded' : 'scene-chip';
    var tags = s.tags.length ? s.tags.join(', ') : 'no tags';
    // Determine current vote state
    var downActive = s.excluded ? ' active' : '';
    var upActive = (!s.excluded && s.status === 'up') ? ' active' : '';

    html += '<div class="' + cls + '" id="sc-' + s.scene_id + '">'
      + '<img src="/api/thumbnail/' + s.scene_id + '" loading="lazy"/>'
      + '<div class="sc-info">'
      + '<span class="sc-name">#' + s.scene_id + '</span>'
      + '<span class="sc-tags">' + escHtml(tags) + '</span>'
      + '</div>'
      + '<div class="sc-votes">'
      + '<button class="sv sv-down' + downActive + '" onclick="voteScene(' + s.scene_id + ',\'down\')" title="Hide">'
      + '<svg viewBox="0 0 24 24"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>'
      + '</button>'
      + '<button class="sv sv-up' + upActive + '" onclick="voteScene(' + s.scene_id + ',\'up\')" title="Keep">'
      + '<svg viewBox="0 0 24 24"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>'
      + '</button>'
      + '</div>'
      + '</div>';
  }
  html += '</div></div>';
  return html;
}

async function voteScene(sceneId, action) {
  await fetch('/rate/api/grade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scene_id: sceneId, action: action}),
  });
  // Update all chips for this scene across the page
  var chips = document.querySelectorAll('#sc-' + sceneId);
  for (var i = 0; i < chips.length; i++) {
    var chip = chips[i];
    var downBtn = chip.querySelector('.sv-down');
    var upBtn = chip.querySelector('.sv-up');
    if (action === 'down') {
      chip.classList.add('excluded');
      downBtn.classList.add('active');
      upBtn.classList.remove('active');
    } else {
      chip.classList.remove('excluded');
      downBtn.classList.remove('active');
      upBtn.classList.add('active');
    }
  }
  loadExcluded();
}

function toggleExcluded() {
  var body = document.getElementById('exc-body');
  var arrow = document.getElementById('exc-arrow');
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

async function loadExcluded() {
  var res = await fetch('/wizard/api/excluded-scenes');
  var scenes = await res.json();
  var panel = document.getElementById('excluded-panel');
  var count = document.getElementById('exc-count');
  var grid = document.getElementById('exc-grid');

  if (!scenes.length) {
    panel.style.display = 'none';
    return;
  }

  panel.style.display = '';
  count.textContent = '(' + scenes.length + ')';

  var html = '';
  for (var i = 0; i < scenes.length; i++) {
    var s = scenes[i];
    var tags = s.tags.length ? s.tags.join(', ') : 'no tags';
    html += '<div class="scene-chip" id="exc-sc-' + s.id + '">'
      + '<img src="/api/thumbnail/' + s.id + '" loading="lazy"/>'
      + '<div class="sc-info">'
      + '<span class="sc-name">' + escHtml(s.filename) + '</span>'
      + '<span class="sc-tags">' + escHtml(tags) + ' &middot; ' + s.duration + 's</span>'
      + '</div>'
      + '<button class="sc-exclude" style="border-color:#4caf50;color:#4caf50" '
      + 'onclick="unblockScene(' + s.id + ')">Unblock</button>'
      + '</div>';
  }
  grid.innerHTML = html;
}

async function unblockScene(sceneId) {
  await fetch('/wizard/api/exclude', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scene_id: sceneId, exclude: false}),
  });
  loadExcluded();
  loadHistory();
}

async function emailVideo(videoId, filename) {
  var btn = event.target.closest('.btn-email');
  btn.disabled = true;
  var origText = btn.innerHTML;
  btn.innerHTML = btn.querySelector('svg').outerHTML + ' Opening Mail...';

  try {
    var res = await fetch('/wizard/api/email/' + videoId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        subject: 'PeaceGrappler Video - ' + filename,
        body: 'Check out this video from PeaceGrappler!',
      }),
    });
    var data = await res.json();
    if (data.status === 'ok') {
      btn.innerHTML = btn.querySelector('svg').outerHTML + ' Mail Opened!';
    } else {
      btn.innerHTML = btn.querySelector('svg').outerHTML + ' ' + (data.error || 'Failed');
    }
  } catch(e) {
    btn.innerHTML = btn.querySelector('svg').outerHTML + ' Error';
  }
  setTimeout(function() { btn.innerHTML = origText; btn.disabled = false; }, 3000);
}

// ── Research panel ──

function toggleResearch() {
  var body = document.getElementById('research-body');
  var arrow = document.getElementById('research-arrow');
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

async function loadResearch() {
  var res = await fetch('/wizard/api/research');
  var data = await res.json();
  currentResearch = data.research;
  renderResearch();
}

// Field definitions for the research editor
var RESEARCH_FIELDS = [
  {key:'optimal_duration', label:'Optimal Duration (seconds)', type:'number'},
  {key:'ideal_duration_range', label:'Duration Range', type:'range'},
  {key:'aspect_ratio', label:'Aspect Ratio', type:'text'},
  {key:'hook_strategy', label:'Hook Strategy (first 1-3s)', type:'textarea'},
  {key:'pacing_cuts_per_minute', label:'Pacing (cuts per minute)', type:'number'},
  {key:'content_structure', label:'Content Structure (phases)', type:'list'},
  {key:'music_strategy', label:'Music Strategy', type:'textarea'},
  {key:'transition_strategy', label:'Transition Strategy', type:'textarea'},
  {key:'opening_types', label:'Best Opening Types', type:'list'},
  {key:'closing_strategy', label:'Closing Strategy', type:'textarea'},
  {key:'engagement_tips', label:'Engagement Tips', type:'list'},
  {key:'avoid', label:'Things to Avoid', type:'list'},
];

function renderResearch() {
  var container = document.getElementById('research-content');
  var status = document.getElementById('research-status');

  if (!currentResearch) {
    container.innerHTML = '<div class="research-empty">No research data yet. Click "Re-generate with AI" or run a generation to create it.</div>';
    status.textContent = '(empty)';
    return;
  }

  status.textContent = '(loaded)';
  var html = '<div class="research-fields">';

  for (var i = 0; i < RESEARCH_FIELDS.length; i++) {
    var f = RESEARCH_FIELDS[i];
    var val = currentResearch[f.key];
    html += '<div class="research-field">';
    html += '<label>' + f.label + '</label>';

    if (f.type === 'number') {
      html += '<input type="number" id="rf-' + f.key + '" value="' + (val || 0) + '"/>';
    } else if (f.type === 'text') {
      html += '<input type="text" id="rf-' + f.key + '" value="' + escHtml(val || '') + '"/>';
    } else if (f.type === 'textarea') {
      var text = val || '';
      html += '<textarea id="rf-' + f.key + '" rows="' + calcRows(text) + '">' + escHtml(text) + '</textarea>';
    } else if (f.type === 'range') {
      var min = val ? (val.min || 0) : 0;
      var max = val ? (val.max || 0) : 0;
      html += '<div style="display:flex;gap:8px;align-items:center">'
        + '<input type="number" id="rf-' + f.key + '-min" value="' + min + '" style="width:80px"/> '
        + '<span style="color:#666">to</span> '
        + '<input type="number" id="rf-' + f.key + '-max" value="' + max + '" style="width:80px"/> '
        + '<span style="color:#666">seconds</span></div>';
    } else if (f.type === 'list') {
      var items = Array.isArray(val) ? val.join('\n') : (val || '');
      html += '<textarea id="rf-' + f.key + '" rows="' + calcRows(items) + '">' + escHtml(items) + '</textarea>';
      html += '<div class="hint">One item per line</div>';
    }

    html += '</div>';
  }

  html += '</div>';
  container.innerHTML = html;
}

function calcRows(text) {
  if (!text) return 2;
  var lines = text.split('\n');
  var rows = 0;
  for (var i = 0; i < lines.length; i++) {
    rows += Math.max(1, Math.ceil((lines[i].length + 1) / 85));
  }
  return Math.max(rows, 2);
}

function collectResearch() {
  var r = {};
  for (var i = 0; i < RESEARCH_FIELDS.length; i++) {
    var f = RESEARCH_FIELDS[i];
    if (f.type === 'number') {
      r[f.key] = parseFloat(document.getElementById('rf-' + f.key).value) || 0;
    } else if (f.type === 'text' || f.type === 'textarea') {
      r[f.key] = document.getElementById('rf-' + f.key).value;
    } else if (f.type === 'range') {
      r[f.key] = {
        min: parseFloat(document.getElementById('rf-' + f.key + '-min').value) || 0,
        max: parseFloat(document.getElementById('rf-' + f.key + '-max').value) || 0,
      };
    } else if (f.type === 'list') {
      var raw = document.getElementById('rf-' + f.key).value;
      r[f.key] = raw.split('\n').map(function(s){return s.trim()}).filter(function(s){return s});
    }
  }
  // Preserve any extra keys from Claude that we don't have fields for
  if (currentResearch) {
    for (var key in currentResearch) {
      if (!(key in r)) r[key] = currentResearch[key];
    }
  }
  return r;
}

async function saveResearch() {
  var research = collectResearch();
  var status = document.getElementById('research-save-status');
  status.textContent = 'Saving...';
  status.style.color = '#888';

  var res = await fetch('/wizard/api/research', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({research: research}),
  });

  if (res.ok) {
    currentResearch = research;
    status.textContent = 'Saved!';
    status.style.color = '#4caf50';
    document.getElementById('research-status').textContent = '(loaded)';
  } else {
    status.textContent = 'Failed to save';
    status.style.color = '#ef5350';
  }
  setTimeout(function() { status.textContent = ''; }, 3000);
}

async function refreshResearch() {
  var model = document.getElementById('model-select').value;
  var status = document.getElementById('research-save-status');
  status.textContent = 'Researching with AI...';
  status.style.color = '#818cf8';

  try {
    var res = await fetch('/wizard/api/refresh-research', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model}),
    });
    var data = await res.json();
    if (data.status === 'ok') {
      currentResearch = data.research;
      renderResearch();
      status.textContent = 'Research updated!';
      status.style.color = '#4caf50';
    } else {
      status.textContent = 'Failed: ' + (data.message || 'unknown error');
      status.style.color = '#ef5350';
    }
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
    status.style.color = '#ef5350';
  }
  setTimeout(function() { status.textContent = ''; }, 4000);
}


function addLine(text, cls) {
  var lines = document.getElementById('progress-lines');
  var div = document.createElement('div');
  div.className = 'line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  lines.appendChild(div);
  var prog = document.getElementById('progress');
  prog.scrollTop = prog.scrollHeight;
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  try {
    var d = new Date(dateStr + 'Z');
    return d.toLocaleDateString(undefined, {year:'numeric',month:'short',day:'numeric'})
      + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});
  } catch(e) { return dateStr; }
}

// ── Pipeline ──

var pipelineRunning = false;

function startPipeline() {
  if (pipelineRunning) return;
  pipelineRunning = true;
  var model = document.getElementById('model-select').value;

  document.getElementById('pipeline-btn').disabled = true;
  document.getElementById('pipeline-stop-btn').style.display = '';

  var prog = document.getElementById('pipeline-progress');
  var lines = document.getElementById('pipeline-lines');
  prog.classList.add('active');
  lines.innerHTML = '';
  addPipelineLine('Starting auto-pipeline...');

  fetch('/wizard/api/pipeline/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model: model}),
  }).then(function() {
    var es = new EventSource('/wizard/api/pipeline/status');
    es.onmessage = function(e) {
      var data = JSON.parse(e.data);
      var msg = data.message;
      if (msg.startsWith('DONE:')) {
        es.close();
        pipelineRunning = false;
        document.getElementById('pipeline-btn').disabled = false;
        document.getElementById('pipeline-stop-btn').style.display = 'none';
        addPipelineLine(
          msg === 'DONE:ok' ? 'Pipeline complete!' : (msg === 'DONE:stopped' ? 'Pipeline stopped.' : 'Pipeline failed'),
          msg === 'DONE:ok' ? 'done' : 'error'
        );
        loadHistory();
      } else if (msg.startsWith('Strategy:')) {
        addPipelineLine(msg, 'strategy');
      } else {
        addPipelineLine(msg);
      }
    };
    es.onerror = function() {
      es.close();
      pipelineRunning = false;
      document.getElementById('pipeline-btn').disabled = false;
      document.getElementById('pipeline-stop-btn').style.display = 'none';
      addPipelineLine('Connection lost', 'error');
    };
  });
}

function stopPipeline() {
  fetch('/wizard/api/pipeline/stop', {method: 'POST'});
  addPipelineLine('Stopping pipeline...');
}

function addPipelineLine(text, cls) {
  var lines = document.getElementById('pipeline-lines');
  var div = document.createElement('div');
  div.className = 'line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  lines.appendChild(div);
  var prog = document.getElementById('pipeline-progress');
  prog.scrollTop = prog.scrollHeight;
}

init();
</script>
</body>
</html>"""
