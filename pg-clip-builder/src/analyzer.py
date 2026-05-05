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

from db import (
    get_all_videos, get_analyzed_tags, get_db, get_video_by_id,
    register_video, save_analysis,
)
from video import VIDEO_DIR, VIDEO_EXTENSIONS, get_video_dimensions, get_video_duration

analyzer_bp = Blueprint("analyzer", __name__)

CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"

# ── master tag list ──────────────────────────────────────────────────────────

TAGS = {
    "activity": [
        "grappling", "striking", "punching", "kicking", "takedown", "submission",
        "ground-and-pound", "clinch", "sprawl", "guard-pass", "sweep", "mount",
        "back-control", "arm-bar", "choke", "triangle", "knee-bar", "leg-lock",
        "wrestling", "judo-throw", "elbow", "knee-strike",
        "training", "sparring", "drilling", "pad-work", "bag-work", "warm-up",
        "stretching", "conditioning", "weightlifting", "running",
        "interview", "press-conference", "weigh-in", "face-off",
        "walkout", "entrance", "celebration", "corner-advice",
        "crowd", "audience-reaction", "referee", "judges",
        "promo", "graphic", "text-overlay", "logo", "intro", "outro",
        "behind-the-scenes", "travel", "eating", "lifestyle",
        "slow-motion", "replay", "highlight-reel", "talking", "posing", "photo",
    ],
    "setting": [
        "octagon", "cage", "ring", "gym", "outdoor", "beach", "street", "hotel",
        "arena", "backstage", "locker-room", "studio",
    ],
    "camera": [
        "close-up", "medium-shot", "wide-shot", "overhead", "pov", "handheld",
        "steady", "tracking", "slow-pan",
    ],
    "energy": [
        "high-energy", "medium-energy", "low-energy",
    ],
}

ALL_TAGS = []
for group in TAGS.values():
    ALL_TAGS.extend(group)
ALL_TAG_SET = set(ALL_TAGS)

# ── server-side analysis state ────────────────────────────────────────────────

progress_queue = queue.Queue()
_analysis_lock = threading.Lock()
_analysis_state = {
    "running": False,
    "video_id": None,
    "video_name": None,
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


# ── Claude CLI ───────────────────────────────────────────────────────────────

def call_claude(frames, prompt_text):
    """Send frames + prompt to Claude, return raw text response."""
    content = []
    for jpeg_bytes, label in frames:
        content.append({"type": "text", "text": f"[Frame at {label}]"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(jpeg_bytes).decode(),
            },
        })
    content.append({"type": "text", "text": prompt_text})

    message = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    })

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "--print",
                 "--input-format", "stream-json",
                 "--output-format", "stream-json",
                 "--verbose",
                 "--model", "claude-haiku-4-5-20251001"],
                input=message, capture_output=True, text=True, timeout=120,
            )

            raw = result.stdout.strip()
            stderr = result.stderr.strip() if result.stderr else ""

            # Check for auth / CLI errors
            if result.returncode != 0 and not raw:
                err_msg = stderr[:200] if stderr else "unknown error"
                if "auth" in err_msg.lower() or "login" in err_msg.lower() or "api key" in err_msg.lower():
                    emit_progress("Claude CLI not authenticated. Run 'claude' in Terminal to sign in.")
                    return None
                emit_progress(f"Claude CLI error: {err_msg}")
                if attempt < max_retries:
                    emit_progress(f"Retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(5)
                    continue
                return None

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

            if text_content:
                return text_content

            if attempt < max_retries:
                emit_progress(f"Empty response, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(5)
        except Exception as e:
            if attempt < max_retries:
                emit_progress(f"Attempt failed ({e}), retrying ({attempt + 1}/{max_retries})...")
                time.sleep(5)
            else:
                raise
    return None


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
You are analyzing frames from an MMA / combat sports video.
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
- Return ONLY the JSON object, no markdown fences, no explanation
"""

INCREMENTAL_TAG_PROMPT = """\
You are analyzing frames from an MMA / combat sports video.
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

    prompt = FULL_ANALYSIS_PROMPT.format(duration=duration, tag_list=tag_list)

    try:
        raw = call_claude(frames, prompt)
        if not raw:
            return None
        result = parse_json_response(raw)
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
    return ANALYZE_HTML


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
        })

    return jsonify({"registered": registered, "videos": result})


