"""generator.py — Montage generation routes for PeaceGrappler."""

import json
import math
import os
import queue
import random
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from db import get_all_scenes, get_scene_grades, save_grade, save_generated_video
from video import (
    ASSETS_DIR, AUDIO_EXTENSIONS, OUTPUT_DIR, TRANSITIONS, XFADE_DUR,
    concatenate_clips, extract_subclip, find_asset, get_video_duration,
    normalize_clip, overlay_music,
)

generator_bp = Blueprint("generator", __name__)

MIN_CLIP_DUR = 2.5
MAX_CLIP_DUR = 8.0

gen_progress = queue.Queue()


def emit(msg):
    gen_progress.put(msg)


def find_music_files():
    """Find all music files under assets/music/ or assets/."""
    music = []
    music_dir = ASSETS_DIR / "music"
    search_dirs = [music_dir, ASSETS_DIR] if music_dir.exists() else [ASSETS_DIR]
    for d in search_dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in AUDIO_EXTENSIONS:
                music.append({"name": f.stem, "path": str(f)})
    return music


def clip_key(scene):
    return f"{Path(scene['video_filename']).name}@{scene['start_time']}"


def clip_weight(scene, grades_data):
    w = 1.0
    sid = scene["id"]
    if sid in grades_data:
        g = grades_data[sid]
        avg = g["total_score"] / g["times_graded"]
        w *= 0.3 + (avg - 1) * 0.175
    return max(0.05, w)


def pick_subclip(scene, target_dur):
    range_start = scene["start_time"]
    range_end = scene["end_time"]
    range_dur = range_end - range_start

    clip_dur = min(target_dur, range_dur)
    clip_dur = max(clip_dur, min(MIN_CLIP_DUR, range_dur))

    max_start = range_start + range_dur - clip_dur
    if max_start <= range_start:
        return range_start, clip_dur

    start = round(random.uniform(range_start, max_start), 1)
    return start, round(clip_dur, 1)


def select_clips(all_scenes, selected_tags, target_duration,
                  grades_data, used_keys=None, max_scene=None):
    if used_keys is None:
        used_keys = set()

    tag_set = set(selected_tags)

    matching = [
        s for s in all_scenes
        if set(s["tags"]) & tag_set
        and clip_key(s) not in used_keys
    ]

    if not matching:
        matching = [
            s for s in all_scenes
            if set(s["tags"]) & tag_set
        ]
    if not matching:
        return []

    effective_max = max_scene or MAX_CLIP_DUR

    pool = []
    for i, scene in enumerate(matching):
        dur = scene["end_time"] - scene["start_time"]
        if dur >= MIN_CLIP_DUR:
            w = clip_weight(scene, grades_data)
            if max_scene:
                if dur <= max_scene:
                    w *= 1.0 + (dur / max_scene) * 0.5
            pool.append((i, w))

    if not pool:
        return []

    pool.sort(key=lambda iw: -math.log(random.random() + 1e-10) / iw[1])

    selected = []
    total_dur = 0.0

    for pool_idx, (i, _w) in enumerate(pool):
        if total_dur >= target_duration:
            break

        scene = matching[i]
        remaining = target_duration - total_dur
        target_clip = random.uniform(MIN_CLIP_DUR, min(effective_max, remaining + 1.0))
        target_clip = min(target_clip, remaining + 1.5)
        if max_scene:
            target_clip = min(target_clip, max_scene)

        sub_start, sub_dur = pick_subclip(scene, target_clip)

        selected.append({
            **scene,
            "clip_start": sub_start,
            "clip_duration": sub_dur,
        })
        total_dur += sub_dur

    random.shuffle(selected)
    return selected


# ── routes ───────────────────────────────────────────────────────────────────

@generator_bp.route("/generate")
def generate_page():
    return GENERATE_HTML


@generator_bp.route("/generate/tags")
def generate_tags():
    """Return available tags with counts."""
    scenes = get_all_scenes()
    tag_counts = {}
    for s in scenes:
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    return jsonify(dict(sorted(tag_counts.items())))


@generator_bp.route("/generate/music")
def generate_music():
    return jsonify(find_music_files())


