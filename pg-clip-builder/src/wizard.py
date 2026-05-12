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
    extract_wide_split, extract_wide_subclip_autocrop,
    find_asset, get_video_duration, normalize_clip,
    overlay_music,
)

wizard_bp = Blueprint("wizard", __name__)

import ai_cli  # noqa: E402  — provider-agnostic dispatcher

RESEARCH_TTL_DAYS = 7

progress = queue.Queue()


def emit(msg):
    progress.put(msg)


# ── DB helpers ───────────────────────────────────────────────────────────────

def _get_cached_research(include_provider=False):
    """Return cached research if fresh enough, else None.

    When *include_provider* is True, returns a dict
    ``{research, provider, researched_at}`` instead of just the research
    payload — used by /settings to render an attribution badge.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM wizard_research WHERE topic='instagram_reels' "
            "ORDER BY researched_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None if not include_provider else \
                {"research": None, "provider": "", "researched_at": ""}
        researched = datetime.fromisoformat(row["researched_at"])
        if datetime.utcnow() - researched > timedelta(days=RESEARCH_TTL_DAYS):
            if include_provider:
                # Return stale data with a flag so the UI can still show
                # the badge, but callers that drive AI behavior get None.
                return {"research": None, "provider": row["provider"] or "",
                        "researched_at": row["researched_at"]}
            return None
        payload = json.loads(row["result_json"])
        if include_provider:
            return {"research": payload, "provider": row["provider"] or "",
                    "researched_at": row["researched_at"]}
        return payload
    except Exception:
        return None if not include_provider else \
            {"research": None, "provider": "", "researched_at": ""}
    finally:
        conn.close()


def _save_research(result, provider=None, model=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO wizard_research (topic, result_json, provider, model) "
            "VALUES (?, ?, ?, ?)",
            ("instagram_reels", json.dumps(result), provider, model),
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
            "gv.timeline_json, gv.caption, "
            "gv.wizard_provider, gv.caption_provider, "
            "gv.wizard_model, gv.caption_model "
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
                "wizard_provider":  r["wizard_provider"] or "",
                "caption_provider": r["caption_provider"] or "",
                "wizard_model":     r["wizard_model"]     if "wizard_model"     in r.keys() else "",
                "caption_model":    r["caption_model"]    if "caption_model"    in r.keys() else "",
                "feedback": [{"text": f["feedback"], "at": f["created_at"]}
                             for f in fb_rows],
                "scenes": scenes_used,
            })
        return result
    except Exception:
        return []
    finally:
        conn.close()


# ── AI CLI helpers (delegates to ai_cli; provider chosen on /settings) ──────

# Thread-local provider override. The wizard's pipeline / generation
# threads set _call_ctx.provider at the top; every nested _call_claude
# inside that thread inherits it without needing to pass an extra arg
# through every helper. Outside the wizard threads it stays None and the
# task→provider mapping from /settings is used.
_call_ctx = threading.local()


def _active_provider_name(task="wizard"):
    """Return the provider name that will actually handle *task* right now —
    threadlocal override (set by the wizard pipeline) wins, otherwise the
    configured task→provider mapping. Use this whenever recording who
    produced a piece of generated content, so attribution matches the
    actual run (e.g. minimax → minimax badge) instead of always the
    default."""
    override = getattr(_call_ctx, "provider", None)
    return override or ai_cli.get_provider_for_task(task)


def _active_provider_and_model(task="wizard"):
    """Resolve both the provider AND the specific model that will handle
    *task* right now, respecting any threadlocal overrides. Returns
    ``(provider, model)``. Used to stamp DB rows with full attribution so
    the UI badge can show brand + version-on-hover."""
    override_p = getattr(_call_ctx, "provider", None)
    override_m = getattr(_call_ctx, "model", None)
    return ai_cli.resolve_provider_model(task,
                                          provider=override_p,
                                          model=override_m)


def _active_provider_label(task="wizard"):
    """Friendly label for the provider that will actually be used —
    threadlocal override wins, otherwise the task→provider config. Used in
    user-facing log lines so they read 'Gemini CLI is planning…' instead
    of always saying 'Claude'."""
    override = getattr(_call_ctx, "provider", None)
    cfg = ai_cli.get_config()
    name = override or cfg["tasks"].get(task, "claude")
    return (cfg["providers"].get(name) or {}).get("label") or name


def _call_claude(prompt_text, model, timeout=300, task="wizard",
                 provider=None):
    """Call the AI CLI configured for *task* and return its text response.

    *task* defaults to "wizard" (research + reel composition). Caption
    generation passes "captions" so users can route short copywriting to a
    different provider if they want.

    *provider* (optional) bypasses the task→provider mapping for this one
    call. If not given, picks up any provider override set on
    ``_call_ctx`` by the surrounding wizard thread.
    """
    p = provider or getattr(_call_ctx, "provider", None)
    return ai_cli.call_ai(
        prompt_text, task=task, model=model, timeout=timeout, on_log=emit,
        provider=p,
    )


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
for {domain} content.

Based on your knowledge of the current Instagram Reels algorithm and best \
practices (2025-2026), provide detailed, actionable recommendations for \
creating {domain} highlight reels that MAXIMIZE engagement (views, likes, \
shares, saves, and follows).

Consider: optimal video duration, pacing, hook strategy (first 1-3s), \
content structure, music usage, transition style, and what makes {domain} \
content go viral on Reels.

Return a JSON object with EXACTLY this structure:
{{
  "ideal_duration_range": {{"min": <seconds>, "max": <seconds>}},
  "optimal_duration": <seconds>,
  "aspect_ratio": "9:16",
  "hook_strategy": "<detailed strategy for first 1-3 seconds>",
  "pacing_cuts_per_minute": <number>,
  "content_structure": ["<phase1>", "<phase2>", ...],
  "music_strategy": "<how to use music for maximum engagement>",
  "transition_strategy": "<recommended transition approach for {domain} content>",
  "engagement_tips": ["<tip1>", "<tip2>", ...],
  "avoid": ["<thing to avoid 1>", ...],
  "opening_types": ["<best hook types for {domain}>", ...],
  "closing_strategy": "<how to end for max engagement>"
}}

Return ONLY the JSON object. No explanation, no markdown fences.
"""


