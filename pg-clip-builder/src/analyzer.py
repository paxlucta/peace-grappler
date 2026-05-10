"""analyzer.py — Video analysis routes for PeaceGrappler."""

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

import db
from db import (
    add_scene_tag, get_all_videos, get_analyzed_tags, get_db,
    get_imported_external_ids, get_tag_vote_signature, get_video_by_id,
    record_imported_external, register_video, save_analysis,
    set_scene_excluded,
)
from video import VIDEO_DIR, VIDEO_EXTENSIONS, get_video_dimensions, get_video_duration

analyzer_bp = Blueprint("analyzer", __name__)

import ai_cli
import app_config

# ── master tag list ──────────────────────────────────────────────────────────
#
# Sourced from app_config so users can replace the schema for non-MMA
# domains (cooking, skateboarding, weddings, …). Read at module load —
# restart the app after editing the schema in /settings for changes to take
# effect.
TAGS = app_config.get_tag_schema()

ALL_TAGS = []
for group in TAGS.values():
    ALL_TAGS.extend(group)
ALL_TAG_SET = set(ALL_TAGS)

# System-applied marker tag — NOT in Claude's vocabulary. Set by post-analysis
# auto-hide and by the vote-learning pass. Scenes carrying this tag are also
# excluded=True. Removed when a user up-votes the scene.
AUTO_HIDDEN_TAG = "auto-hidden"

# ── server-side analysis state ────────────────────────────────────────────────

progress_queue = queue.Queue()
_analysis_lock = threading.Lock()
_analysis_state = {
    "running": False,
    "video_id": None,
    "video_name": None,
    "mode": None,       # 'visual' | 'speech' — which kind of pass is running
    "queued": [],       # list of {"id": int, "force": bool}
    "completed": 0,
    "total": 0,
}


def emit_progress(msg):
    progress_queue.put(msg)


def _get_status_snapshot():
    with _analysis_lock:
        return {
            "running": _analysis_state["running"],
            "video_id": _analysis_state["video_id"],
            "video_name": _analysis_state["video_name"],
            "mode": _analysis_state.get("mode"),
            "queued": len(_analysis_state["queued"]),
            "completed": _analysis_state["completed"],
            "total": _analysis_state["total"],
        }


# ── frame extraction ─────────────────────────────────────────────────────────

def extract_frames(video_path, duration):
    """Extract frames at regular intervals."""
    if duration <= 0:
        return []

    if duration <= 10:
        interval = 1.0
    elif duration <= 60:
        interval = 2.0
    else:
        interval = 3.0

    timestamps = []
    t = 0.5
    while t < duration - 0.3:
        timestamps.append(t)
        t += interval
    timestamps = timestamps[:30]

    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(timestamps):
            out = os.path.join(tmp, f"frame_{i:03d}.jpg")
            try:
                r = subprocess.run(
                    ["ffmpeg", "-ss", f"{ts:.2f}", "-i", str(video_path),
                     "-frames:v", "1", "-q:v", "4", "-y", out],
                    capture_output=True, timeout=15,
                )
                if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                    frames.append((open(out, "rb").read(), f"{ts:.1f}s"))
            except Exception:
                pass
    return frames


# ── AI CLI dispatch (delegates to ai_cli; provider chosen on /settings) ─────

def call_claude(frames, prompt_text):
    """Send frames + prompt to the active AI CLI, return raw text response.

    Function name retained for callers; the underlying provider is chosen on
    the /settings page (claude / codex / gemini).

    Raises ``ai_cli.AIQuotaError`` when the provider hits a hard quota /
    billing limit — analyze_speech / analyze_full both catch it and bail
    instead of grinding through every remaining batch.
    """
    return ai_cli.call_ai(
        prompt_text, task="analysis", frames=frames,
        timeout=120, on_log=emit_progress,
    )