@generator_bp.route("/generate/run", methods=["POST"])
def generate_run():
    """Generate a montage with selected parameters. Runs in background."""
    data = request.json
    selected_tags = data.get("tags", [])
    target_duration = data.get("target_duration", 60)
    num_videos = data.get("num_videos", 1)
    max_scene = data.get("max_scene", 0) or None
    music_name = data.get("music", "")

    if not selected_tags:
        return jsonify({"error": "No tags selected"}), 400

    def _run():
        try:
            scenes = get_all_scenes()
            grades_data = get_scene_grades()

            if not scenes:
                emit("No analyzed scenes found in database")
                emit("DONE:error")
                return

            music_files = find_music_files()
            music_lookup = {m["name"]: m["path"] for m in music_files}
            music_path = music_lookup.get(music_name)

            intro_file = find_asset("intro")
            outro_file = find_asset("outro")

            today = datetime.now().strftime("%Y-%m-%d")
            date_dir = OUTPUT_DIR / today
            date_dir.mkdir(parents=True, exist_ok=True)

            used_keys = set()

            for vid_num in range(1, num_videos + 1):
                emit(f"Video {vid_num}/{num_videos}: selecting clips...")

                clips_info = select_clips(
                    scenes, selected_tags, target_duration,
                    grades_data, used_keys, max_scene,
                )

                if not clips_info:
                    emit(f"Video {vid_num}: no matching clips found")
                    continue

                # Track used clips
                for ci in clips_info:
                    used_keys.add(clip_key(ci))

                emit(f"Video {vid_num}: selected {len(clips_info)} clips")

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

                total_dur = int(sum(c["clip_duration"] for c in clips_info))
                out_file = date_dir / f"hl-{total_dur}-{counter}.mp4"

                with tempfile.TemporaryDirectory() as tmp:
                    clip_paths = []
                    intro_count = 0

                    if intro_file:
                        emit(f"Video {vid_num}: normalizing intro...")
                        intro_norm = os.path.join(tmp, "intro_norm.mp4")
                        if normalize_clip(str(intro_file), intro_norm):
                            clip_paths.append(intro_norm)
                            intro_count = 1

                    clip_transitions = [
                        random.choice(TRANSITIONS)
                        for _ in range(max(0, len(clips_info) - 1))
                    ]

                    for i, entry in enumerate(clips_info):
                        clip_out = os.path.join(tmp, f"clip_{i:03d}.mp4")
                        cs = entry["clip_start"]
                        cd = entry["clip_duration"]
                        src = Path(entry["video_path"]).name
                        emit(f"Video {vid_num}: clip {i+1}/{len(clips_info)} "
                             f"[{cs:.1f}s +{cd:.1f}s] from {src}")
                        if extract_subclip(entry["video_path"], cs, cd, clip_out):
                            clip_paths.append(clip_out)

                    outro_added = False
                    if outro_file:
                        emit(f"Video {vid_num}: normalizing outro...")
                        outro_norm = os.path.join(tmp, "outro_norm.mp4")
                        if normalize_clip(str(outro_file), outro_norm):
                            clip_paths.append(outro_norm)
                            outro_added = True

                    if len(clip_paths) < 2:
                        emit(f"Video {vid_num}: not enough clips assembled")
                        continue

                    # Build transitions list
                    n_paths = len(clip_paths)
                    all_transitions = [None] * (n_paths - 1)
                    if intro_count:
                        all_transitions[0] = "fade"
                    for j in range(len(clip_transitions)):
                        idx = j + intro_count
                        if idx < n_paths - 1:
                            all_transitions[idx] = clip_transitions[j]
                    if outro_added:
                        all_transitions[-1] = "fade"

                    emit(f"Video {vid_num}: assembling {len(clip_paths)} segments...")
                    assembled = os.path.join(tmp, "assembled.mp4")
                    if not concatenate_clips(clip_paths, assembled, all_transitions):
                        emit(f"Video {vid_num}: assembly failed")
                        continue

                    if music_path:
                        emit(f"Video {vid_num}: adding music...")
                        if not overlay_music(assembled, music_path, str(out_file)):
                            shutil.copy2(assembled, str(out_file))
                    else:
                        shutil.copy2(assembled, str(out_file))

                # Save timeline to database
                timeline = []
                if music_name:
                    timeline.append({"type": "music", "name": music_name, "volume": 3})
                for i, clip_entry in enumerate(clips_info):
                    if i > 0 and i - 1 < len(clip_transitions):
                        timeline.append({"type": "transition",
                                         "name": clip_transitions[i - 1]})
                    cs = clip_entry["clip_start"]
                    cd = clip_entry["clip_duration"]
                    timeline.append({
                        "type": "clip",
                        "video_file": clip_entry["video_path"],
                        "start": round(cs, 2),
                        "end": round(cs + cd, 2),
                    })

                final_dur = get_video_duration(str(out_file))
                save_generated_video(str(out_file), round(final_dur, 1), timeline)
                emit(f"Video {vid_num}: done! {final_dur:.1f}s -> {out_file}")

            emit("DONE:ok")

        except Exception as e:
            emit(f"Error: {e}")
            emit("DONE:error")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@generator_bp.route("/generate/status")