def _run_research(model):
    """Run the research phase — returns cached or fresh research."""
    cached = _get_cached_research()
    if cached:
        emit("Using cached Instagram Reels research (less than 7 days old)")
        return cached

    emit("Researching Instagram Reels best practices...")
    try:
        import app_config
        raw = _call_claude(
            RESEARCH_PROMPT.format(**app_config.domain_vars()),
            model, timeout=120,
        )
        if not raw:
            emit("Research returned empty — using defaults")
            return _default_research()
        result = _parse_json(raw)
        _rp, _rm = _active_provider_and_model("wizard")
        _save_research(result, provider=_rp, model=_rm)
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
You are an expert video editor creating an Instagram Reel for a {domain} \
channel called {brand}. Your ONLY goal: MAXIMIZE ENGAGEMENT (views, \
likes, shares, saves).

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
Prefer scenes tagged "high-energy" or with action/impact tags from the available list.

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
        # When the user toggles "Enable AI text overlays" we want the model
        # to act like a social-video editor: surface punchy on-screen text
        # ONLY on the beats that would lift retention (hook, payoff,
        # surprise, climax, CTA). It's an explicit engagement tool — not a
        # decoration — so the instruction calls that out directly and tells
        # the model to skip overlays on clips where they'd hurt the read.
        text_instruction = (
            "- TEXT OVERLAYS ARE ENABLED. Treat \"text_overlay\" as a "
            "retention tool: insert short, punchy ALL-CAPS text ONLY where "
            "you judge it will measurably improve viewer engagement "
            "(stop-the-scroll hook, payoff moment, surprise reveal, "
            "climax, final CTA). 2-6 words, max one line, in the spirit "
            "of best-performing Reels for this domain.\n"
            "  - Aim for 3-5 overlays across the whole reel — quality "
            "beats quantity. Skip clips where text would clutter the "
            "moment or where the scene already speaks for itself.\n"
            "  - Examples (adapt to the actual content): 'WATCH THIS', "
            "'WAIT FOR IT', 'PURE POWER', 'YOU WON'T BELIEVE THIS', "
            "'SUBMISSION LOCKED', 'KO OF THE YEAR'.\n"
            "  - Place text on: the hook (1st clip), any payoff/reveal, "
            "the climax, the ending/CTA.\n"
            "  - Works on both portrait AND widescreen scenes.\n"
            "  - Set \"text_overlay\" to null for every clip that doesn't "
            "need one — do not pad."
        )
    else:
        text_instruction = (
            "- Text overlays are DISABLED. Set \"text_overlay\" to null for all clips."
        )

    import app_config
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
        **app_config.domain_vars(),
    )

    label = f"{_active_provider_label()} is planning the video"
    if variation_ctx and variation_ctx["total"] > 1:
        label += f" (variation {variation_ctx['num']}/{variation_ctx['total']})"
    emit(f"{label}...")
    raw = _call_claude(prompt, model, timeout=300)
    if not raw:
        return None

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        emit(f"Failed to parse {_active_provider_label()}'s plan: {e}")
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
                    mute_source=False, add_captions=False,
                    caption_style=None, auto_crop_wide=False):
    """Assemble a video from Claude's plan. Returns output path or None.

    *add_captions* — when True, burn each scene's transcript onto the
    extracted clip before subsequent steps (text overlay → mute → concat).
    Style is taken from *caption_style* (font/color/bg/position); see
    captions.build_ass for keys.
    """
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
            elif auto_crop_wide and is_wide:
                emit(f"Video {video_num}/{total}: clip {i+1}/{len(clips)} "
                     f"[{start:.1f}s +{duration:.1f}s] from {src} (auto-crop)")
                ok = extract_wide_subclip_autocrop(
                    scene["video_path"], start, duration, clip_out,
                )
                if not ok:
                    emit(f"  Auto-crop failed for clip {i+1}, "
                         f"falling back to letterboxed extract")
                    ok = extract_subclip(
                        scene["video_path"], start, duration, clip_out,
                    )
            else:
                emit(f"Video {video_num}/{total}: clip {i+1}/{len(clips)} "
                     f"[{start:.1f}s +{duration:.1f}s] from {src}")
                ok = extract_subclip(scene["video_path"], start, duration, clip_out)

            if ok:
                # Burn-in transcript captions if the user opted in.
                # Done before text-overlay so the wizard's overlay text
                # can render on top of the captions if both are enabled.
                if add_captions:
                    from db import get_transcript_for_clip
                    from captions import burn_captions
                    segs = get_transcript_for_clip(
                        scene["video_id"], clip["start"], clip["end"],
                    )
                    if segs:
                        cap_out = os.path.join(tmp, f"clip_{i:03d}_cap.mp4")
                        if burn_captions(clip_out, cap_out, segs,
                                         style=caption_style):
                            clip_out = cap_out
                        else:
                            emit(f"  Captions failed for clip {i+1}, "
                                 f"using clip without captions")

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
    _wp, _wm = _active_provider_and_model("wizard")
    save_generated_video(
        str(out_file), round(final_dur, 1), timeline,
        wizard_provider=_wp, wizard_model=_wm,
    )

    return {
        "path": str(out_file),
        "filename": out_file.name,
        "duration": round(final_dur, 1),
        "rationale": plan.get("rationale", ""),
    }