def _analyze_one(video_id, force):
    """Analyze a single video. Runs inside the worker thread."""
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

        if force or not analyzed_tags:
            emit_progress(f"Full analysis of {video['filename']} ({duration:.1f}s)...")
            result = analyze_full(video_path, duration)
            if result is None:
                emit_progress("Analysis failed")
                return False
            save_analysis(video_id, result["tags"], result["moments"],
                          list(ALL_TAG_SET))
            emit_progress(f"Saved {len(result['tags'])} tags")

        elif new_tags:
            emit_progress(f"Incremental analysis ({len(new_tags)} new tags)...")
            new_tag_results = analyze_incremental(video_path, duration, new_tags)
            if new_tag_results:
                save_analysis(video_id, new_tag_results, [], list(new_tags))
                emit_progress(f"Saved {len(new_tag_results)} new tags")
            else:
                save_analysis(video_id, {}, [], list(new_tags))
                emit_progress("No new tags found")

        else:
            emit_progress("Video is up to date, nothing to analyze")

        return True
    except Exception as e:
        emit_progress(f"Error: {e}")
        return False


def _worker_loop():
    """Background worker that processes the analysis queue."""
    while True:
        with _analysis_lock:
            if not _analysis_state["queued"]:
                _analysis_state["running"] = False
                _analysis_state["video_id"] = None
                _analysis_state["video_name"] = None
                emit_progress("QUEUE:done")
                return
            item = _analysis_state["queued"].pop(0)

        vid_id = item["id"]
        force = item["force"]
        emit_progress(f"--- Video {_analysis_state['completed'] + 1}/{_analysis_state['total']} ---")
        ok = _analyze_one(vid_id, force)

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
    """Queue a single video for analysis."""
    video = get_video_by_id(video_id)
    if not video:
        return jsonify({"error": "Video not found"}), 404
    force = request.json.get("force", False) if request.json else False
    _start_worker([{"id": video_id, "force": force}])
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
<title>PeaceGrappler - Video Analysis</title>
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
.progress{
  margin-top:20px;padding:16px;background:#1a1a1a;border-radius:8px;
  font-size:13px;max-height:300px;overflow-y:auto;display:none;
}
.progress.active{display:block}
.progress .line{padding:2px 0;color:#aaa}
.progress .line.error{color:#ef5350}
.progress .line.done{color:#4caf50;font-weight:600}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/wizard">AI Wizard</a>
    <a href="/builder">Builder</a>
    <a href="/library">Library</a>
    <a href="/rate">Scenes</a>
    <a href="/analyze" class="active">Analyze</a>
  </nav>
</header>

<div class="controls">
  <button class="primary" onclick="scanVideos()">Scan for New Videos</button>
  <button class="primary" id="analyze-all-btn" onclick="analyzeAll()" style="display:none">Analyze All</button>
  <span id="scan-status" style="font-size:13px;color:#888"></span>
</div>

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

function renderList() {
  var tbody = document.getElementById('video-list');
  tbody.innerHTML = '';
  for (var i = 0; i < videos.length; i++) {
    var v = videos[i];
    var status, statusClass;
    if (analyzing && analyzingVideoId === v.id) {
      status = 'Analyzing\u2026';
      statusClass = 'status-analyzing';
    } else if (!v.analyzed_at) {
      status = 'New';
      statusClass = 'status-new';
    } else if (v.needs_update) {
      status = v.analyzed_tag_count + '/' + v.total_tag_count + ' tags';
      statusClass = 'status-partial';
    } else {
      status = 'Done';
      statusClass = 'status-done';
    }
    var dims = v.width + 'x' + v.height;
    if (v.wide) dims += ' (wide)';
    var btnLabel = v.analyzed_at ? (v.needs_update ? 'Update' : 'Re-analyze') : 'Analyze';
    var force = !!(v.analyzed_at && !v.needs_update);
    tbody.innerHTML += '<tr>'
      + '<td>' + v.filename + '</td>'
      + '<td>' + v.duration + 's</td>'
      + '<td>' + dims + '</td>'
      + '<td><span class="status-badge ' + statusClass + '">' + status + '</span></td>'
      + '<td><button onclick="analyzeVideo(' + v.id + ',' + force + ')" '
      + (analyzing ? 'disabled' : '') + '>' + btnLabel + '</button></td>'
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
  if (analyzing) return;
  analyzing = true;
  analyzingVideoId = videoId;
  renderList();
  document.getElementById('progress-lines').innerHTML = '';
  addLine('Starting analysis...');

  fetch('/analyze/run/' + videoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({force: force}),
  }).then(function() {
    connectSSE();
  }).catch(function(e) {
    analyzing = false;
    analyzingVideoId = null;
    addLine('Error: ' + e.message, 'error');
    renderList();
  });
}

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