def generate_status():
    """SSE endpoint for progress updates."""
    def generate():
        while True:
            try:
                msg = gen_progress.get(timeout=30)
                yield f"data: {json.dumps({'message': msg})}\n\n"
                if msg.startswith("DONE:"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'message': 'waiting...'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ── HTML ─────────────────────────────────────────────────────────────────────

GENERATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - Montage Generator</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  padding:20px;max-width:800px;margin:0 auto;
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
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;color:#888;margin-bottom:6px;font-weight:600}
input[type="number"]{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 12px;font-size:14px;width:120px;
}
input[type="number"]:focus{outline:none;border-color:#e53935}
select{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 12px;font-size:14px;min-width:200px;
}
select:focus{outline:none;border-color:#e53935}
.tag-grid{display:flex;flex-wrap:wrap;gap:6px}
.tag-check{
  display:flex;align-items:center;gap:4px;
  padding:4px 10px;border-radius:16px;font-size:12px;
  background:#1a1a1a;border:1px solid #333;cursor:pointer;
  transition:all .15s;user-select:none;
}
.tag-check:hover{border-color:#666}
.tag-check.selected{background:#1b5e20;border-color:#4caf50;color:#a5d6a7}
.tag-check input{display:none}
.tag-check .count{color:#666;font-size:11px}
button{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:8px 16px;font-size:13px;cursor:pointer;
}
button.primary{background:#e53935;color:#fff;border-color:#e53935;font-weight:600;
  padding:10px 24px;font-size:14px}
button.primary:hover{background:#c62828}
button:disabled{opacity:.5;cursor:not-allowed}
.progress{
  margin-top:20px;padding:16px;background:#1a1a1a;border-radius:8px;
  font-size:13px;max-height:400px;overflow-y:auto;display:none;
}
.progress.active{display:block}
.progress .line{padding:2px 0;color:#aaa}
.progress .line.error{color:#ef5350}
.progress .line.done{color:#4caf50;font-weight:600}
.select-btns{display:flex;gap:8px;margin-bottom:8px}
.select-btns button{font-size:11px;padding:3px 8px}
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
    <a href="/analyze">Analyze</a>
  </nav>
</header>

<div class="form-group">
  <label>Tags</label>
  <div class="select-btns">
    <button onclick="selectAllTags()">Select All</button>
    <button onclick="selectNone()">Select None</button>
  </div>
  <div class="tag-grid" id="tag-grid"></div>
</div>

<div class="form-group">
  <label>Target Duration (seconds)</label>
  <input type="number" id="target-dur" value="60" min="10" max="600">
</div>

<div class="form-group">
  <label>Number of Videos</label>
  <input type="number" id="num-videos" value="1" min="1" max="20">
</div>

<div class="form-group">
  <label>Max Scene Duration (0 = no limit)</label>
  <input type="number" id="max-scene" value="0" min="0" max="60">
</div>

<div class="form-group">
  <label>Music</label>
  <select id="music-select">
    <option value="">No Music</option>
  </select>
</div>

<button class="primary" id="gen-btn" onclick="generate()">Generate Montage</button>

<div class="progress" id="progress">
  <div id="progress-lines"></div>
</div>

<script>
var generating = false;

async function init() {
  var tags = await fetch('/generate/tags').then(function(r){return r.json()});
  var grid = document.getElementById('tag-grid');
  for (var tag in tags) {
    var div = document.createElement('div');
    div.className = 'tag-check';
    div.dataset.tag = tag;
    div.innerHTML = '<input type="checkbox" value="' + tag + '"/> '
      + tag + ' <span class="count">(' + tags[tag] + ')</span>';
    div.onclick = function() {
      this.classList.toggle('selected');
      this.querySelector('input').checked = this.classList.contains('selected');
    };
    grid.appendChild(div);
  }

  var music = await fetch('/generate/music').then(function(r){return r.json()});
  var sel = document.getElementById('music-select');
  for (var i = 0; i < music.length; i++) {
    var o = document.createElement('option');
    o.value = music[i].name;
    o.textContent = music[i].name;
    sel.appendChild(o);
  }
}

function selectAllTags() {
  document.querySelectorAll('.tag-check').forEach(function(el) {
    el.classList.add('selected');
    el.querySelector('input').checked = true;
  });
}

function selectNone() {
  document.querySelectorAll('.tag-check').forEach(function(el) {
    el.classList.remove('selected');
    el.querySelector('input').checked = false;
  });
}

async function generate() {
  if (generating) return;

  var tags = [];
  document.querySelectorAll('.tag-check.selected').forEach(function(el) {
    tags.push(el.dataset.tag);
  });
  if (!tags.length) { alert('Select at least one tag'); return; }

  generating = true;
  document.getElementById('gen-btn').disabled = true;

  var prog = document.getElementById('progress');
  var lines = document.getElementById('progress-lines');
  prog.classList.add('active');
  lines.innerHTML = '';
  addLine('Starting generation...');

  try {
    await fetch('/generate/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        tags: tags,
        target_duration: parseInt(document.getElementById('target-dur').value) || 60,
        num_videos: parseInt(document.getElementById('num-videos').value) || 1,
        max_scene: parseInt(document.getElementById('max-scene').value) || 0,
        music: document.getElementById('music-select').value,
      }),
    });

    var es = new EventSource('/generate/status');
    es.onmessage = function(e) {
      var data = JSON.parse(e.data);
      var msg = data.message;
      if (msg.startsWith('DONE:')) {
        es.close();
        generating = false;
        document.getElementById('gen-btn').disabled = false;
        addLine(msg === 'DONE:ok' ? 'Generation complete!' : 'Generation failed',
                msg === 'DONE:ok' ? 'done' : 'error');
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
  } catch(e) {
    generating = false;
    document.getElementById('gen-btn').disabled = false;
    addLine('Error: ' + e.message, 'error');
  }
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

init();
</script>
</body>
</html>"""