# ── Caption generation ──────────────────────────────────────────────────────

CAPTION_PROMPT = """\
You are a social media expert for a {domain} Instagram channel \
called {brand} ({handle}).

Generate an Instagram Reel caption + hashtags for a video with these details:
- Duration: {duration}s
- Creative strategy: {rationale}
- Tags/content: {tags}
- Music: {music}

Requirements:
- Caption should be 1-3 punchy lines that drive engagement (likes, comments, saves, shares)
- Include a hook or question to encourage comments
- Add 5-10 relevant hashtags (mix of broad {domain} hashtags + niche + trending)
- Format: caption text first, then hashtags on a new line
- Keep it authentic to {domain} culture
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

    import app_config
    dv = app_config.domain_vars()
    prompt = CAPTION_PROMPT.format(
        duration=duration,
        rationale=plan.get("rationale", f"{dv['domain']} highlight reel"),
        tags=", ".join(sorted(tags_used)),
        music=music_name,
        **dv,
    )

    try:
        raw = _call_claude(prompt, model, timeout=60, task="captions")
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


def _filter_scenes_by_files(scenes, filenames):
    """Filter scenes to those whose source video filename is in *filenames*.

    Used by the AI Builder's file-level Video Source picker (matches the
    Builder page's file filter UX). *filenames* is a list/set of bare
    filenames (basename only, no path).
    """
    targets = {str(f).strip() for f in filenames if f}
    if not targets:
        return scenes
    return [s for s in scenes
            if Path(s.get("video_path", "")).name in targets
            or s.get("video_filename") in targets]


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
              files=None,
              mute_source=False, enable_text_overlays=False,
              music_folders=None, include_wide=True, provider=None,
              no_music=False, add_captions=False, caption_style=None,
              auto_crop_wide=False):
    """Full wizard generation flow. Runs in a background thread.

    *no_music* — when True, the AI plans without a music track and the
    assembly step skips the overlay. Useful for spoken / instructional
    content where original audio carries the value.
    """
    # Stash the provider + model override so every nested _call_claude in
    # this thread routes through the user's chosen pair, and so the save
    # paths can stamp DB rows with the exact model that ran.
    _call_ctx.provider = provider
    _call_ctx.model    = model
    try:
        # 0. Auto-analyze unanalyzed videos in selected folders. Skip when
        # a per-file picker is in use — those files were already produced
        # by /api/clips, so they're analyzed by definition.
        if folders and not files:
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

        # Filter by file (new) or folder (legacy). Files win when present —
        # the wizard UI exposes file-level selection now.
        if files:
            before = len(scenes)
            scenes = _filter_scenes_by_files(scenes, files)
            emit(f"Filtered to {len(scenes)} scenes from {len(files)} "
                 f"selected file(s) (was {before}).")
        elif folders:
            from video import VIDEO_DIR
            scenes = _filter_scenes_by_folders(scenes, folders, VIDEO_DIR)
            emit(f"Filtered to {len(scenes)} scenes from selected folders")

        # Filter out wide scenes if not included
        if not include_wide:
            before = len(scenes)
            scenes = [s for s in scenes if not s.get("wide", False)]
            if len(scenes) < before:
                emit(f"Excluded {before - len(scenes)} widescreen scenes")

        if not scenes:
            emit("No analyzed scenes found for the selected folders.")
            emit("DONE:error")
            return

        if no_music:
            music_files = []
            emit("No-music mode: original audio only.")
        else:
            music_files = _find_music_files()
        # Filter music by selected folders if specified
        if music_folders and not no_music:
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
                    emit(f"{var_label}: {_active_provider_label()} couldn't create a plan")
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
                                        mute_source=mute_source,
                                        add_captions=add_captions,
                                        caption_style=caption_style,
                                        auto_crop_wide=auto_crop_wide)
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
                            _cp, _cm = _active_provider_and_model("captions")
                            update_video_caption(match["id"], caption,
                                                 provider=_cp, model=_cm)
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



# ── Routes ───────────────────────────────────────────────────────────────────

@wizard_bp.route("/wizard")
def wizard_page():
    from chrome import inject_chrome
    return inject_chrome(WIZARD_HTML, active="wizard")


@wizard_bp.route("/wizard/api/models")
def api_models():
    """Return the cross-provider model catalog so the wizard dropdown can
    list every option (cheapest first) regardless of which provider is set
    for the wizard task.

    Shape:
      {
        groups:   [{provider, label, bin_found, models:[id,...]}, ...]
        cheapest: {provider, model}            ← cheapest installed combo
      }
    """
    cfg = ai_cli.get_config()
    groups = []
    for key, p in cfg["providers"].items():
        groups.append({
            "provider":  key,
            "label":     p.get("label", key),
            "bin_found": bool(p.get("bin_found")),
            "default":   p.get("model"),
            "models":    list(p.get("models") or []),
        })
    cheapest = None
    for r in cfg.get("cheapest_rank", []):
        pr = cfg["providers"].get(r["provider"]) or {}
        if pr.get("bin_found"):
            cheapest = {"provider": r["provider"], "model": r["model"]}
            break
    return jsonify({"groups": groups, "cheapest": cheapest})


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
    model = (data.get("model") or "").strip() or None
    provider = (data.get("provider") or "").strip() or None
    num_videos = max(1, min(10, data.get("num_videos", 1)))
    num_variations = max(1, min(3, data.get("variations", 1)))
    folders = data.get("folders", []) or []
    # File-level Video Source picker — new wizard UI. Server prefers
    # ``files`` over ``folders`` when both are present.
    files_pick = data.get("files", []) or []
    mute_source = data.get("mute_source", False)
    enable_text_overlays = data.get("text_overlays", False)
    music_folders = data.get("music_folders", [])
    include_wide = data.get("include_wide", True)
    no_music = bool(data.get("no_music", False))
    add_captions = bool(data.get("add_captions", False))
    auto_crop_wide = bool(data.get("auto_crop_wide", False))
    caption_style = data.get("caption_style") or {}

    threading.Thread(
        target=_generate,
        kwargs={
            "model": model,
            "num_videos": num_videos,
            "num_variations": num_variations,
            "folders": folders,
            "files": files_pick,
            "mute_source": mute_source,
            "enable_text_overlays": enable_text_overlays,
            "music_folders": music_folders,
            "include_wide": include_wide,
            "provider": provider,
            "no_music": no_music,
            "add_captions": add_captions,
            "caption_style": caption_style,
            "auto_crop_wide": auto_crop_wide,
        },
        daemon=True,
    ).start()
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
    model = (data.get("model") or "").strip() or None
    provider = (data.get("provider") or "").strip() or None
    # Synchronous endpoint — set the provider/model override on this
    # request's thread for the duration of the call.
    _call_ctx.provider = provider
    _call_ctx.model    = model

    from db import get_all_generated_videos
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    # Build a minimal plan from timeline
    import app_config
    timeline = video.get("timeline", [])
    plan = {"clips": [], "music": {},
            "rationale": f"{app_config.get_config()['content_domain']} highlight reel"}
    for item in timeline:
        if item.get("type") == "clip":
            plan["clips"].append(item)
        elif item.get("type") == "music":
            plan["music"] = {"name": item.get("name")}

    caption = _generate_caption(model, plan, video["duration"])
    if caption:
        _cp, _cm = _active_provider_and_model("captions")
        update_video_caption(video_id, caption, provider=_cp, model=_cm)
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
    """Return the current cached research with provider attribution."""
    bundle = _get_cached_research(include_provider=True) or {}
    return jsonify({
        "research":      bundle.get("research"),
        "provider":      bundle.get("provider") or "",
        "researched_at": bundle.get("researched_at") or "",
    })


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
    # User edits don't have a provider — preserve the most recent one we
    # have (so the badge keeps reflecting whoever produced the original
    # cache) rather than dropping attribution.
    prev = _get_cached_research(include_provider=True) or {}
    _save_research(research, provider=(prev.get("provider") or None))
    return jsonify({"status": "saved"})


@wizard_bp.route("/wizard/api/refresh-research", methods=["POST"])
def api_refresh_research():
    """Force refresh the cached research.

    Body (all optional):
      provider — claude/codex/gemini override (bypasses the task→provider map)
      model    — override the provider's configured default model
    """
    import ai_cli
    import app_config as _ac

    data = request.json or {}
    provider_override = (data.get("provider") or "").strip() or None
    model_override = (data.get("model") or "").strip() or None

    # Delete old cache
    conn = get_db()
    try:
        conn.execute("DELETE FROM wizard_research")
        conn.commit()
    finally:
        conn.close()

    try:
        raw = ai_cli.call_ai(
            RESEARCH_PROMPT.format(**_ac.domain_vars()),
            task="wizard",
            provider=provider_override,
            model=model_override,
            timeout=180,
            on_log=emit,
        )
        if raw:
            result = _parse_json(raw)
            actual_provider, actual_model = ai_cli.resolve_provider_model(
                "wizard", provider=provider_override, model=model_override,
            )
            _save_research(result, provider=actual_provider,
                                    model=actual_model)
            return jsonify({"status": "ok", "research": result,
                            "provider": actual_provider,
                            "model":    actual_model})
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
    import app_config as _ac
    subject = data.get("subject",
                       f"{_ac.get_config()['brand_name']} Video - {path.name}")
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


# ── HTML ─────────────────────────────────────────────────────────────────────

WIZARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClipBuilder - AI Builder</title>
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
  display:none;position:absolute;top:100%;left:0;
  background:#222;border:1px solid #444;border-radius:6px;
  margin-top:4px;max-height:250px;overflow-y:auto;z-index:20;
  padding:4px 0;width:max-content;min-width:220px;max-width:min(90vw,900px);
}
.folder-dropdown.open{display:block}
.folder-item{
  display:flex;align-items:center;gap:8px;padding:6px 12px;
  cursor:pointer;font-size:12px;color:#ccc;
}
.folder-item:hover{background:#333}
.folder-item input{accent-color:#e53935}
.folder-item .fi-name{flex:1;white-space:nowrap}
.folder-item .fi-count{color:#777;font-size:10px;margin-left:8px;flex-shrink:0}
.folder-actions .folder-act-btn{
  flex:1;font-size:11px;padding:5px 10px;background:#1a1a1a;
  border:1px solid #333;color:#ddd;border-radius:4px;cursor:pointer;
  text-align:center;
}
.folder-actions .folder-act-btn:hover{border-color:#666;color:#fff}
/* "No Music" sits at the top of the music picker and is mutually
 * exclusive with the regular folder rows. */
.folder-item.fi-nomusic{
  border-bottom:1px solid #1a1a24;font-weight:600;color:#fff;
}

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
.result-card .result-layout{display:flex;gap:20px;align-items:flex-start}
.result-card .result-video{flex-shrink:0;width:280px}
.result-card .result-video video{
  width:100%;border-radius:8px;background:#000;
}
.result-card .result-detail{flex:1;min-width:0}
@media(max-width:700px){
  .result-card .result-layout{flex-direction:column}
  .result-card .result-video{width:100%}
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
.hist-layout{display:flex;gap:20px;align-items:flex-start;margin-top:12px}
.hist-video{flex-shrink:0;width:280px}
.hist-video video{width:100%;border-radius:8px;background:#000}
.hist-detail{flex:1;min-width:0}
.hist-body .feedback-form{margin-top:12px}
@media(max-width:700px){
  .hist-layout{flex-direction:column}
  .hist-video{width:100%}
}
.hist-actions{
  display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap;
}
.btn-edit,.btn-email{
  display:inline-flex;align-items:center;gap:6px;
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 14px;font-size:12px;cursor:pointer;
}
.btn-edit:hover,.btn-email:hover{border-color:#666;color:#fff}
.btn-edit svg,.btn-email svg{width:14px;height:14px;fill:currentColor}

</style>
</head>
<body>

<!-- pg-chrome -->

<div class="content">
  <h2>AI Builder</h2>
  <p class="subtitle">Autonomous reel generator — AI researches best practices, picks scenes, music, and transitions to maximize engagement.</p>

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
        <!-- File-level picker (matches the Builder's filter). Lists each
             individual source video with its analyzed-scene count so the
             user can include / exclude specific files instead of broad
             folders. Populated from /api/clips on init. -->
        <div class="folder-picker" id="folder-picker">
          <button class="folder-btn" type="button" onclick="toggleDropdown('file-dropdown')">
            <span id="file-btn-label">All files</span>
            <span class="arrow">&#9660;</span>
          </button>
          <div class="folder-dropdown ff-dropdown" id="file-dropdown">
            <div class="folder-actions" style="display:flex;gap:6px;padding:6px 8px 4px">
              <button type="button" class="folder-act-btn" onclick="wizFileSelectAll(true)">Select all</button>
              <button type="button" class="folder-act-btn" onclick="wizFileSelectAll(false)">Deselect all</button>
            </div>
            <input type="search" id="file-search" placeholder="Filter files…"
                   oninput="renderFileList()"
                   style="margin:4px 8px 8px;width:calc(100% - 16px);padding:6px 8px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:4px;font-size:12px;box-sizing:border-box">
            <div id="file-list" class="folder-list" style="max-height:240px;overflow-y:auto"></div>
          </div>
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
        <input type="checkbox" id="mute-source"> Mute source audio
      </label>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer"
             title="When enabled, the AI inserts short, punchy text overlays on engagement-driving moments (hook, climax, ending).">
        <input type="checkbox" id="text-overlays"> Enable AI text overlays
      </label>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer">
        <input type="checkbox" id="include-wide" checked> Include widescreen scenes
      </label>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer"
             title="For widescreen scenes, auto-detect the most visually interesting region and crop to 9:16 (1080x1920) instead of letterboxing.">
        <input type="checkbox" id="auto-crop-wide"> Auto-crop wide videos
      </label>
      <div style="display:flex;align-items:center;gap:4px">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer"
               title="Burn the spoken transcript onto each scene as on-screen captions.">
          <input type="checkbox" id="add-captions"> Add captions
        </label>
        <button type="button" id="cap-cfg-btn" onclick="openCaptionConfig()"
                title="Customize caption style for this run"
                style="background:transparent;border:1px solid #2e2e3e;color:#aaa;
                       cursor:pointer;border-radius:4px;width:24px;height:22px;
                       display:inline-flex;align-items:center;justify-content:center;
                       font-size:13px;line-height:1;padding:0">⚙</button>
      </div>
    </div>

    <!-- Generate button on its own row. -->
    <div class="config-row" style="margin-top:14px;justify-content:flex-end">
      <button class="btn-primary" id="gen-btn" onclick="startGeneration()">✨ Generate Video</button>
    </div>

    <!-- Caption settings modal — opens via ⚙ button next to "Add captions".
         Defaults come from the active brand profile; tweaks apply to this
         run only unless the user clicks "Save as default". -->
    <div id="cap-cfg-overlay"
         style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
                z-index:9000;align-items:center;justify-content:center;
                padding:20px;box-sizing:border-box">
      <div style="background:#15151c;border:1px solid #2e2e3e;border-radius:10px;
                  width:440px;max-width:100%;padding:18px;
                  box-shadow:0 12px 40px rgba(0,0,0,.6);
                  box-sizing:border-box;overflow:hidden">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
          <h3 style="font-size:14px;font-weight:600;color:#fff;flex:1;margin:0">Caption settings</h3>
          <button onclick="closeCaptionConfig()"
                  style="background:none;border:none;color:#888;font-size:20px;
                         cursor:pointer;padding:0 4px;line-height:1">&times;</button>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;width:100%">
          <div style="min-width:0">
            <label style="font-size:11px;color:#888;display:block;margin-bottom:4px">Font</label>
            <select id="cap-font" style="width:100%;padding:6px 8px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:5px;font-size:12px;box-sizing:border-box">
              <option value="sans">Sans-serif</option>
              <option value="serif">Serif</option>
              <option value="mono">Monospace</option>
            </select>
          </div>
          <div style="min-width:0">
            <label style="font-size:11px;color:#888;display:block;margin-bottom:4px">Position</label>
            <select id="cap-pos" style="width:100%;padding:6px 8px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:5px;font-size:12px;box-sizing:border-box">
              <option value="bottom">Bottom</option>
              <option value="middle">Middle</option>
              <option value="top">Top</option>
            </select>
          </div>
          <div style="min-width:0">
            <label style="font-size:11px;color:#888;display:block;margin-bottom:4px">Text color</label>
            <input type="color" id="cap-color" value="#ffffff"
                   style="width:100%;height:32px;padding:2px;background:#0c0c14;border:1px solid #2e2e3e;border-radius:5px;box-sizing:border-box">
          </div>
          <div style="min-width:0">
            <label style="font-size:11px;color:#888;display:block;margin-bottom:4px">Background color</label>
            <input type="color" id="cap-bg" value="#000000"
                   style="width:100%;height:32px;padding:2px;background:#0c0c14;border:1px solid #2e2e3e;border-radius:5px;box-sizing:border-box">
          </div>
        </div>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;cursor:pointer;margin-top:10px">
          <input type="checkbox" id="cap-bg-on"> Show colored background behind text
        </label>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
          <button onclick="captionConfigSaveDefault()"
                  style="background:#1a1a22;border:1px solid #2e2e3e;color:#aaa;
                         padding:7px 14px;border-radius:6px;cursor:pointer;font-size:12px">
            Save as default
          </button>
          <button onclick="closeCaptionConfig()"
                  style="background:#e53935;border:1px solid #e53935;color:#fff;
                         padding:7px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
            Done
          </button>
        </div>
        <div id="cap-cfg-msg" style="font-size:11px;color:#888;margin-top:8px;min-height:14px"></div>
      </div>
    </div>
  </div>

  <!-- Research data was moved to /settings (Strategy Research section). -->

  <div class="progress" id="progress">
    <div id="progress-lines"></div>
  </div>

  <div class="results" id="results">
    <h3>Generated Reels</h3>
    <div id="result-cards"></div>
  </div>
</div>


<script>
var generating = false;
var generatedVideos = [];

var currentResearch = null;

// Caption defaults from the active brand profile. Populated by loadAppCfg()
// on init and re-loaded after "Save as default" so the modal reflects what
// the wizard will actually use for this run.
var captionDefaults = {
  font: 'sans', color: '#ffffff',
  bg_on: false, bg_color: '#000000',
  position: 'bottom',
};

async function init() {
  await loadModels();
  loadFiles();
  loadMusicFolders();
  await loadAppCfg();
}

async function loadAppCfg() {
  try {
    var cfg = await fetch('/settings/api/app').then(function(r){return r.json()});
    if (cfg && cfg.captions) {
      captionDefaults = {
        font:     cfg.captions.font     || 'sans',
        color:    cfg.captions.color    || '#ffffff',
        bg_on:    !!cfg.captions.bg_on,
        bg_color: cfg.captions.bg_color || '#000000',
        position: cfg.captions.position || 'bottom',
      };
    }
    applyCaptionDefaultsToModal();
  } catch (e) { /* keep built-in defaults */ }
}

function applyCaptionDefaultsToModal() {
  var f = document.getElementById('cap-font');     if (f) f.value     = captionDefaults.font;
  var c = document.getElementById('cap-color');    if (c) c.value     = captionDefaults.color;
  var b = document.getElementById('cap-bg');       if (b) b.value     = captionDefaults.bg_color;
  var o = document.getElementById('cap-bg-on');    if (o) o.checked   = captionDefaults.bg_on;
  var p = document.getElementById('cap-pos');      if (p) p.value     = captionDefaults.position;
}

function openCaptionConfig() {
  // Pre-fill from the active brand profile so the user sees current defaults
  // each time they open the modal. They can override for this run.
  applyCaptionDefaultsToModal();
  document.getElementById('cap-cfg-msg').textContent = '';
  document.getElementById('cap-cfg-overlay').style.display = 'flex';
}

function closeCaptionConfig() {
  document.getElementById('cap-cfg-overlay').style.display = 'none';
}

function currentCaptionStyle() {
  return {
    font:     document.getElementById('cap-font').value,
    color:    document.getElementById('cap-color').value,
    bg_on:    document.getElementById('cap-bg-on').checked,
    bg_color: document.getElementById('cap-bg').value,
    position: document.getElementById('cap-pos').value,
  };
}

async function captionConfigSaveDefault() {
  var msg = document.getElementById('cap-cfg-msg');
  msg.textContent = 'Saving...';
  try {
    // Read current config first so we don't clobber other fields.
    var cfg = await fetch('/settings/api/app').then(function(r){return r.json()});
    var body = {
      profile_name:   cfg.profile_name,
      brand_name:     cfg.brand_name,
      content_domain: cfg.content_domain,
      source_folder:  cfg.source_folder,
      output_folder:  cfg.output_folder,
      intro_video:    cfg.intro_video,
      outro_video:    cfg.outro_video,
      brand_color:    cfg.brand_color || '',
      tag_schema:     cfg.tag_schema,
      socials:        cfg.socials,
      analysis_mode:  cfg.analysis_mode,
      transcribe_provider: cfg.transcribe_provider,
      transcribe_model:    cfg.transcribe_model,
      whisper_model:  cfg.whisper_model,
      whisper_language:  cfg.whisper_language,
      whisper_translate: cfg.whisper_translate,
      ai:             cfg.ai,
      captions:       currentCaptionStyle(),
    };
    var r = await fetch('/settings/api/app', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    var d = await r.json();
    if (d.ok === false) { msg.textContent = 'Save failed: ' + (d.error || 'unknown'); return; }
    captionDefaults = body.captions;
    msg.textContent = 'Saved as default for this brand.';
  } catch (e) { msg.textContent = 'Save failed: ' + e.message; }
}

async function loadModels() {
  var data = await fetch('/wizard/api/models').then(function(r){return r.json()});
  var sel = document.getElementById('model-select');
  sel.innerHTML = '';
  var groups = data.groups || [];
  var cheapest = data.cheapest || null;
  var cheapestVal = cheapest ? (cheapest.provider + '::' + cheapest.model) : '';

  for (var gi = 0; gi < groups.length; gi++) {
    var g = groups[gi];
    var optgroup = document.createElement('optgroup');
    optgroup.label = g.label + (g.bin_found ? '' : '  (binary missing)');
    for (var mi = 0; mi < g.models.length; mi++) {
      var m = g.models[mi];
      var opt = document.createElement('option');
      opt.value = g.provider + '::' + m;
      var label = m;
      if (m === g.default) label += '   (default)';
      if (cheapest && g.provider === cheapest.provider && m === cheapest.model) {
        label += '   💸 cheapest';
      }
      opt.textContent = label;
      optgroup.appendChild(opt);
    }
    sel.appendChild(optgroup);
  }
  // Default to the cheapest installed (provider, model) combo overall.
  if (cheapestVal && [].slice.call(sel.options).some(function(o){return o.value===cheapestVal})) {
    sel.value = cheapestVal;
  } else if (sel.options.length) {
    sel.selectedIndex = 0;
  }
}

// Used by every "click Generate / Regenerate" handler so the wizard
// always sends both fields and the server can override the task→provider
// mapping for this single run.
function getSelectedModel() {
  var raw = (document.getElementById('model-select') || {}).value || '';
  var sep = raw.indexOf('::');
  if (sep > 0) {
    return {provider: raw.slice(0, sep), model: raw.slice(sep + 2)};
  }
  return {provider: '', model: raw};
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

// ── Video Source: file-level picker (mirrors the Builder's filter) ────
// We populate from /api/clips so the picker lists the actual scenes the
// wizard will see, and each row shows how many analyzed scenes come from
// that file (useful signal for what'll be available for selection).
var _wizFiles = [];           // [{name, count}, ...]
var _wizFileSelected = null;  // Set<filename>; null until first load

async function loadFiles() {
  var counts = {};
  try {
    var clips = await fetch('/api/clips').then(function(r){return r.json()});
    for (var i = 0; i < clips.length; i++) {
      var fn = clips[i].filename || '';
      if (!fn) continue;
      counts[fn] = (counts[fn] || 0) + 1;
    }
  } catch (e) { /* leave _wizFiles empty */ }
  _wizFiles = Object.keys(counts).sort().map(function(fn){
    return {name: fn, count: counts[fn]};
  });
  _wizFileSelected = new Set(_wizFiles.map(function(f){return f.name}));
  renderFileList();
  wizFileUpdateLabel();
}

function renderFileList() {
  var list = document.getElementById('file-list');
  if (!list) return;
  var q = (document.getElementById('file-search').value || '').toLowerCase();
  var html = '';
  for (var i = 0; i < _wizFiles.length; i++) {
    var f = _wizFiles[i];
    if (q && f.name.toLowerCase().indexOf(q) < 0) continue;
    var checked = _wizFileSelected.has(f.name) ? ' checked' : '';
    var safe = f.name.replace(/"/g, '&quot;');
    html += '<label class="folder-item">'
      + '<input type="checkbox" data-fn="' + safe + '"' + checked
      +   ' onchange="wizFileToggle(this)">'
      + '<span class="fi-name" title="' + safe + '">' + safe + '</span>'
      + '<span class="fi-count">' + f.count + '</span>'
      + '</label>';
  }
  list.innerHTML = html
    || '<div style="font-size:11px;color:#666;padding:8px 12px">No matching files.</div>';
}

function wizFileToggle(input) {
  var fn = input.getAttribute('data-fn');
  if (input.checked) _wizFileSelected.add(fn);
  else _wizFileSelected.delete(fn);
  wizFileUpdateLabel();
}

function wizFileSelectAll(on) {
  _wizFileSelected = on
    ? new Set(_wizFiles.map(function(f){return f.name}))
    : new Set();
  renderFileList();
  wizFileUpdateLabel();
}

function wizFileUpdateLabel() {
  var el = document.getElementById('file-btn-label');
  if (!el) return;
  var total = _wizFiles.length;
  var n = _wizFileSelected.size;
  if (total === 0)         el.textContent = 'No files yet';
  else if (n === total)    el.textContent = 'All files (' + total + ')';
  else if (n === 0)        el.textContent = 'No files';
  else                     el.textContent = n + ' / ' + total + ' files';
}

// Returns the explicit list of filenames to include in this run, or [] if
// "every file" is selected (server treats [] as no filter for parity with
// the old folders API).
function getWizSelectedFiles() {
  if (!_wizFileSelected || _wizFileSelected.size === _wizFiles.length) return [];
  return Array.from(_wizFileSelected);
}

async function loadMusicFolders() {
  var folders = await fetch('/wizard/api/music-folders').then(function(r){return r.json()});
  var dd = document.getElementById('music-dropdown');
  // "No Music" sits at the top as a mutually-exclusive choice — checking
  // it deselects the folder rows; checking any folder deselects it.
  var html = '<label class="folder-item fi-nomusic">'
    + '<input type="checkbox" id="music-nomusic"'
    +   ' onchange="onMusicNoMusicToggle(this)">'
    + '<span class="fi-name">No Music</span></label>';
  for (var i = 0; i < folders.length; i++) {
    html += '<label class="folder-item">'
      + '<input type="checkbox" value="' + folders[i] + '" checked'
      +   ' onchange="onMusicFolderToggle(this)"/>'
      + '<span class="fi-name">' + folders[i] + '</span></label>';
  }
  dd.innerHTML = html;
  updatePickerLabel('music-dropdown', 'music-btn-label', 'All Music');
}

function onMusicNoMusicToggle(input) {
  // Mutually exclusive: when "No Music" is checked, uncheck every folder.
  if (input.checked) {
    var folderChecks = document.querySelectorAll(
      '#music-dropdown input[type=checkbox]:not(#music-nomusic)'
    );
    for (var i = 0; i < folderChecks.length; i++) folderChecks[i].checked = false;
  }
  updateMusicLabel();
}

function onMusicFolderToggle(input) {
  if (input.checked) {
    var nm = document.getElementById('music-nomusic');
    if (nm && nm.checked) nm.checked = false;
  }
  updateMusicLabel();
}

function updateMusicLabel() {
  var nm = document.getElementById('music-nomusic');
  var label = document.getElementById('music-btn-label');
  if (nm && nm.checked) {
    if (label) label.textContent = 'No music';
    return;
  }
  // Same logic as updatePickerLabel but ignores the No-Music checkbox so
  // "All Music" reflects the folder rows only.
  var checks = document.querySelectorAll(
    '#music-dropdown input[type=checkbox]:not(#music-nomusic)'
  );
  var total = checks.length, checked = 0;
  for (var i = 0; i < checks.length; i++) if (checks[i].checked) checked++;
  if (!label) return;
  if (checked === 0 || checked === total) label.textContent = 'All Music';
  else label.textContent = checked + ' of ' + total;
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

  var sel = getSelectedModel();
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

  addLine('Starting AI Builder...', 'phase');

  fetch('/wizard/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      model: sel.model,
      provider: sel.provider,
      num_videos: numVids,
      variations: numVars,
      files: getWizSelectedFiles(),
      music_folders: (function(){
        // Exclude the special No-Music checkbox from the folder list.
        var checks = document.querySelectorAll(
          '#music-dropdown input[type=checkbox]:not(#music-nomusic)'
        );
        var total = checks.length, selected = [];
        for (var i = 0; i < checks.length; i++) {
          if (checks[i].checked) selected.push(checks[i].value);
        }
        return (selected.length === total) ? [] : selected;
      })(),
      mute_source: document.getElementById('mute-source').checked,
      no_music: !!(document.getElementById('music-nomusic')
                   && document.getElementById('music-nomusic').checked),
      text_overlays: document.getElementById('text-overlays').checked,
      include_wide: document.getElementById('include-wide').checked,
      auto_crop_wide: document.getElementById('auto-crop-wide').checked,
      add_captions: document.getElementById('add-captions').checked,
      caption_style: {
        font:     document.getElementById('cap-font').value,
        color:    document.getElementById('cap-color').value,
        bg:       document.getElementById('cap-bg-on').checked
                    ? document.getElementById('cap-bg').value
                    : null,
        position: document.getElementById('cap-pos').value,
      },
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

      // Inline provider badges so the user sees which AI composed the
      // reel (e.g. minimax logo when minimax was selected) and which one
      // wrote the caption, when they differ.
      var wizModel = match.wizard_model || '';
      var capModel = match.caption_model || '';
      var wizBadge = (window.pgAiBadge && match.wizard_provider)
        ? ' ' + window.pgAiBadge(match.wizard_provider, {
            size: 13,
            model: wizModel,
            title: 'Reel composed by ' + match.wizard_provider
                  + (wizModel ? ' · ' + wizModel : ''),
          }) : '';
      var capBadge = (window.pgAiBadge && match.caption_provider
                      && (match.caption_provider !== match.wizard_provider
                          || capModel !== wizModel))
        ? ' ' + window.pgAiBadge(match.caption_provider, {
            size: 11,
            model: capModel,
            title: 'Caption by ' + match.caption_provider
                  + (capModel ? ' · ' + capModel : ''),
          }) : '';

      var card = document.createElement('div');
      card.className = 'result-card';
      card.innerHTML = '<div class="result-header">'
        + '<span class="filename">' + escHtml(match.filename) + wizBadge + capBadge + '</span>'
        + '<span class="dur">' + match.duration + 's</span>'
        + '</div>'
        + '<div class="result-layout">'
        + '<div class="result-video">'
        + '<video controls src="/wizard/api/video/' + match.id + '"></video>'
        + '</div>'
        + '<div class="result-detail">'
        + buildCaptionBox(match.caption, match.id)
        + buildSceneChips(match.scenes || [])
        + '<div class="hist-actions">'
        + '<button class="btn-edit" onclick="editInBuilder(\'' + escHtml(match.filename) + '\')">'
        + '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>'
        + 'Edit</button>'
        + '<button class="btn-email" onclick="emailVideo(' + match.id + ',\'' + escHtml(match.filename) + '\')">'
        + '<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'
        + 'Email Video</button>'
        + '</div>'
        + '<div class="feedback-form">'
        + '<textarea id="fb-' + match.id + '" placeholder="How was this video? Your feedback helps the AI improve future generations..."></textarea>'
        + '<div class="fb-row">'
        + '<button onclick="submitFeedback(' + match.id + ')">Submit Feedback</button>'
        + '<span class="fb-status" id="fb-status-' + match.id + '"></span>'
        + '</div></div>'
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
      + '<button class="cap-btn" onclick="regenCaption(' + videoId + ')">✨ Generate Caption</button>'
      + '</div></div>';
  }
  return '<div class="caption-box"><div class="cap-label">Instagram Caption</div>'
    + '<div class="cap-text" id="cap-text-' + videoId + '">' + escHtml(caption) + '</div>'
    + '<div class="cap-actions">'
    + '<button class="cap-btn" id="cap-copy-' + videoId + '" onclick="copyCaption(' + videoId + ')">Copy</button>'
    + '<button class="cap-btn" onclick="regenCaption(' + videoId + ')">✨ Regenerate</button>'
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
  var sel = getSelectedModel();
  var box = event.target.closest('.caption-box');
  var textEl = box.querySelector('.cap-text');
  textEl.textContent = 'Generating caption...';
  textEl.style.color = '#888';

  try {
    var res = await fetch('/wizard/api/caption/' + videoId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: sel.model, provider: sel.provider}),
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
}

function editInBuilder(filename) {
  window.location.href = '/builder?load=' + encodeURIComponent(filename);
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
        subject: ((window.PG_APP && window.PG_APP.brand) || 'Video')
                 + ' Video - ' + filename,
        body: 'Check out this video from '
              + ((window.PG_APP && window.PG_APP.brand) || '') + '!',
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

init();
</script>
</body>
</html>"""