def parse_json_response(raw):
    """Extract JSON object or array from Claude's raw text response."""
    raw = re.sub(r"^\s*```[a-z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()

    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return json.loads(raw)


# ── prompts ──────────────────────────────────────────────────────────────────

FULL_ANALYSIS_PROMPT = """\
You are analyzing frames from a {domain} video.
Video duration: {duration:.1f}s. Frames are shown at their timestamps.

Your job: produce a TAG-CENTRIC analysis. For each tag that applies to this
video, provide the TIME RANGES where that tag is present. Also note any
important moments (dialog, key events).

AVAILABLE TAGS (only use tags from this list):
{tag_list}

Return a JSON object with this exact structure:
{{
  "tags": {{
    "tag_name": [{{"start": 0.0, "end": 5.2}}, {{"start": 12.0, "end": 18.5}}],
    "another_tag": [{{"start": 0.0, "end": 30.0}}]
  }},
  "moments": [
    {{"at": 3.5, "note": "clean right hook lands", "dialog": null}},
    {{"at": 15.0, "note": "coach gives instructions", "dialog": "Mao na cara dele [EN: Hand on his face]"}}
  ]
}}

RULES:
- Only include tags that actually appear in the video
- Time ranges can overlap -- e.g. "striking" and "high-energy" can cover different ranges
- A tag can have multiple ranges if it appears at different times
- Be precise with timestamps -- use the frame timestamps as anchors
- Ranges must be within 0.0 to {duration:.1f}
- A broad tag like "cage" can span the entire video if applicable
- For "moments": include dialog/speech (with English translation if not English),
  key events, visible on-screen text, and any notable points useful for montage editing
- Apply "low-quality" to ranges that are unusable for a highlight reel:
  badly out-of-focus, motion-blurred to the point of being unreadable,
  black/blank/transition frames, severe shaky-cam, accidental footage
  (filmer's feet, lens cap), or visually broken (compression artifacts).
  Do NOT apply "low-quality" just because the action is calm or boring --
  only when the FOOTAGE itself is unusable.
- Return ONLY the JSON object, no markdown fences, no explanation
"""

INCREMENTAL_TAG_PROMPT = """\
You are analyzing frames from a {domain} video.
Video duration: {duration:.1f}s. Frames are shown at their timestamps.

This video has already been analyzed for some tags. Now I need you to check
for ONLY these NEW tags:
{new_tags}

For each of these tags that appears in the video, provide the time ranges
where it is present. Skip any tag that doesn't apply.

Return a JSON object:
{{
  "tags": {{
    "tag_name": [{{"start": 0.0, "end": 5.2}}, ...],
    ...
  }}
}}

RULES:
- Only check for the tags listed above -- ignore everything else
- Time ranges must be within 0.0 to {duration:.1f}
- Be precise with timestamps using the frame timestamps as anchors
- Return ONLY the JSON object, no markdown fences, no explanation
- If NONE of the new tags apply, return: {{"tags": {{}}}}
"""


# ── analysis functions ───────────────────────────────────────────────────────

def analyze_full(video_path, duration):
    """Full tag-centric analysis of a video."""
    emit_progress(f"Extracting frames from {Path(video_path).name}...")
    frames = extract_frames(video_path, duration)
    if not frames:
        emit_progress(f"No frames extracted from {Path(video_path).name}")
        return None

    emit_progress(f"Extracted {len(frames)} frames, sending to Claude (full analysis)...")

    tag_list = ""
    for group_name, tags in TAGS.items():
        tag_list += f"  {group_name.upper()}: {', '.join(tags)}\n"

    prompt = FULL_ANALYSIS_PROMPT.format(
        duration=duration, tag_list=tag_list,
        domain=app_config.get_config()["content_domain"],
    )

    try:
        raw = call_claude(frames, prompt)
        if not raw:
            return None
        result = parse_json_response(raw)
    except ai_cli.AIQuotaError as e:
        emit_progress(
            f"AI quota exhausted — aborting. {e}. "
            f"Switch provider on /settings or wait for the quota to reset."
        )
        return None
    except Exception as e:
        emit_progress(f"Analysis failed: {e}")
        return None

    tags = result.get("tags", {})
    moments = result.get("moments", [])

    # Validate and clamp time ranges
    clean_tags = {}
    for tag, ranges in tags.items():
        if tag not in ALL_TAG_SET:
            continue
        clean_ranges = []
        for r in ranges:
            s = max(0, round(float(r.get("start", 0)), 1))
            e = min(duration, round(float(r.get("end", duration)), 1))
            if e > s:
                clean_ranges.append({"start": s, "end": e})
        if clean_ranges:
            clean_tags[tag] = clean_ranges

    # Validate moments
    clean_moments = []
    for m_item in moments:
        at = round(float(m_item.get("at", 0)), 1)
        if 0 <= at <= duration:
            clean_moments.append({
                "at": at,
                "note": m_item.get("note", ""),
                "dialog": m_item.get("dialog"),
            })

    emit_progress(f"Got {len(clean_tags)} tags, {len(clean_moments)} moments")
    return {"tags": clean_tags, "moments": clean_moments}


# ── Speech-mode analysis ────────────────────────────────────────────────────

SPEECH_BATCH_PROMPT = """\
You are tagging scenes from a {domain} video. Below are several scenes —
each is a short segment of spoken content with its time range and a
representative video frame.

For each scene, return the applicable tags from the list. Each scene
should get 0-5 tags, focused on what's specifically happening in THAT
scene (not the whole video).

AVAILABLE TAGS (only use tags from this list):
{tag_list}

Scenes (the video frames are attached in order, one per scene):
{scene_lines}

Return ONLY a JSON object with this shape:
{{
  "scenes": [
    {{"index": 1, "tags": ["tag1", "tag2"], "topic": "brief 2-5 word summary"}},
    {{"index": 2, "tags": [...], "topic": "..."}},
    ...
  ]
}}

Rules:
- Tag values must come from the AVAILABLE TAGS list exactly. Skip if none apply.
- "topic" is a 2-5 word summary of what the scene is about.
- Apply "low-quality" only when the FOOTAGE is unusable (out-of-focus,
  black/blank, accidental footage). Don't tag low-quality just because
  the topic is dull.
- Return ONLY the JSON object. No markdown fences, no explanation.
"""


def _sample_frame_at(video_path, timestamp):
    """Extract one JPEG frame at *timestamp* (seconds). Returns bytes or
    None on failure."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "f.jpg")
        try:
            r = subprocess.run(
                ["ffmpeg", "-ss", f"{max(0, timestamp):.2f}",
                 "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "4", "-y", out],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return open(out, "rb").read()
        except Exception:
            pass
    return None


def analyze_speech(video_path, duration, overrides=None):
    """Speech-mode analysis: Whisper transcribes audio → each transcript
    segment becomes a scene → AI tags each scene from frame + spoken text.

    *overrides* (optional) — per-video values that replace the brand
    profile defaults: ``{whisper_model, whisper_language, whisper_translate}``.

    Returns the same {tags, moments} shape as analyze_full() so the
    existing save_analysis() works unchanged.
    """
    import audio_analysis
    cfg = app_config.get_config()
    o = overrides or {}
    model = (o.get("whisper_model") or cfg.get("whisper_model") or "base")
    language = (o.get("whisper_language") if "whisper_language" in o
                else cfg.get("whisper_language")) or None
    translate = bool(o["whisper_translate"]) if "whisper_translate" in o \
                else bool(cfg.get("whisper_translate", False))

    result = audio_analysis.transcribe(
        video_path, model=model, language=language, translate=translate,
        on_log=emit_progress,
    )
    segments = result.get("segments", [])
    detected_language = (result.get("language") or "").strip().lower()
    if not segments:
        emit_progress("No speech detected — falling back to visual analysis.")
        return analyze_full(video_path, duration)

    emit_progress(
        f"Got {len(segments)} transcript segments. Sampling frames..."
    )

    # Persist the raw (un-merged) transcript so it's searchable and viewable
    # per scene. We need a video_id to attach to — register the video if the
    # caller hasn't already (analyze_full does this too; idempotent on hash).
    try:
        video_id = db.register_video(video_path)
        is_xlat_pass = bool(translate)
        db.save_transcripts(
            video_id, segments,
            language=(detected_language or (language or "")),
            is_translation=is_xlat_pass,
        )
        # Bilingual: if the source language isn't English and the user
        # didn't already ask for translate-mode, run a second pass to get
        # an English version stored alongside the original. This way the
        # scene-level transcript modal can show both, and search works in
        # either language.
        if (not is_xlat_pass
                and detected_language
                and detected_language != "en"):
            emit_progress(
                f"Source is '{detected_language}' — generating English "
                f"translation alongside original..."
            )
            xlat = audio_analysis.transcribe(
                video_path, model=model, language=language, translate=True,
                on_log=emit_progress,
            )
            xlat_segs = xlat.get("segments", [])
            if xlat_segs:
                db.save_transcripts(
                    video_id, xlat_segs,
                    language=detected_language,  # remembered as the SOURCE lang
                    is_translation=True,
                )
                emit_progress(
                    f"Saved {len(xlat_segs)} translated segments."
                )
    except Exception as e:
        emit_progress(f"(transcript persist warning: {e})")

    # Collapse very short segments into their neighbors so we don't waste
    # AI calls on 0.3s "uh" segments. Target minimum 2s per scene.
    merged = []
    cur = None
    MIN_DUR = 2.0
    for seg in segments:
        if cur is None:
            cur = dict(seg)
            continue
        cur_dur = cur["end"] - cur["start"]
        if cur_dur < MIN_DUR:
            cur["end"] = seg["end"]
            cur["text"] = (cur["text"] + " " + seg["text"]).strip()
        else:
            merged.append(cur)
            cur = dict(seg)
    if cur is not None:
        merged.append(cur)
    segments = merged
    emit_progress(f"Merged short segments → {len(segments)} scenes.")

    # Build the AI tag list (with categories) once.
    tag_list = ""
    for group_name, tags in TAGS.items():
        tag_list += f"  {group_name.upper()}: {', '.join(tags)}\n"

    # Process in batches — each batch = up to 8 segments + their frames.
    BATCH = 8
    all_tags = {}    # tag_name -> [{start, end}, ...]
    all_moments = []

    for batch_start in range(0, len(segments), BATCH):
        batch = segments[batch_start:batch_start + BATCH]
        emit_progress(
            f"Tagging scenes {batch_start + 1}-{batch_start + len(batch)} "
            f"of {len(segments)}..."
        )

        # Extract one frame per segment (midpoint).
        frames = []
        scene_lines = []
        for i, seg in enumerate(batch):
            mid = (seg["start"] + seg["end"]) / 2
            jpeg = _sample_frame_at(video_path, mid)
            if jpeg:
                frames.append((jpeg, f"{mid:.1f}s"))
            text_short = seg["text"][:200]
            scene_lines.append(
                f"[Scene {i + 1}] [{seg['start']:.1f}-{seg['end']:.1f}s] "
                f"\"{text_short}\""
            )

        prompt = SPEECH_BATCH_PROMPT.format(
            domain=app_config.get_config()["content_domain"],
            tag_list=tag_list,
            scene_lines="\n".join(scene_lines),
        )

        try:
            raw = call_claude(frames, prompt)
            if not raw:
                emit_progress(f"  ↳ batch returned empty — skipping")
                continue
            parsed = parse_json_response(raw)
        except ai_cli.AIQuotaError as e:
            emit_progress(
                f"AI quota exhausted — aborting analysis. {e}. "
                f"Switch provider on /settings or wait for the quota to "
                f"reset, then re-run analysis."
            )
            # Stash whatever we tagged before the quota hit so it still
            # gets saved, then propagate so the worker drains its queue
            # instead of hammering the same exhausted provider on every
            # remaining video.
            err = ai_cli.AIQuotaError(str(e))
            err.partial = {"tags": all_tags, "moments": all_moments}
            raise err
        except Exception as e:
            emit_progress(f"  ↳ batch failed: {e}")
            continue

        # Walk each returned scene and project into the same {tags, moments}
        # shape used by save_analysis.
        for scene_out in (parsed.get("scenes") or []):
            try:
                idx = int(scene_out.get("index", 0)) - 1
                if idx < 0 or idx >= len(batch):
                    continue
                seg = batch[idx]
                tags = scene_out.get("tags") or []
                topic = (scene_out.get("topic") or "").strip()
                for t in tags:
                    if t not in ALL_TAG_SET:
                        continue
                    all_tags.setdefault(t, []).append({
                        "start": round(seg["start"], 1),
                        "end":   round(seg["end"],   1),
                    })
                # Use the transcript text as a moment so it survives in the
                # DB and the wizard can read it back.
                all_moments.append({
                    "at":     round(seg["start"], 1),
                    "note":   topic or seg["text"][:80],
                    "dialog": seg["text"],
                })
            except Exception:
                continue

    emit_progress(
        f"Speech analysis complete — {len(all_tags)} unique tags across "
        f"{len(segments)} scenes."
    )
    return {"tags": all_tags, "moments": all_moments}


def analyze_incremental(video_path, duration, new_tags):
    """Analyze only new tags for an already-analyzed video."""
    emit_progress(f"Extracting frames for incremental analysis...")
    frames = extract_frames(video_path, duration)
    if not frames:
        emit_progress(f"No frames extracted from {Path(video_path).name}")
        return {}

    emit_progress(f"Checking {len(new_tags)} new tags for {Path(video_path).name}...")

    prompt = INCREMENTAL_TAG_PROMPT.format(
        duration=duration,
        new_tags=", ".join(sorted(new_tags)),
        domain=app_config.get_config()["content_domain"],
    )

    try:
        raw = call_claude(frames, prompt)
        if not raw:
            return {}
        result = parse_json_response(raw)
    except Exception as e:
        emit_progress(f"Incremental analysis failed: {e}")
        return {}

    tags = result.get("tags", {})

    clean_tags = {}
    for tag, ranges in tags.items():
        if tag not in new_tags:
            continue
        clean_ranges = []
        for r in ranges:
            s = max(0, round(float(r.get("start", 0)), 1))
            e = min(duration, round(float(r.get("end", duration)), 1))
            if e > s:
                clean_ranges.append({"start": s, "end": e})
        if clean_ranges:
            clean_tags[tag] = clean_ranges

    emit_progress(f"Found {len(clean_tags)} new tags with ranges")
    return clean_tags


# ── routes ───────────────────────────────────────────────────────────────────

@analyzer_bp.route("/analyze")
def analyze_page():
    from chrome import inject_chrome
    return inject_chrome(ANALYZE_HTML, active="analyze")


@analyzer_bp.route("/analyze/scan", methods=["POST"])
def scan_videos():
    """Scan videos/ directory, register new files in DB."""
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    registered = 0
    for root, _, files in os.walk(VIDEO_DIR):
        for name in sorted(files):
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS and not name.startswith("."):
                path = Path(root) / name
                register_video(path)
                registered += 1

    videos = get_all_videos()
    transcript_video_ids = db.get_video_ids_with_transcripts()
    result = []
    for v in videos:
        analyzed_tags = get_analyzed_tags(v["id"])
        new_tags = ALL_TAG_SET - analyzed_tags
        result.append({
            "id": v["id"],
            "filename": v["filename"],
            "path": v["path"],
            "duration": round(v["duration"], 1),
            "width": v["width"],
            "height": v["height"],
            "wide": bool(v["wide"]),
            "analyzed_at": v["analyzed_at"],
            "analyzed_tag_count": len(analyzed_tags),
            "total_tag_count": len(ALL_TAG_SET),
            "needs_update": len(new_tags) > 0,
            "analyzer_provider": v["analyzer_provider"]
                if "analyzer_provider" in v.keys() else None,
            "has_transcript": v["id"] in transcript_video_ids,
            "visual_analyzed_at": v["visual_analyzed_at"]
                if "visual_analyzed_at" in v.keys() else None,
            "speech_analyzed_at": v["speech_analyzed_at"]
                if "speech_analyzed_at" in v.keys() else None,
            "visual_analyzer_provider": v["visual_analyzer_provider"]
                if "visual_analyzer_provider" in v.keys() else None,
            "speech_analyzer_provider": v["speech_analyzer_provider"]
                if "speech_analyzer_provider" in v.keys() else None,
        })

    return jsonify({"registered": registered, "videos": result})


def _analyze_one(video_id, force, overrides=None):
    """Analyze a single video. Runs inside the worker thread.

    *overrides* (optional) — dict of per-run overrides that beat the
    active brand profile's defaults:
        mode             — 'visual' | 'speech'
        whisper_model    — tiny/base/small/medium/large-v3
        whisper_language — ISO code or ''
        whisper_translate— bool
    Used by the per-video Visual / Audio buttons + ⚙ popover on /analyze.
    """
    overrides = overrides or {}
    video = get_video_by_id(video_id)
    if not video:
        emit_progress(f"Video {video_id} not found")
        return False

    with _analysis_lock:
        _analysis_state["video_id"] = video_id
        _analysis_state["video_name"] = video["filename"]

    try:
        video_path = video["path"]
        duration = video["duration"]

        if duration <= 0:
            emit_progress(f"Cannot read duration for {video['filename']}")
            return False

        analyzed_tags = get_analyzed_tags(video_id)
        new_tags = ALL_TAG_SET - analyzed_tags

        # Capture which AI produced this analysis so the UI can attribute it.
        provider = ai_cli.get_provider_for_task("analysis")

        # Per-run override beats the brand profile mode.
        mode = (overrides.get("mode")
                or app_config.get_config().get("analysis_mode")
                or "visual")
        with _analysis_lock:
            _analysis_state["mode"] = mode

        if force or not analyzed_tags:
            try:
                if mode == "speech":
                    emit_progress(
                        f"Speech analysis of {video['filename']} ({duration:.1f}s)..."
                    )
                    result = analyze_speech(video_path, duration,
                                            overrides=overrides)
                else:
                    emit_progress(
                        f"Visual analysis of {video['filename']} ({duration:.1f}s)..."
                    )
                    result = analyze_full(video_path, duration)
            except ai_cli.AIQuotaError as e:
                # Save whatever the call accumulated before the quota
                # hit, then bubble up so the worker stops processing the
                # rest of the queue.
                partial = getattr(e, "partial", None) or {"tags": {}, "moments": []}
                if partial["tags"] or partial["moments"]:
                    save_analysis(video_id, partial["tags"], partial["moments"],
                                  list(ALL_TAG_SET), provider=provider, mode=mode)
                    emit_progress(
                        f"Saved {len(partial['tags'])} partial tags before quota hit."
                    )
                raise
            if result is None:
                emit_progress("Analysis failed")
                return False
            save_analysis(video_id, result["tags"], result["moments"],
                          list(ALL_TAG_SET), provider=provider, mode=mode)
            emit_progress(f"Saved {len(result['tags'])} tags")

        elif new_tags:
            emit_progress(f"Incremental analysis ({len(new_tags)} new tags)...")
            new_tag_results = analyze_incremental(video_path, duration, new_tags)
            if new_tag_results:
                save_analysis(video_id, new_tag_results, [], list(new_tags),
                              provider=provider, mode=mode)
                emit_progress(f"Saved {len(new_tag_results)} new tags")
            else:
                save_analysis(video_id, {}, [], list(new_tags),
                              provider=provider, mode=mode)
                emit_progress("No new tags found")

        else:
            emit_progress("Video is up to date, nothing to analyze")

        # Post-analysis: auto-hide low-quality scenes flagged by Claude.
        try:
            n = auto_hide_low_quality_scenes(video_id=video_id)
            if n:
                emit_progress(f"Auto-hid {n} low-quality scene(s)")
        except Exception as e:
            emit_progress(f"Auto-hide skipped: {e}")

        return True
    except Exception as e:
        emit_progress(f"Error: {e}")
        return False


# ── Auto-hide ────────────────────────────────────────────────────────────────

def auto_hide_low_quality_scenes(video_id=None):
    """Mark every scene tagged 'low-quality' as auto-hidden + excluded.

    If *video_id* is given, scope to that video. Returns count of scenes hidden.
    """
    conn = get_db()
    try:
        sql = (
            "SELECT s.id FROM scenes s "
            "JOIN scene_tags t ON t.scene_id = s.id "
            "WHERE t.tag = 'low-quality'"
        )
        params = ()
        if video_id is not None:
            sql += " AND s.video_id = ?"
            params = (video_id,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    hidden = 0
    for r in rows:
        sid = r["id"]
        add_scene_tag(sid, AUTO_HIDDEN_TAG)
        set_scene_excluded(sid, True)
        hidden += 1
    return hidden


def auto_hide_from_votes(down_threshold=0.7, min_tag_votes=2,
                         min_scene_score=0.6):
    """Use manual votes to predict bad scenes and auto-hide them.

    Algorithm:
      1. From manually graded scenes (excluding system-hidden ones), build a
         per-tag down-vote signature: each tag gets a `down_rate` in [0,1]
         and a vote count.
      2. "Blacklist" tags = those with `down_rate >= down_threshold` and at
         least `min_tag_votes` total votes.
      3. For every unrated, non-excluded scene that carries at least one
         blacklist tag: compute its predicted-down score (mean of down_rate
         over its blacklist tags). If it crosses `min_scene_score`, hide it.

    Returns dict: {hidden, blacklist_tags, scanned}.
    """
    sig = get_tag_vote_signature(
        min_votes=min_tag_votes, exclude_tag=AUTO_HIDDEN_TAG,
    )
    blacklist = {
        tag: c["down_rate"] for tag, c in sig.items()
        if c["down_rate"] >= down_threshold
    }
    if not blacklist:
        return {"hidden": 0, "blacklist_tags": [], "scanned": 0}

    conn = get_db()
    try:
        # Candidate scenes: not already excluded, no manual grade yet.
        rows = conn.execute(
            """SELECT s.id FROM scenes s
               WHERE s.excluded = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM grades g WHERE g.scene_id = s.id
                 )"""
        ).fetchall()
        scanned = len(rows)

        hidden = 0
        for r in rows:
            sid = r["id"]
            tags = [
                t["tag"] for t in conn.execute(
                    "SELECT tag FROM scene_tags WHERE scene_id=?", (sid,)
                ).fetchall()
            ]
            matched = [blacklist[t] for t in tags if t in blacklist]
            if not matched:
                continue
            score = sum(matched) / len(matched)
            if score >= min_scene_score:
                add_scene_tag(sid, AUTO_HIDDEN_TAG)
                set_scene_excluded(sid, True)
                hidden += 1
    finally:
        conn.close()

    return {
        "hidden": hidden,
        "scanned": scanned,
        "blacklist_tags": [
            {"tag": t, "down_rate": round(r, 2)}
            for t, r in sorted(blacklist.items(),
                               key=lambda x: -x[1])
        ],
    }


def _worker_loop():
    """Background worker that processes the analysis queue."""
    while True:
        with _analysis_lock:
            if not _analysis_state["queued"]:
                _analysis_state["running"] = False
                _analysis_state["video_id"] = None
                _analysis_state["video_name"] = None
                _analysis_state["mode"] = None
                emit_progress("QUEUE:done")
                return
            item = _analysis_state["queued"].pop(0)

        vid_id = item["id"]
        force = item["force"]
        overrides = item.get("overrides") or None
        emit_progress(f"--- Video {_analysis_state['completed'] + 1}/{_analysis_state['total']} ---")
        try:
            ok = _analyze_one(vid_id, force, overrides=overrides)
        except ai_cli.AIQuotaError:
            # Hard provider failure — drain the queue and stop. Anything
            # already saved stays; the user can re-run later or switch
            # providers on /settings.
            with _analysis_lock:
                _analysis_state["completed"] += 1
                _analysis_state["queued"].clear()
            emit_progress(f"VIDEO:{vid_id}:error")
            emit_progress(
                "Queue cleared. Switch AI provider on /settings or wait "
                "for quota to reset, then re-run."
            )
            continue

        with _analysis_lock:
            _analysis_state["completed"] += 1
        emit_progress(f"VIDEO:{vid_id}:{'ok' if ok else 'error'}")

        if not ok:
            with _analysis_lock:
                _analysis_state["queued"].clear()


def _start_worker(items):
    """Enqueue items and start worker if not already running."""
    with _analysis_lock:
        if _analysis_state["running"]:
            # Add to existing queue
            _analysis_state["queued"].extend(items)
            _analysis_state["total"] += len(items)
            return
        _analysis_state["running"] = True
        _analysis_state["queued"] = list(items)
        _analysis_state["completed"] = 0
        _analysis_state["total"] = len(items)
    # Drain any stale messages
    while not progress_queue.empty():
        try:
            progress_queue.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_worker_loop, daemon=True).start()


@analyzer_bp.route("/analyze/run/<int:video_id>", methods=["POST"])
def run_analysis(video_id):
    """Queue a single video for analysis.

    Body (all optional):
      force            — re-run even if already analyzed
      mode             — 'visual' | 'speech' override for THIS video
      whisper_model    — per-video whisper model override
      whisper_language — per-video language override
      whisper_translate— per-video translate-to-English override
    """
    video = get_video_by_id(video_id)
    if not video:
        return jsonify({"error": "Video not found"}), 404
    data = request.json or {}
    force = bool(data.get("force", False))

    overrides = {}
    mode = (data.get("mode") or "").strip().lower()
    if mode in ("visual", "speech"):
        overrides["mode"] = mode
    if data.get("whisper_model"):
        overrides["whisper_model"] = data["whisper_model"].strip()
    if "whisper_language" in data:
        overrides["whisper_language"] = (data.get("whisper_language") or "").strip()
    if "whisper_translate" in data:
        overrides["whisper_translate"] = bool(data["whisper_translate"])

    item = {"id": video_id, "force": force}
    if overrides:
        item["overrides"] = overrides
    _start_worker([item])
    return jsonify({"status": "started"})


@analyzer_bp.route("/analyze/run-all", methods=["POST"])
def run_all_analysis():
    """Queue all pending videos for analysis."""
    videos = get_all_videos()
    items = []
    for v in videos:
        analyzed_tags = get_analyzed_tags(v["id"])
        new_tags = ALL_TAG_SET - analyzed_tags
        if not v["analyzed_at"] or new_tags:
            force = bool(v["analyzed_at"] and not new_tags)
            items.append({"id": v["id"], "force": force})
    if not items:
        return jsonify({"status": "nothing", "count": 0})
    _start_worker(items)
    return jsonify({"status": "started", "count": len(items)})


@analyzer_bp.route("/analyze/auto-hide", methods=["POST"])
def auto_hide():
    """Run both auto-hide passes: low-quality (already applied at analysis
    time, but idempotent) + vote-learning."""
    lq = auto_hide_low_quality_scenes()
    votes = auto_hide_from_votes()
    return jsonify({
        "low_quality_hidden": lq,
        "vote_learning": votes,
        "total_hidden": lq + votes["hidden"],
    })


# ── Import from social channels ────────────────────────────────────────────

_import_state = {}  # external_id -> {running, log, done, ok, video_id}


@analyzer_bp.route("/analyze/imports")
def analyze_imports_list():
    """List videos available for import from each configured social channel,
    minus the ones already imported into the active brand profile."""
    import app_config
    import external_videos as ev

    socials = (app_config.get_config().get("socials") or {})
    out = {"platforms": {}, "errors": {}, "warnings": {},
           "error_details": {}}
    for platform in ("youtube", "tiktok", "instagram"):
        slot = socials.get(platform) or {}
        handle = slot.get("handle") or ""
        url = slot.get("url") or ""
        cookies = slot.get("cookies") or ""
        if not handle and not url:
            continue
        try:
            entries = ev.list_channel_videos(platform, handle, url, limit=40)
        except ev.ExternalListError as e:
            out["errors"][platform] = str(e)
            if getattr(e, "detail", None):
                out["error_details"][platform] = e.detail
            continue
        already = get_imported_external_ids(platform)
        items = []
        for ent in entries:
            ent_out = dict(ent)
            ent_out["imported"] = ent["id"] in already
            items.append(ent_out)
        out["platforms"][platform] = items
        # YouTube + Instagram practically require cookies to actually
        # download anything; warn the user up front so they don't burn time
        # trying anonymous imports.
        if platform in ("youtube", "instagram") and not cookies:
            out["warnings"][platform] = (
                f"{platform.capitalize()} downloads usually need cookies "
                f"now. In /settings, set this platform's Cookies field to "
                f"`chrome`, `firefox`, `brave`, or a cookies.txt path."
            )
    return jsonify(out)


def _import_worker(platform, external_id, page_url, title):
    state = _import_state[external_id] = {
        "running": True, "log": [], "done": False, "ok": False,
        "video_id": None,
        "error": None,         # one-line summary surfaced on the import card
        "error_detail": None,  # full multi-line detail for the log footer
    }

    def log(msg):
        state["log"].append(msg)
        emit_progress(f"[import] {msg}")

    try:
        import app_config
        import external_videos as ev
        dest = app_config.get_source_dir()
        log(f"Downloading from {platform}...")
        local = ev.download_video(platform, page_url or external_id, dest,
                                   on_log=log)
        log(f"Downloaded → {local.name}")

        # Register in DB
        from db import register_video as _rv
        video_id = _rv(local)
        state["video_id"] = video_id
        record_imported_external(
            platform, external_id, title=title,
            page_url=page_url, local_path=str(local), video_id=video_id,
        )
        log(f"Registered as video #{video_id}")

        # Queue for analysis using the existing single-video worker.
        _start_worker([{"id": video_id, "force": False}])
        log("Queued for analysis.")
        state["ok"] = True
    except ev.ExternalDownloadError as e:
        state["error"] = str(e)
        state["error_detail"] = getattr(e, "detail", None) or str(e)
        log(f"FAILED: {e}")
    except Exception as e:
        import traceback
        state["error"] = str(e)
        state["error_detail"] = traceback.format_exc()
        log(f"FAILED: {e}")
    finally:
        state["running"] = False
        state["done"] = True


@analyzer_bp.route("/analyze/imports/<platform>/<path:external_id>",
                   methods=["POST"])
def analyze_imports_one(platform, external_id):
    if platform not in ("youtube", "tiktok", "instagram"):
        return jsonify({"ok": False, "error": "Unknown platform"}), 400
    cur = _import_state.get(external_id)
    if cur and cur.get("running"):
        return jsonify({"ok": False, "error": "Already running"}), 409

    data = request.get_json(silent=True) or {}
    page_url = (data.get("page_url") or "").strip()
    title = (data.get("title") or "").strip()

    import threading
    t = threading.Thread(
        target=_import_worker,
        args=(platform, external_id, page_url, title),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@analyzer_bp.route("/analyze/imports/status/<path:external_id>")
def analyze_imports_status(external_id):
    return jsonify(_import_state.get(external_id,
                                     {"running": False, "done": False}))


_LANG_NAMES = {
    "en": "English", "ru": "Russian", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "tr": "Turkish", "pl": "Polish", "uk": "Ukrainian", "nl": "Dutch",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
}


def _lang_label(code, is_translation):
    code = (code or "").lower()
    name = _LANG_NAMES.get(code, code.upper() if code else "Unknown")
    if is_translation:
        return f"English (translated from {name})"
    return name


@analyzer_bp.route("/analyze/api/video/<int:video_id>/transcript")
def api_video_transcript(video_id):
    """Return full-video transcript groups for the Analyze-page modal."""
    v = get_video_by_id(video_id)
    if not v:
        return jsonify({"error": "video not found"}), 404
    groups = db.get_video_transcripts(video_id)
    return jsonify({
        "video_id": video_id,
        "filename": v["filename"],
        "duration": round(v["duration"], 1) if "duration" in v.keys() else 0,
        "groups":   [{
            "language":       g["language"],
            "is_translation": g["is_translation"],
            "label":          _lang_label(g["language"], g["is_translation"]),
            "segments":       g["segments"],
        } for g in groups],
    })


@analyzer_bp.route("/analyze/api/scene-from-selection", methods=["POST"])
def api_scene_from_selection():
    """Create a new scene spanning [start, end] for *video_id* from a
    transcript text selection on the Analyze page. The selected text is
    saved as a moment so it shows up alongside the new scene."""
    data = request.json or {}
    try:
        video_id = int(data.get("video_id"))
        start    = float(data.get("start"))
        end      = float(data.get("end"))
    except (TypeError, ValueError):
        return jsonify({"error": "video_id, start, end required"}), 400
    if end <= start:
        return jsonify({"error": "end must be greater than start"}), 400
    text = (data.get("text") or "").strip()
    v = get_video_by_id(video_id)
    if not v:
        return jsonify({"error": "video not found"}), 404
    # Clamp to the video's duration so we don't write nonsense.
    dur = v["duration"] if "duration" in v.keys() else 0
    if dur and end > dur:
        end = dur
    scene_id = db.create_scene(video_id, start, end, tags=["custom"])
    if text:
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO moments (video_id, at_time, note, dialog) "
                "VALUES (?, ?, ?, ?)",
                (video_id, start, text[:80], text),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    # Enrich with the source video's filename + wide flag so the Builder can
    # append this scene to its timeline without a second round-trip.
    vk = v.keys()
    return jsonify({
        "scene_id": scene_id,
        "start":    start,
        "end":      end,
        "duration": round(end - start, 2),
        "wide":     bool(v["wide"]) if "wide" in vk else False,
        "filename": v["filename"] if "filename" in vk else "",
        "video_file": v["path"] if "path" in vk else "",
    })


@analyzer_bp.route("/analyze/api/scene/<int:scene_id>", methods=["DELETE"])
def api_delete_scene(scene_id):
    """Discard a scene. Used by the Cut Scene confirmation modal so the
    user can drop an accidental cut without leaving it in the Scenes page."""
    db.delete_scene(scene_id)
    return jsonify({"ok": True, "scene_id": scene_id})


@analyzer_bp.route("/analyze/state")
def analyze_state():
    """Return current analysis state (for reconnecting after navigation)."""
    return jsonify(_get_status_snapshot())


@analyzer_bp.route("/analyze/status")
def analyze_status():
    """SSE endpoint for progress updates."""
    def generate():
        while True:
            try:
                msg = progress_queue.get(timeout=30)
                yield f"data: {json.dumps({'message': msg})}\n\n"
                if msg == "QUEUE:done":
                    break
            except queue.Empty:
                # Send heartbeat and check if still running
                snap = _get_status_snapshot()
                if not snap["running"]:
                    yield f"data: {json.dumps({'message': 'QUEUE:done'})}\n\n"
                    break
                yield f"data: {json.dumps({'message': 'waiting...'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ── HTML ─────────────────────────────────────────────────────────────────────

ANALYZE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClipBuilder - Video Analysis</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  padding:20px;
}
header{
  display:flex;align-items:center;gap:16px;margin-bottom:24px;
  background:#141414;border-bottom:1px solid #2a2a2a;
  padding:10px 20px;margin:-20px -20px 24px;
}
header h1{font-size:18px;font-weight:600;color:#fff;white-space:nowrap}
header h1 span{color:#e53935}
nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
  border:1px solid #444;border-radius:6px}
nav a:hover{color:#fff;border-color:#888}
nav a.active{color:#e53935;border-color:#e53935}
button{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 16px;font-size:13px;cursor:pointer;
}
button:hover{border-color:#666}
button.primary{background:#e53935;color:#fff;border-color:#e53935}
button.primary:hover{background:#c62828}
button:disabled{opacity:.5;cursor:not-allowed}
.controls{display:flex;gap:12px;margin-bottom:20px;align-items:center}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #222}
th{color:#888;font-size:12px;font-weight:600;text-transform:uppercase}
td{font-size:13px}
.status-badge{
  display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;
}
.status-done{background:#1b5e20;color:#a5d6a7}
.status-partial{background:#e65100;color:#ffcc80}
.status-new{background:#333;color:#888}
.status-analyzing{background:#1565c0;color:#90caf9}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.status-analyzing{animation:pulse 1.5s ease-in-out infinite}

/* -- Per-mode status badges (Visual / Audio) -- */
.status-stack{display:flex;flex-direction:column;gap:4px;align-items:flex-start}
.status-line{display:inline-flex;align-items:center;gap:6px}
.status-line .status-prov{display:inline-flex;align-items:center}
.status-stack .status-badge{
  display:inline-flex;align-items:center;gap:5px;
  padding:2px 8px 2px 6px;
}
.status-stack .status-badge svg{
  width:11px;height:11px;flex-shrink:0;fill:currentColor;
}
.status-stack .status-badge.status-new{
  background:transparent;border:1px dashed #333;color:#666;
}
.progress{
  margin-top:20px;padding:16px;background:#1a1a1a;border-radius:8px;
  font-size:13px;max-height:300px;overflow-y:auto;display:none;
}
.progress.active{display:block}
.progress .line{padding:2px 0;color:#aaa}
.progress .line.error{color:#ef5350}
.progress .line.done{color:#4caf50;font-weight:600}

/* Per-row Visual/Audio action buttons. Single cog at the far right opens
   a unified popover with both Visual + Audio settings — replaced the old
   per-mode cog buttons. */
.row-actions{display:flex;gap:6px;flex-wrap:nowrap;align-items:center}
.row-actions .ra-btn{
  padding:4px 10px;background:#1a1a1a;border:1px solid #333;color:#ddd;
  border-radius:4px;cursor:pointer;font-size:12px;
}
.row-actions .ra-btn:hover:not(:disabled){background:#252525;color:#fff;border-color:#555}
.row-actions .ra-cog{
  padding:4px 8px;background:#1a1a1a;border:1px solid #333;color:#888;
  border-radius:4px;cursor:pointer;font-size:14px;line-height:1;
}
.row-actions .ra-cog:hover:not(:disabled){color:#e53935;border-color:#e53935}
.row-actions .ra-cog-end{margin-left:auto}
.row-actions button:disabled{opacity:0.4;cursor:not-allowed}

/* Per-video analyze options popover */
.aopts{
  position:fixed;z-index:9000;display:none;
  background:#15151c;border:1px solid #2e2e3e;border-radius:8px;
  padding:12px 14px;width:360px;font-size:12px;color:#ddd;
  box-shadow:0 12px 32px rgba(0,0,0,.6);max-height:90vh;overflow-y:auto;
}
.aopts.open{display:block}
.aopts h4{margin:0 0 6px 0;color:#fff;font-size:13px;font-weight:700}
.aopts .aopts-help{color:#888;font-size:11px;margin-bottom:10px;line-height:1.45}
.aopts label.field{
  display:block;font-size:10px;color:#888;font-weight:700;
  text-transform:uppercase;letter-spacing:1px;margin:8px 0 4px;
}
.aopts input[type=text],.aopts select{
  width:100%;padding:6px 8px;background:#0c0c14;color:#eee;
  border:1px solid #2e2e3e;border-radius:4px;font-size:12px;box-sizing:border-box;
}
.aopts label.tog{
  display:flex;align-items:center;gap:6px;font-size:12px;color:#ccc;
  margin-top:8px;cursor:pointer;
}
.aopts label.tog input{accent-color:#e53935}
.aopts .aopts-actions{display:flex;gap:8px;margin-top:12px;justify-content:flex-end}
.aopts .aopts-actions button{
  padding:6px 12px;border-radius:4px;font-size:12px;cursor:pointer;
  background:#1a1a1a;border:1px solid #333;color:#ccc;
}
.aopts .aopts-actions button.go{
  background:linear-gradient(135deg,#e53935,#c62828);border-color:#e53935;color:#fff;font-weight:600;
}
/* Sections inside the unified Visual + Audio settings popover. */
.aopts .aopts-section{
  border:1px solid #1f1f2c;border-radius:6px;
  padding:10px 12px;margin-top:10px;background:#0d0d14;
}
.aopts .aopts-section-title{
  font-size:10px;font-weight:800;color:#e53935;text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:6px;
}
.aopts .aopts-section-action{display:flex;justify-content:flex-end;margin-top:10px}
.aopts .aopts-section-action button.go{
  padding:6px 12px;border-radius:4px;font-size:12px;cursor:pointer;font-weight:600;
  background:linear-gradient(135deg,#e53935,#c62828);border:1px solid #e53935;color:#fff;
}

/* -- Per-row transcript button -- */
.row-actions .ra-tx{
  padding:4px 8px;background:#1a1a1a;border:1px solid #333;color:#aaa;
  border-radius:4px;cursor:pointer;font-size:13px;line-height:1;
  display:inline-flex;align-items:center;
}
.row-actions .ra-tx:hover:not(:disabled){
  color:#1976d2;border-color:#1976d2;background:#0d1f30;
}
.row-actions .ra-tx svg{width:14px;height:14px;fill:currentColor}

/* -- Video transcript modal (Analyze page) -- */
.vtx-overlay{
  display:none;position:fixed;inset:0;z-index:9100;
  background:rgba(0,0,0,.85);align-items:center;justify-content:center;
}
.vtx-overlay.active{display:flex}
.vtx-modal{
  background:#15151c;border:1px solid #2e2e3e;border-radius:12px;
  width:min(760px,94vw);max-height:88vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.7);overflow:hidden;
}
.vtx-head{
  display:flex;align-items:center;gap:12px;padding:14px 18px;
  border-bottom:1px solid #1e1e2a;
}
.vtx-head h3{font-size:14px;font-weight:600;color:#fff;flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0;}
.vtx-close{background:none;border:none;color:#888;font-size:24px;
  cursor:pointer;padding:0 4px;line-height:1}
.vtx-close:hover{color:#fff}
.vtx-help{
  padding:8px 18px;background:#0d1217;border-bottom:1px solid #1e1e2a;
  font-size:11px;color:#888;line-height:1.5;
}
.vtx-help b{color:#1976d2}
.vtx-body{padding:10px 18px 12px;overflow-y:auto;flex:1}
.vtx-group{margin-top:14px}
.vtx-group:first-child{margin-top:4px}
.vtx-group-label{
  font-size:10px;font-weight:700;color:#1976d2;text-transform:uppercase;
  letter-spacing:.5px;margin-bottom:8px;position:sticky;top:0;
  background:#15151c;padding:4px 0;
}
.vtx-group.is-xlat .vtx-group-label{color:#4caf50}
.vtx-seg{
  display:flex;gap:10px;padding:5px 0;font-size:13px;line-height:1.5;
  border-bottom:1px solid #1c1c24;
}
.vtx-seg:last-child{border-bottom:none}
.vtx-seg-time{
  color:#666;font-size:10px;font-family:'SF Mono',Menlo,monospace;
  flex-shrink:0;min-width:54px;padding-top:3px;user-select:none;
}
.vtx-seg-text{color:#ddd;flex:1}
.vtx-seg-text::selection{background:rgba(25,118,210,.45);color:#fff}
.vtx-foot{
  padding:10px 18px;border-top:1px solid #1e1e2a;
  display:flex;align-items:center;gap:12px;background:#0d1217;
}
.vtx-foot .vtx-sel-info{font-size:11px;color:#888;flex:1}
.vtx-foot .vtx-sel-info b{color:#fff}
.vtx-foot button{
  padding:7px 14px;font-size:12px;font-weight:600;
  border-radius:6px;cursor:pointer;border:1px solid #2e2e3e;
}
.vtx-foot .vtx-cut{
  background:linear-gradient(135deg,#1976d2,#1565c0);
  border-color:#1976d2;color:#fff;
}
.vtx-foot .vtx-cut:disabled{opacity:.4;cursor:not-allowed;background:#222}
.vtx-foot .vtx-add{
  background:linear-gradient(135deg,#43a047,#2e7d32);
  border-color:#2e7d32;color:#fff;
}
.vtx-foot .vtx-add:disabled{opacity:.4;cursor:not-allowed;background:#222}

/* -- Cut Scene confirmation modal -- */
.cut-overlay{
  display:none;position:fixed;inset:0;z-index:9200;
  background:rgba(0,0,0,.88);align-items:center;justify-content:center;
}
.cut-overlay.active{display:flex}
.cut-modal{
  background:#15151c;border:1px solid #2e2e3e;border-radius:12px;
  width:min(560px,94vw);max-height:90vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.7);overflow:hidden;
}
.cut-head{padding:14px 18px;border-bottom:1px solid #1e1e2a;
  display:flex;align-items:center;gap:12px}
.cut-head h3{font-size:14px;font-weight:600;color:#fff;flex:1;margin:0;
  display:flex;align-items:center;gap:8px}
.cut-head h3 .cut-tag{
  display:inline-block;font-size:9px;font-weight:800;letter-spacing:.5px;
  text-transform:uppercase;color:#fff;background:#1976d2;
  padding:2px 6px;border-radius:3px;
}
.cut-head .cut-range{font-size:11px;color:#888;font-family:'SF Mono',Menlo,monospace}
.cut-body{padding:14px 18px;display:flex;flex-direction:column;gap:10px;
  overflow-y:auto}
.cut-body video{width:100%;max-height:50vh;border-radius:6px;background:#000}
.cut-body .cut-text{
  font-size:12px;color:#bbb;line-height:1.5;
  background:#0d0d14;border:1px solid #1e1e2a;border-radius:6px;
  padding:8px 10px;max-height:90px;overflow-y:auto;
}
.cut-body .cut-text:empty{display:none}
.cut-foot{padding:12px 18px;border-top:1px solid #1e1e2a;
  display:flex;align-items:center;gap:12px;background:#0d1217;}
.cut-foot .cut-status{flex:1;font-size:11px;color:#888}
.cut-foot button{
  padding:8px 16px;font-size:12px;font-weight:600;border-radius:6px;
  cursor:pointer;border:1px solid #2e2e3e;
}
.cut-foot .cut-discard{background:#1a1a1a;color:#ef5350;border-color:#3a1a1a}
.cut-foot .cut-discard:hover:not(:disabled){background:#3a1414;border-color:#ef5350}
.cut-foot .cut-keep{
  background:linear-gradient(135deg,#22c55e,#16a34a);
  border-color:#22c55e;color:#fff;
}
.cut-foot .cut-keep:hover:not(:disabled){filter:brightness(1.1)}
.cut-foot button:disabled{opacity:.45;cursor:not-allowed}

/* -- Import-from-socials modal -- */
.imp-overlay{
  display:none;position:fixed;inset:0;z-index:9000;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center;
  padding:20px;
}
.imp-overlay.active{display:flex}
.imp-modal{
  background:#0f0f14;border:1px solid #2e2e3e;border-radius:12px;
  width:900px;max-width:100%;max-height:88vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.7);overflow:hidden;
}
.imp-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;border-bottom:1px solid #1e1e2a;
}
.imp-header h2{font-size:18px;color:#fff;font-weight:700;margin:0}
.imp-close{
  background:none;border:none;color:#888;font-size:26px;
  line-height:1;cursor:pointer;padding:0 6px;
}
.imp-close:hover{color:#fff}
.imp-help{padding:0 20px;color:#888;font-size:12px;margin:10px 0}
.imp-status{padding:0 20px;font-size:12px;min-height:18px;color:#888}
.imp-status.err{color:#ef4444}
.imp-status.ok{color:#22c55e}
#imp-platforms{padding:8px 20px 20px;overflow-y:auto;flex:1}
.imp-platform{margin-bottom:18px}
.imp-platform-title{
  display:flex;align-items:center;gap:8px;
  font-size:11px;font-weight:800;color:#aaa;letter-spacing:1.2px;
  text-transform:uppercase;margin-bottom:8px;
}
.imp-platform-title .imp-count{
  background:#2a2a36;color:#888;padding:1px 8px;border-radius:99px;
  font-size:9px;font-weight:700;letter-spacing:.5px;
}
.imp-grid{
  display:grid;gap:10px;
  grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
}
.imp-card{
  background:#15151c;border:1px solid #1e1e2a;border-radius:8px;
  overflow:hidden;display:flex;flex-direction:column;font-size:11px;
}
.imp-card .imp-thumb{
  position:relative;aspect-ratio:9/16;background:#000;
  overflow:hidden;
}
.imp-card .imp-thumb img{
  width:100%;height:100%;object-fit:cover;display:block;
}
.imp-card .imp-thumb .imp-dur{
  position:absolute;bottom:5px;right:5px;
  background:rgba(0,0,0,.75);color:#fff;font-size:10px;font-weight:600;
  padding:2px 6px;border-radius:3px;
}
.imp-card .imp-meta{padding:8px;display:flex;flex-direction:column;gap:6px;flex:1}
.imp-card .imp-title{
  color:#eee;font-weight:600;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;
}
.imp-card .imp-uploader{color:#777;font-size:10px}
.imp-card .imp-action{
  margin-top:auto;padding:6px 10px;background:#1a1a24;border:1px solid #2e2e3e;
  color:#ccc;border-radius:5px;font-weight:600;font-size:11px;cursor:pointer;
}
.imp-card .imp-action:hover{background:#22222e;border-color:#444;color:#fff}
.imp-card .imp-action.primary{
  background:linear-gradient(135deg,#e53935,#c62828);
  border-color:#e53935;color:#fff;
}
.imp-card .imp-action.primary:hover{filter:brightness(1.1)}
.imp-card .imp-action[disabled]{opacity:.6;cursor:not-allowed}
.imp-card .imp-action.busy{
  background:#3a2a1a;border-color:#a06a00;color:#ffc060;
}
.imp-card .imp-action.imported{
  background:#1a3a1a;border-color:#2e6b2e;color:#9be09b;
}
.imp-empty{color:#666;font-size:12px;padding:20px;text-align:center}
.imp-error{color:#ef4444;font-size:11px;padding:8px 12px;
  background:rgba(239,68,68,0.08);border-radius:6px;margin-bottom:8px}

/* Inline failure block on import cards */
.imp-fail{
  margin-top:6px;padding:6px 8px;border-radius:5px;
  background:rgba(239,68,68,0.10);border:1px solid rgba(239,68,68,0.3);
}
.imp-fail .imp-fail-msg{
  color:#ffb0b0;font-size:10px;line-height:1.45;
  max-height:88px;overflow-y:auto;
}
.imp-fail .imp-fail-actions{display:flex;gap:6px;margin-top:6px}
.imp-fail .imp-fail-link{
  color:#fff;background:#7c3aed;text-decoration:none;
  padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;
}
.imp-fail .imp-fail-link:hover{background:#9333ea}
.imp-fail .imp-fail-copy{
  background:#1a1a1a;border:1px solid #444;color:#aaa;
  padding:3px 8px;border-radius:4px;font-size:10px;cursor:pointer;
}
.imp-fail .imp-fail-copy:hover{color:#fff;border-color:#666}
.imp-warn{color:#d4a017;font-size:11px;padding:8px 12px;
  background:rgba(212,160,23,0.08);border:1px solid rgba(212,160,23,0.3);
  border-radius:6px;margin-bottom:8px}
.imp-warn b{color:#ffd060}
.imp-warn a{font-weight:600}
</style>
</head>
<body>

<!-- pg-chrome -->

<div class="controls">
  <button class="primary" onclick="scanVideos()">Scan for New Videos</button>
  <button onclick="openImports()" id="imports-btn"
          title="Browse videos from your IG/TikTok/YouTube channels">
    Import Videos
  </button>
  <button onclick="pullFromDrive()" id="drive-pull-btn" style="display:none">Pull from Drive</button>
  <button onclick="autoHide()" id="auto-hide-btn"
          title="Hide low-quality scenes and ones similar to your down-voted scenes">
    Auto Hide
  </button>
  <button class="primary" id="analyze-all-btn" onclick="analyzeAll()" style="display:none">Analyze All</button>
  <span id="scan-status" style="font-size:13px;color:#888"></span>
</div>

<!-- Per-video analyze options popover -->
<div id="aopts" class="aopts"></div>

<!-- Import-from-socials modal -->
<div id="imp-overlay" class="imp-overlay" onclick="if(event.target===this)closeImports()">
  <div class="imp-modal">
    <div class="imp-header">
      <h2>Import Videos</h2>
      <button class="imp-close" onclick="closeImports()" title="Close">&times;</button>
    </div>
    <p class="imp-help">
      Lists videos from the IG / TikTok / YouTube channels in your Settings.
      Already-imported videos are hidden. Click <b>Import</b> to download +
      auto-analyze.
    </p>
    <div id="imp-status" class="imp-status muted"></div>
    <div id="imp-platforms"></div>
  </div>
</div>

<div id="vtx-overlay" class="vtx-overlay">
  <div class="vtx-modal">
    <div class="vtx-head">
      <h3 id="vtx-title">Transcript</h3>
      <button class="vtx-close" onclick="closeVideoTranscript()">&times;</button>
    </div>
    <div class="vtx-help">
      Tip: <b>highlight any text</b> in the transcript, then click <b>Cut Scene</b>
      to create a new scene spanning the selection's timestamps.
    </div>
    <div id="vtx-body" class="vtx-body"></div>
    <div class="vtx-foot">
      <span class="vtx-sel-info" id="vtx-sel-info">Select text to enable Cut Scene.</span>
      <button class="vtx-cut" id="vtx-cut-btn" disabled onclick="cutSceneFromSelection()">Cut Scene</button>
      <button class="vtx-add" id="vtx-add-btn" disabled onclick="addSelectionToBuilder()">Add to Builder</button>
    </div>
  </div>
</div>

<div id="cut-overlay" class="cut-overlay">
  <div class="cut-modal">
    <div class="cut-head">
      <h3>New scene <span class="cut-tag">custom</span></h3>
      <span class="cut-range" id="cut-range"></span>
    </div>
    <div class="cut-body">
      <video id="cut-video" controls preload="auto"></video>
      <div class="cut-text" id="cut-text"></div>
    </div>
    <div class="cut-foot">
      <span class="cut-status" id="cut-status"></span>
      <button class="cut-discard" id="cut-discard" onclick="cutDiscard()">Discard</button>
      <button class="cut-keep" id="cut-keep" onclick="cutKeep()">Keep</button>
    </div>
  </div>
</div>
<script>
(function(){
  if (window.PG_FEATURES && window.PG_FEATURES.drive) {
    var b = document.getElementById('drive-pull-btn');
    if (b) b.style.display = '';
  }
})();
</script>

<table>
  <thead>
    <tr>
      <th>Filename</th>
      <th>Duration</th>
      <th>Size</th>
      <th>Status</th>
      <th>Action</th>
    </tr>
  </thead>
  <tbody id="video-list"></tbody>
</table>

<div class="progress" id="progress">
  <div id="progress-lines"></div>
</div>

<script>
var videos = [];
var analyzing = false;
var analyzingVideoId = null;
var analyzingMode = null;       // 'visual' | 'speech' | null
var evtSource = null;

async function scanVideos() {
  document.getElementById('scan-status').textContent = 'Scanning...';
  try {
    var res = await fetch('/analyze/scan', {method:'POST'});
    var data = await res.json();
    videos = data.videos;
    document.getElementById('scan-status').textContent =
      'Found ' + data.registered + ' video files';
    renderList();
  } catch(e) {
    document.getElementById('scan-status').textContent = 'Scan failed: ' + e.message;
  }
}

/* ── Import from social channels ──────────────────────────────────── */

var IMP_PLATFORM_LABEL = {
  youtube:   'YouTube',
  tiktok:    'TikTok',
  instagram: 'Instagram',
};

function openImports(){
  document.getElementById('imp-overlay').classList.add('active');
  document.getElementById('imp-platforms').innerHTML =
    '<div class="imp-empty">Querying your channels…</div>';
  setImpStatus('', '');
  loadImports();
}

function closeImports(){
  document.getElementById('imp-overlay').classList.remove('active');
}

function setImpStatus(text, cls){
  var el = document.getElementById('imp-status');
  el.textContent = text || '';
  el.className = 'imp-status' + (cls ? ' ' + cls : '');
  // Mirror modal status into the global log footer so it survives the
  // modal closing and is available for triage.
  if (text && window.pgLog) {
    var lg = (cls === 'err') ? 'error' : (cls === 'ok' ? 'ok' : '');
    window.pgLog('[import] ' + text, lg);
  }
}

function _attachImpFailureBlock(card, item, errMsg) {
  // Replace any previous failure block on this card.
  var old = card.querySelector('.imp-fail');
  if (old) old.remove();
  if (!errMsg) return;
  var meta = card.querySelector('.imp-meta');
  if (!meta) return;
  var div = document.createElement('div');
  div.className = 'imp-fail';
  var safeMsg = errMsg.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  var safeUrl = (item.page_url || '').replace(/"/g,'&quot;');
  var open = '';
  if (item.page_url) {
    open = '<div class="imp-fail-actions">'
         +   '<a href="' + safeUrl + '" target="_blank" rel="noopener" class="imp-fail-link">'
         +     'Open in browser ↗'
         +   '</a>'
         +   '<button class="imp-fail-copy" type="button">Copy URL</button>'
         + '</div>';
  }
  div.innerHTML = '<div class="imp-fail-msg">' + safeMsg + '</div>' + open;
  meta.appendChild(div);
  var copy = div.querySelector('.imp-fail-copy');
  if (copy) {
    copy.addEventListener('click', function(e) {
      e.stopPropagation();
      navigator.clipboard.writeText(item.page_url).then(function() {
        copy.textContent = 'Copied ✓';
        setTimeout(function(){ copy.textContent = 'Copy URL'; }, 1500);
      });
    });
  }
}

// Multi-line dump helper — splits a long detail block into footer lines
// tagged "error" so the user can scroll the footer log to triage.
function _logDetailLines(prefix, detail){
  if (!detail || !window.pgLog) return;
  var lines = String(detail).split(/\\r?\\n/);
  for (var i = 0; i < lines.length; i++) {
    var ln = lines[i].replace(/\s+$/, '');
    if (ln.length === 0) continue;
    window.pgLog(prefix + ' ' + ln, 'error');
  }
}

function escImp(s){
  return (s == null ? '' : String(s))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function fmtDur(secs){
  secs = Math.round(secs || 0);
  if (!secs) return '';
  var m = Math.floor(secs / 60), s = secs % 60;
  return m + ':' + (s < 10 ? '0' : '') + s;
}

async function loadImports(){
  try {
    var r = await fetch('/analyze/imports');
    var d = await r.json();
    var root = document.getElementById('imp-platforms');
    root.innerHTML = '';

    var keys = Object.keys(d.platforms || {});
    var errors = d.errors || {};
    var warnings = d.warnings || {};
    if (!keys.length && !Object.keys(errors).length) {
      root.innerHTML = '<div class="imp-empty">'
        + 'No social channels configured. Open <a href="/settings" style="color:#ff5252">Settings</a> '
        + 'and fill in at least an IG, TikTok, or YouTube handle.</div>';
      return;
    }

    // Render error banners first, and tee the full detail into the footer.
    for (var ek in errors) {
      var div = document.createElement('div');
      div.className = 'imp-error';
      div.textContent = ek + ': ' + errors[ek];
      root.appendChild(div);
      if (window.pgLog) {
        window.pgLog('[import] ' + ek + ' listing failed: ' + errors[ek], 'error');
        var details = (d.error_details || {})[ek];
        if (details) _logDetailLines('[import:' + ek + ']', details);
      }
    }
    // Then yellow warnings (e.g. missing cookies on platforms that need them).
    for (var wk in warnings) {
      var wdiv = document.createElement('div');
      wdiv.className = 'imp-warn';
      wdiv.innerHTML = '<b>' + wk + ':</b> ' + warnings[wk]
        + ' <a href="/settings" style="color:#ffd060">Open settings</a>';
      root.appendChild(wdiv);
    }

    for (var i = 0; i < keys.length; i++) {
      var platform = keys[i];
      var items = d.platforms[platform] || [];
      var available = items.filter(function(x){ return !x.imported; });
      var section = document.createElement('div');
      section.className = 'imp-platform';
      section.innerHTML =
        '<div class="imp-platform-title">'
        + escImp(IMP_PLATFORM_LABEL[platform] || platform)
        + '<span class="imp-count">' + available.length + ' available · '
        + items.length + ' total</span>'
        + '</div>'
        + '<div class="imp-grid" id="imp-grid-' + platform + '"></div>';
      root.appendChild(section);

      var grid = section.querySelector('.imp-grid');
      if (!available.length) {
        grid.innerHTML = '<div class="imp-empty">No new videos to import.</div>';
        continue;
      }
      for (var j = 0; j < available.length; j++) {
        grid.appendChild(buildImpCard(platform, available[j]));
      }
    }
  } catch (e) {
    setImpStatus('Failed to query channels: ' + e.message, 'err');
  }
}

function buildImpCard(platform, item){
  var card = document.createElement('div');
  card.className = 'imp-card';
  card.dataset.id = item.id;
  card.dataset.platform = platform;
  var thumb = item.thumbnail
    ? '<img src="' + escImp(item.thumbnail) + '" alt="" loading="lazy"/>'
    : '<div style="background:#0a0a0a;width:100%;height:100%"></div>';
  var dur = item.duration ? '<div class="imp-dur">' + fmtDur(item.duration) + '</div>' : '';
  card.innerHTML =
    '<div class="imp-thumb">' + thumb + dur + '</div>'
    + '<div class="imp-meta">'
    +   '<div class="imp-title">' + escImp(item.title) + '</div>'
    +   (item.uploader ? '<div class="imp-uploader">' + escImp(item.uploader) + '</div>' : '')
    +   '<button class="imp-action primary" data-act="import">Import</button>'
    + '</div>';
  card.querySelector('[data-act="import"]').addEventListener('click', function(){
    importOne(platform, item, card);
  });
  return card;
}

async function importOne(platform, item, card){
  var btn = card.querySelector('.imp-action');
  btn.disabled = true;
  btn.classList.remove('primary');
  btn.classList.add('busy');
  btn.textContent = 'Starting…';
  if (window.pgLog) {
    window.pgLog('[import:' + platform + '] starting → ' + (item.title || item.id));
  }
  try {
    var r = await fetch(
      '/analyze/imports/' + platform + '/' + encodeURIComponent(item.id),
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify({page_url: item.page_url, title: item.title})}
    );
    if (r.status === 409) {
      btn.textContent = 'Already in progress';
      if (window.pgLog) window.pgLog('[import:' + platform + '] already running', 'error');
      return;
    }
    var d = await r.json();
    if (!d.ok) { throw new Error(d.error || 'unknown'); }
    pollImport(platform, item, card, btn);
  } catch (e) {
    btn.classList.remove('busy');
    btn.textContent = 'Failed: ' + e.message;
    if (window.pgLog) window.pgLog('[import:' + platform + '] kickoff failed: ' + e.message, 'error');
  }
}

function pollImport(platform, item, card, btn){
  var url = '/analyze/imports/status/' + encodeURIComponent(item.id);
  var seenLines = 0;
  var prefix = '[import:' + platform + ']';
  var poll = async function(){
    try {
      var s = await (await fetch(url)).json();
      if (s.log && s.log.length) {
        btn.textContent = s.log[s.log.length - 1];
        // Push only the lines we haven't already logged into the footer.
        for (var i = seenLines; i < s.log.length; i++) {
          var line = s.log[i] || '';
          var cls = /^FAILED|error|⚠/i.test(line) ? 'error' : '';
          if (window.pgLog) window.pgLog(prefix + ' ' + line, cls);
        }
        seenLines = s.log.length;
      }
      if (!s.done) { setTimeout(poll, 800); return; }
      if (s.ok) {
        btn.classList.remove('busy');
        btn.classList.add('imported');
        btn.textContent = '✓ Imported (analyzing…)';
        if (window.pgLog) window.pgLog(prefix + ' ' + (item.title || item.id) + ' → done', 'ok');
        if (typeof scanVideos === 'function') scanVideos();
      } else {
        btn.classList.remove('busy');
        btn.classList.add('primary');
        btn.disabled = false;
        btn.textContent = 'Retry';
        if (window.pgLog && s.error) {
          window.pgLog(prefix + ' ' + (item.title || item.id) + ' → ' + s.error, 'error');
        }
        // Detailed multi-line traceback / yt-dlp output for triage.
        _logDetailLines(prefix, s.error_detail);
        // Inline failure block on the card: full error text + "Open in
        // browser" link so the user can manually fetch the video.
        _attachImpFailureBlock(card, item, s.error);
      }
    } catch (e) {
      btn.classList.remove('busy');
      btn.classList.add('primary');
      btn.disabled = false;
      btn.textContent = 'Retry';
      if (window.pgLog) window.pgLog(prefix + ' poll error: ' + e.message, 'error');
    }
  };
  poll();
}

document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') closeImports();
});

async function autoHide() {
  var btn = document.getElementById('auto-hide-btn');
  var st = document.getElementById('scan-status');
  btn.disabled = true;
  st.textContent = 'Auto-hiding...';
  try {
    var r = await fetch('/analyze/auto-hide', {method:'POST'});
    var d = await r.json();
    var v = d.vote_learning || {};
    var blackTags = (v.blacklist_tags || []).map(function(b){
      return b.tag + '(' + Math.round(b.down_rate * 100) + '%)';
    }).join(', ');
    var msg = 'Hidden ' + d.total_hidden + ' scene(s)'
      + ' — ' + d.low_quality_hidden + ' low-quality, '
      + (v.hidden || 0) + ' from votes'
      + (v.scanned ? ' (scanned ' + v.scanned + ')' : '')
      + (blackTags ? '. Down-vote tags: ' + blackTags : '');
    st.textContent = msg;
    if (window.pgLog) window.pgLog('[auto-hide] ' + msg, 'ok');
  } catch (e) {
    st.textContent = 'Auto-hide failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function pullFromDrive() {
  var btn = document.getElementById('drive-pull-btn');
  var st = document.getElementById('scan-status');
  // Pre-flight: check Drive is configured
  var sc = await (await fetch('/drive/api/status')).json();
  if (!sc.has_token) {
    if (confirm('Google Drive is not connected. Open Drive settings?')) {
      window.location = '/drive';
    }
    return;
  }
  if (!sc.inbox_folder_id) {
    if (confirm('No Drive inbox folder configured. Open Drive settings?')) {
      window.location = '/drive';
    }
    return;
  }
  btn.disabled = true;
  st.textContent = 'Pulling from Drive...';
  var r = await fetch('/drive/api/pull', {method:'POST'});
  if (r.status === 409) { st.textContent = 'Pull already running'; btn.disabled = false; return; }
  var poll = async function() {
    var s = await (await fetch('/drive/api/pull/status')).json();
    var last = (s.log && s.log.length) ? s.log[s.log.length - 1] : 'Working...';
    st.textContent = last;
    if (window.pgLog && s.log && s.log.length) { window.pgLog('[drive] ' + last); }
    if (!s.done) { setTimeout(poll, 800); return; }
    btn.disabled = false;
    if (s.ok) { await scanVideos(); } else { st.textContent = 'Pull failed: ' + last; }
  };
  poll();
}

// Inline SVG icons for the per-mode status badges. Visual = film/eye,
// Audio = speech bubble (matches the transcript icon language).
var VISUAL_ICON = '<svg viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.7 7.6 1 12c1.7 4.4 6 7.5 11 7.5s9.3-3.1 11-7.5c-1.7-4.4-6-7.5-11-7.5zm0 12.5a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>';
var AUDIO_ICON  = '<svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5.3-3a.7.7 0 0 0-.7.7 4.6 4.6 0 0 1-9.2 0 .7.7 0 1 0-1.4 0 6 6 0 0 0 5.3 6V21a.7.7 0 1 0 1.4 0v-2.3a6 6 0 0 0 5.3-6 .7.7 0 0 0-.7-.7z"/></svg>';

function _modeStatus(v, kind) {
  // kind: 'visual' | 'speech'
  var label = kind === 'speech' ? 'Audio' : 'Visual';
  var icon  = kind === 'speech' ? AUDIO_ICON : VISUAL_ICON;
  var prov  = kind === 'speech'
    ? (v.speech_analyzer_provider || v.analyzer_provider || '')
    : (v.visual_analyzer_provider || v.analyzer_provider || '');
  var done  = kind === 'speech'
    ? !!(v.speech_analyzed_at || v.has_transcript)
    : !!v.visual_analyzed_at;
  var cls, text;
  if (analyzing && analyzingVideoId === v.id && analyzingMode === kind) {
    cls = 'status-analyzing';
    text = label + ' analyzing\u2026';
  } else if (kind === 'speech') {
    if (done) { cls = 'status-done'; text = label + ' done'; }
    else      { cls = 'status-new';  text = label; }
  } else {
    if (!done) {
      cls = 'status-new';  text = label;
    } else if (v.needs_update) {
      cls = 'status-partial';
      text = label + ' ' + v.analyzed_tag_count + '/' + v.total_tag_count;
    } else {
      cls = 'status-done'; text = label + ' done';
    }
  }
  // Per-mode AI badge sits inline next to its status \u2014 visual and audio
  // can be tagged by different providers, so each row gets its own badge.
  var aiInline = (done && prov && window.pgAiBadge)
    ? ' <span class="status-prov" title="Tagged by ' + prov + '">'
      + window.pgAiBadge(prov, {size:13}) + '</span>'
    : '';
  return '<div class="status-line">'
    + '<span class="status-badge ' + cls + '">' + icon + text + '</span>'
    + aiInline
    + '</div>';
}

function renderList() {
  var tbody = document.getElementById('video-list');
  tbody.innerHTML = '';
  if (videos.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:36px 0;color:#666">'
      + 'No source videos yet. Click <b>Scan for New Videos</b> above '
      + 'or <b>Import Videos</b> to pull from social channels.</td></tr>';
    var pendBtn = document.getElementById('analyze-all-btn');
    if (pendBtn) pendBtn.style.display = 'none';
    return;
  }
  for (var i = 0; i < videos.length; i++) {
    var v = videos[i];
    var dims = v.width + 'x' + v.height;
    if (v.wide) dims += ' (wide)';
    var disabled = analyzing ? ' disabled' : '';
    var txBtn = v.has_transcript
      ? '<button class="ra-tx" onclick="openVideoTranscript(' + v.id + ')"'
        + ' title="View full transcript (click to read; select + Cut Scene to extract a moment)">'
        + '<svg viewBox="0 0 24 24"><path d="M4 4h16v2H4V4zm0 4h16v2H4V8zm0 4h10v2H4v-2zm0 4h16v2H4v-2zm0 4h10v2H4v-2z"/></svg>'
        + '</button>'
      : '';
    // Single cog at the far right opens a popover with both Visual and
    // Audio settings sections. Replaced the old per-mode cogs since the
    // user wanted one combined config surface.
    var actionCell =
        '<div class="row-actions">'
      +   '<button class="ra-btn"' + disabled
      +   ' onclick="analyzeVideoMode(' + v.id + ',\'visual\')"'
      +   ' title="Visual analysis (frame sampling)">Visual</button>'
      +   '<button class="ra-btn"' + disabled
      +   ' onclick="analyzeVideoMode(' + v.id + ',\'speech\')"'
      +   ' title="Speech analysis (Whisper transcript)">Audio</button>'
      +   txBtn
      +   '<button class="ra-cog ra-cog-end"' + disabled
      +   ' onclick="openAnalyzeOpts(event,' + v.id + ',\'both\')"'
      +   ' title="Per-video Visual + Audio settings">⚙</button>'
      + '</div>';
    tbody.innerHTML += '<tr>'
      + '<td>' + v.filename + '</td>'
      + '<td>' + v.duration + 's</td>'
      + '<td>' + dims + '</td>'
      + '<td><div class="status-stack">'
      +     _modeStatus(v, 'visual')
      +     _modeStatus(v, 'speech')
      + '</div></td>'
      + '<td>' + actionCell + '</td>'
      + '</tr>';
  }

  var pending = videos.filter(function(v) { return !v.analyzed_at || v.needs_update; });
  var allBtn = document.getElementById('analyze-all-btn');
  if (pending.length > 0 && !analyzing) {
    allBtn.style.display = '';
    allBtn.textContent = 'Analyze All (' + pending.length + ')';
    allBtn.disabled = false;
  } else if (analyzing) {
    allBtn.style.display = '';
    allBtn.disabled = true;
  } else {
    allBtn.style.display = 'none';
  }
}

function connectSSE() {
  if (evtSource) evtSource.close();
  var prog = document.getElementById('progress');
  prog.classList.add('active');

  evtSource = new EventSource('/analyze/status');
  evtSource.onmessage = function(e) {
    var data = JSON.parse(e.data);
    var msg = data.message;
    if (msg === 'QUEUE:done') {
      evtSource.close();
      evtSource = null;
      analyzing = false;
      analyzingVideoId = null;
      analyzingMode = null;
      addLine('All done!', 'done');
      scanVideos();
      return;
    }
    var m = msg.match(/^VIDEO:(\d+):(ok|error)$/);
    if (m) {
      var ok = m[2] === 'ok';
      addLine(ok ? 'Video complete' : 'Video failed', ok ? 'done' : 'error');
      scanVideos();
      return;
    }
    if (msg !== 'waiting...') addLine(msg);
  };
  evtSource.onerror = function() {
    evtSource.close();
    evtSource = null;
    // Don't mark as not-analyzing — server may still be running
    checkState();
  };
}

function analyzeVideo(videoId, force) {
  // Legacy single-button entry — preserved for any callers that still use
  // it (e.g. background watcher). Routes through the active profile mode.
  return analyzeVideoMode(videoId, null, {force: force});
}

function analyzeVideoMode(videoId, mode, opts) {
  if (analyzing) return;
  analyzing = true;
  analyzingVideoId = videoId;
  analyzingMode = mode || null;
  renderList();
  document.getElementById('progress-lines').innerHTML = '';
  var modeLabel = mode === 'speech' ? 'audio' : (mode === 'visual' ? 'visual' : 'profile-default');
  addLine('Starting ' + modeLabel + ' analysis...');

  var body = {force: true};
  if (mode) body.mode = mode;
  if (opts) {
    if ('force' in opts) body.force = !!opts.force;
    if (opts.whisper_model)    body.whisper_model    = opts.whisper_model;
    if ('whisper_language' in opts) body.whisper_language = opts.whisper_language;
    if ('whisper_translate' in opts) body.whisper_translate = !!opts.whisper_translate;
  }

  fetch('/analyze/run/' + videoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(function() {
    connectSSE();
  }).catch(function(e) {
    analyzing = false;
    analyzingVideoId = null;
    analyzingMode = null;
    addLine('Error: ' + e.message, 'error');
    renderList();
  });
}

/* ── Per-video options popover ───────────────────────────────────────── */

var _aoptsProfile = null;  // cached profile defaults

async function _aoptsLoadProfile() {
  if (_aoptsProfile) return _aoptsProfile;
  try {
    _aoptsProfile = await (await fetch('/settings/api/app')).json();
  } catch (e) {
    _aoptsProfile = {};
  }
  return _aoptsProfile;
}

async function openAnalyzeOpts(evt, videoId, _mode) {
  evt.stopPropagation();
  var prof = await _aoptsLoadProfile();
  var pop = document.getElementById('aopts');

  // Combined Visual + Audio settings popover. Each section has its own
  // Run button so the user can launch either pass with the per-video
  // overrides shown above it.
  var html = '<h4>Settings for this video</h4>'
    + '<div class="aopts-help">Overrides the brand profile defaults for '
    + 'this video only. Click <b>Run Visual</b> or <b>Run Audio</b> to '
    + 'launch with these settings.</div>'
    + '<label class="tog"><input type="checkbox" id="aopts-force" checked> '
    + 'Force re-analyze (clear previous results)</label>';

  // ── Visual section ──
  html += '<div class="aopts-section">'
    + '<div class="aopts-section-title">Visual</div>'
    + '<div class="aopts-help" style="margin:0">'
    + 'Frame-sampling is fixed for now — no tunables yet.</div>'
    + '<div class="aopts-section-action">'
    + '<button class="go" onclick="runAnalyzeOpts(' + videoId + ',\'visual\')">Run Visual</button>'
    + '</div>'
    + '</div>';

  // ── Audio section ──
  html += '<div class="aopts-section">'
    + '<div class="aopts-section-title">Audio (Speech)</div>'
    + '<label class="field">Whisper model</label>'
    + '<select id="aopts-model">'
    + '  <option value="tiny">tiny — 39 MB</option>'
    + '  <option value="base">base — 74 MB</option>'
    + '  <option value="small">small — 244 MB</option>'
    + '  <option value="medium">medium — 769 MB</option>'
    + '  <option value="large-v3">large-v3 — 1.5 GB (multilingual)</option>'
    + '</select>'
    + '<label class="field">Language (ISO, blank = auto)</label>'
    + '<input type="text" id="aopts-lang" placeholder="en, ru, es, …">'
    + '<label class="tog"><input type="checkbox" id="aopts-translate"> '
    + 'Translate transcript to English</label>'
    + '<div class="aopts-section-action">'
    + '<button class="go" onclick="runAnalyzeOpts(' + videoId + ',\'speech\')">Run Audio</button>'
    + '</div>'
    + '</div>';

  html += '<div class="aopts-actions">'
    +   '<button onclick="closeAnalyzeOpts()">Close</button>'
    + '</div>';
  pop.innerHTML = html;

  // Pre-fill audio defaults from profile.
  var m = document.getElementById('aopts-model');
  if (m) m.value = prof.whisper_model || 'base';
  var l = document.getElementById('aopts-lang');
  if (l) l.value = prof.whisper_language || '';
  var t = document.getElementById('aopts-translate');
  if (t) t.checked = !!prof.whisper_translate;

  // Anchor next to the clicked ⚙ button.
  pop.style.left = '0px';
  pop.style.top  = '0px';
  pop.classList.add('open');
  var br = evt.currentTarget.getBoundingClientRect();
  var pr = pop.getBoundingClientRect();
  var x = br.right + 8;
  var y = br.top;
  if (x + pr.width > window.innerWidth - 8) x = br.left - pr.width - 8;
  if (y + pr.height > window.innerHeight - 8) y = window.innerHeight - pr.height - 8;
  pop.style.left = Math.max(8, x) + 'px';
  pop.style.top  = Math.max(8, y) + 'px';
}

function closeAnalyzeOpts() {
  document.getElementById('aopts').classList.remove('open');
}

function runAnalyzeOpts(videoId, mode) {
  var opts = {force: !!document.getElementById('aopts-force').checked};
  if (mode === 'speech') {
    var m = document.getElementById('aopts-model');
    var l = document.getElementById('aopts-lang');
    var t = document.getElementById('aopts-translate');
    if (m) opts.whisper_model    = m.value;
    if (l) opts.whisper_language = l.value;
    if (t) opts.whisper_translate = t.checked;
  }
  closeAnalyzeOpts();
  analyzeVideoMode(videoId, mode, opts);
}

document.addEventListener('mousedown', function(e) {
  var pop = document.getElementById('aopts');
  if (pop && pop.classList.contains('open')
      && !pop.contains(e.target)
      && !(e.target.classList && e.target.classList.contains('ra-cog'))) {
    closeAnalyzeOpts();
  }
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeAnalyzeOpts();
    closeVideoTranscript();
    if (typeof closeCutConfirm === 'function') closeCutConfirm();
  }
});

// ── Per-video transcript modal ────────────────────────────────────────────
var _vtxState = { videoId: null, selectionStart: null, selectionEnd: null,
                  selectionText: '' };

function _vtxFmt(s) {
  var m = Math.floor(s / 60);
  var sec = (s - m * 60).toFixed(1);
  if (sec.length === 3) sec = '0' + sec;
  return m + ':' + sec;
}
function _vtxEsc(s) {
  return (s || '').replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

async function openVideoTranscript(videoId) {
  _vtxState.videoId = videoId;
  _vtxState.selectionStart = null;
  _vtxState.selectionEnd = null;
  _vtxState.selectionText = '';
  document.getElementById('vtx-cut-btn').disabled = true;
  document.getElementById('vtx-add-btn').disabled = true;
  document.getElementById('vtx-sel-info').innerHTML = 'Loading transcript...';
  var body = document.getElementById('vtx-body');
  body.innerHTML = '';
  document.getElementById('vtx-overlay').classList.add('active');
  try {
    var r = await fetch('/analyze/api/video/' + videoId + '/transcript');
    var data = await r.json();
    document.getElementById('vtx-title').textContent = data.filename || 'Transcript';
    if (!data.groups || data.groups.length === 0) {
      body.innerHTML = '<div style="color:#666;text-align:center;padding:24px">'
        + 'No transcript saved for this video.</div>';
      document.getElementById('vtx-sel-info').textContent = '';
      return;
    }
    var html = '';
    for (var i = 0; i < data.groups.length; i++) {
      var g = data.groups[i];
      html += '<div class="vtx-group' + (g.is_translation ? ' is-xlat' : '') + '">';
      html += '<div class="vtx-group-label">' + _vtxEsc(g.label) + '</div>';
      for (var j = 0; j < g.segments.length; j++) {
        var seg = g.segments[j];
        html += '<div class="vtx-seg">'
          + '<span class="vtx-seg-time">' + _vtxFmt(seg.start) + '</span>'
          + '<span class="vtx-seg-text" data-start="' + seg.start
                + '" data-end="' + seg.end + '">'
          + _vtxEsc(seg.text)
          + '</span>'
          + '</div>';
      }
      html += '</div>';
    }
    body.innerHTML = html;
    document.getElementById('vtx-sel-info').textContent = 'Select text to enable Cut Scene.';
  } catch (e) {
    body.innerHTML = '<div style="color:#ef5350;padding:16px">Failed to load transcript.</div>';
  }
}

function closeVideoTranscript() {
  document.getElementById('vtx-overlay').classList.remove('active');
}

// Walk a node up to find the closest .vtx-seg-text ancestor (or itself).
function _vtxFindSegText(node) {
  while (node && node !== document) {
    if (node.nodeType === 1 && node.classList && node.classList.contains('vtx-seg-text')) {
      return node;
    }
    node = node.parentNode;
  }
  return null;
}

document.addEventListener('selectionchange', function() {
  var overlay = document.getElementById('vtx-overlay');
  if (!overlay || !overlay.classList.contains('active')) return;
  var sel = window.getSelection();
  var btn = document.getElementById('vtx-cut-btn');
  var addBtn = document.getElementById('vtx-add-btn');
  var info = document.getElementById('vtx-sel-info');
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    btn.disabled = true;
    if (addBtn) addBtn.disabled = true;
    _vtxState.selectionStart = null;
    _vtxState.selectionEnd = null;
    _vtxState.selectionText = '';
    info.textContent = 'Select text to enable Cut Scene.';
    return;
  }
  var range = sel.getRangeAt(0);
  // Only react to selections inside the transcript body.
  var body = document.getElementById('vtx-body');
  if (!body.contains(range.commonAncestorContainer)
      && body !== range.commonAncestorContainer) {
    return;
  }
  var startSeg = _vtxFindSegText(range.startContainer);
  var endSeg   = _vtxFindSegText(range.endContainer);
  if (!startSeg || !endSeg) {
    btn.disabled = true;
    if (addBtn) addBtn.disabled = true;
    info.textContent = 'Select text inside a transcript line to enable Cut Scene.';
    return;
  }
  // Walk all .vtx-seg-text nodes and collect those between startSeg and endSeg
  // (inclusive) by document order — handles backwards selections too.
  var allSegs = body.querySelectorAll('.vtx-seg-text');
  var startIdx = -1, endIdx = -1;
  for (var i = 0; i < allSegs.length; i++) {
    if (allSegs[i] === startSeg) startIdx = i;
    if (allSegs[i] === endSeg) endIdx = i;
  }
  if (startIdx < 0 || endIdx < 0) return;
  if (startIdx > endIdx) { var t = startIdx; startIdx = endIdx; endIdx = t; }
  var minStart = Infinity, maxEnd = -Infinity;
  for (var k = startIdx; k <= endIdx; k++) {
    var s = parseFloat(allSegs[k].getAttribute('data-start'));
    var e = parseFloat(allSegs[k].getAttribute('data-end'));
    if (!isNaN(s) && s < minStart) minStart = s;
    if (!isNaN(e) && e > maxEnd) maxEnd = e;
  }
  if (!isFinite(minStart) || !isFinite(maxEnd) || maxEnd <= minStart) {
    btn.disabled = true;
    if (addBtn) addBtn.disabled = true;
    return;
  }
  _vtxState.selectionStart = minStart;
  _vtxState.selectionEnd   = maxEnd;
  _vtxState.selectionText  = sel.toString();
  btn.disabled = false;
  if (addBtn) addBtn.disabled = false;
  info.innerHTML = 'Cut from <b>' + _vtxFmt(minStart) + '</b> to <b>'
    + _vtxFmt(maxEnd) + '</b> (' + (maxEnd - minStart).toFixed(1) + 's)';
});

async function cutSceneFromSelection() {
  if (_vtxState.selectionStart == null || _vtxState.selectionEnd == null) return;
  var btn = document.getElementById('vtx-cut-btn');
  btn.disabled = true;
  btn.textContent = 'Cutting...';
  try {
    var r = await fetch('/analyze/api/scene-from-selection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        video_id: _vtxState.videoId,
        start:    _vtxState.selectionStart,
        end:      _vtxState.selectionEnd,
        text:     _vtxState.selectionText,
      }),
    });
    var data = await r.json();
    if (!r.ok) throw new Error(data.error || 'failed');
    document.getElementById('vtx-sel-info').innerHTML =
      'Created scene <b>#' + data.scene_id + '</b> ['
      + _vtxFmt(data.start) + ' – ' + _vtxFmt(data.end) + ']';
    openCutConfirm(data.scene_id, data.start, data.end,
                   _vtxState.selectionText);
  } catch (e) {
    document.getElementById('vtx-sel-info').textContent = 'Cut failed: ' + e.message;
  } finally {
    btn.textContent = 'Cut Scene';
    btn.disabled = false;
  }
}

// Cut the selected scene and append it to the end of Layer I of the
// builder's saved timeline. Stays on the current page — the change is
// written directly into the same localStorage key the builder restores
// from on next open.
var BUILDER_STATE_KEY = 'pg-builder-state-v1';

function _builderEmptySnapshot() {
  return {
    video_track: [],
    sound_track: [],
    text_overlays: [],
    track_settings: [
      {muted:false, default_position:'top',    captions:'none', default_crop_x_frac:null},
      {muted:false, default_position:'center', captions:'none', default_crop_x_frac:null},
      {muted:false, default_position:'bottom', captions:'none', default_crop_x_frac:null},
    ],
    track_count: 1,
    track_sequential: [true, true, true],
    include_intro: true,
    include_outro: true,
  };
}

async function addSelectionToBuilder() {
  if (_vtxState.selectionStart == null || _vtxState.selectionEnd == null) return;
  var btn = document.getElementById('vtx-add-btn');
  btn.disabled = true;
  var orig = btn.textContent;
  btn.textContent = 'Adding...';
  try {
    var r = await fetch('/analyze/api/scene-from-selection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        video_id: _vtxState.videoId,
        start:    _vtxState.selectionStart,
        end:      _vtxState.selectionEnd,
        text:     _vtxState.selectionText,
      }),
    });
    var data = await r.json();
    if (!r.ok) throw new Error(data.error || 'failed');

    // Load the builder snapshot (or seed an empty one).
    var snap = null;
    try {
      var raw = localStorage.getItem(BUILDER_STATE_KEY);
      if (raw) snap = JSON.parse(raw);
    } catch (e) { snap = null; }
    if (!snap || typeof snap !== 'object') snap = _builderEmptySnapshot();
    if (!Array.isArray(snap.video_track))   snap.video_track = [];
    if (!Array.isArray(snap.track_settings) || snap.track_settings.length < 3) {
      snap.track_settings = _builderEmptySnapshot().track_settings;
    }

    // End of Layer I = max(start_time + duration) over track 0. We don't
    // store duration on the saved entry, so build a {id: duration} map from
    // /api/clips. New entries get a fallback duration from the response.
    var durById = {};
    try {
      var cr = await fetch('/api/clips');
      var clips = await cr.json();
      for (var i = 0; i < clips.length; i++) durById[clips[i].id] = clips[i].duration;
    } catch (e) { /* fall through with empty map */ }
    var endOfLayer1 = 0;
    for (var j = 0; j < snap.video_track.length; j++) {
      var it = snap.video_track[j];
      if (it.type !== 'clip') continue;
      if ((it.track || 0) !== 0) continue;
      var d = (it.id !== undefined && durById[it.id] !== undefined)
              ? durById[it.id]
              : ((it.end != null && it.start != null) ? (it.end - it.start) : 0);
      var endT = (it.start_time || 0) + (d || 0);
      if (endT > endOfLayer1) endOfLayer1 = endT;
    }

    // Append the new clip at end of Layer I.
    snap.video_track.push({
      type: 'clip',
      id: data.scene_id,
      start_time: endOfLayer1,
      track: 0,
      wide: !!data.wide,
      stack_order: 0,
      volume: 5,
      muted: false,
      position: null,
      trans_in: null,
      trans_out: null,
      crop_x_frac: null,
      captions: 'inherit',
    });

    localStorage.setItem(BUILDER_STATE_KEY, JSON.stringify(snap));

    document.getElementById('vtx-sel-info').innerHTML =
      'Added scene <b>#' + data.scene_id + '</b> to Builder Layer I '
      + '(' + (data.duration || (data.end - data.start)).toFixed(1) + 's).';
  } catch (e) {
    document.getElementById('vtx-sel-info').textContent = 'Add failed: ' + e.message;
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

// ── Cut Scene confirmation modal ──────────────────────────────────────────
// Opens after a successful Cut Scene with the freshly-extracted clip
// previewed and Keep / Discard buttons. Keep is the no-op (scene already
// saved); Discard hits DELETE on the new scene_id.

var _cutScene = null;  // { id, start, end }

function openCutConfirm(sceneId, start, end, text) {
  _cutScene = { id: sceneId, start: start, end: end };
  var overlay = document.getElementById('cut-overlay');
  document.getElementById('cut-range').textContent =
    _vtxFmt(start) + ' – ' + _vtxFmt(end)
    + '  (' + (end - start).toFixed(1) + 's)';
  document.getElementById('cut-text').textContent = (text || '').trim();
  var v = document.getElementById('cut-video');
  v.src = '/rate/api/clip/' + sceneId;
  v.load();
  v.play().catch(function(){/* autoplay blocked, that's fine */});
  document.getElementById('cut-status').textContent = '';
  document.getElementById('cut-keep').disabled = false;
  document.getElementById('cut-discard').disabled = false;
  overlay.classList.add('active');
}

function closeCutConfirm() {
  var overlay = document.getElementById('cut-overlay');
  var v = document.getElementById('cut-video');
  v.pause(); v.removeAttribute('src'); v.load();
  overlay.classList.remove('active');
  _cutScene = null;
}

function cutKeep() {
  // Scene was already saved server-side; this just closes the modal.
  closeCutConfirm();
}

async function cutDiscard() {
  if (!_cutScene) return closeCutConfirm();
  var k = document.getElementById('cut-keep');
  var d = document.getElementById('cut-discard');
  var s = document.getElementById('cut-status');
  k.disabled = true; d.disabled = true;
  s.textContent = 'Discarding...';
  try {
    var r = await fetch('/analyze/api/scene/' + _cutScene.id, {method: 'DELETE'});
    if (!r.ok) throw new Error('delete failed');
    document.getElementById('vtx-sel-info').textContent =
      'Discarded scene #' + _cutScene.id + '.';
    closeCutConfirm();
  } catch (e) {
    s.textContent = 'Discard failed: ' + e.message;
    k.disabled = false; d.disabled = false;
  }
}

document.getElementById('vtx-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeVideoTranscript();
});

function analyzeAll() {
  if (analyzing) return;
  analyzing = true;
  renderList();
  document.getElementById('progress-lines').innerHTML = '';
  addLine('Queuing all pending videos...');

  fetch('/analyze/run-all', {method: 'POST'}).then(function(r) {
    return r.json();
  }).then(function(data) {
    if (data.status === 'nothing') {
      analyzing = false;
      addLine('No videos pending analysis.');
      renderList();
      return;
    }
    addLine('Analyzing ' + data.count + ' videos...', 'done');
    connectSSE();
  }).catch(function(e) {
    analyzing = false;
    addLine('Error: ' + e.message, 'error');
    renderList();
  });
}

async function checkState() {
  try {
    var res = await fetch('/analyze/state');
    var state = await res.json();
    if (state.running) {
      analyzing = true;
      analyzingVideoId = state.video_id;
      analyzingMode = state.mode || null;
      var prog = document.getElementById('progress');
      prog.classList.add('active');
      addLine('Reconnected — analyzing ' + (state.video_name || 'video') +
        ' (' + state.completed + '/' + state.total + ' done, ' +
        state.queued + ' queued)');
      renderList();
      connectSSE();
    }
  } catch(e) {}
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

scanVideos();
checkState();
</script>
</body>
</html>"""
