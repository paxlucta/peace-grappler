"""rating.py — Scene rating page for PeaceGrappler."""

import hashlib
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import get_all_scenes, get_scene_grades, save_grade
from video import THUMB_DIR

rating_bp = Blueprint("rating", __name__)


@rating_bp.route("/rate")
def rate_page():
    return RATE_HTML


@rating_bp.route("/rate/api/scenes")
def api_scenes():
    """Return all scenes with grade info for rating."""
    scenes = get_all_scenes(include_ignored=True, include_excluded=False)
    grades = get_scene_grades()
    tag = request.args.get("tag", "")

    result = []
    for s in scenes:
        if tag and tag not in s["tags"]:
            continue
        dur = round(s["end_time"] - s["start_time"], 1)
        grade_info = grades.get(s["id"])
        avg = round(grade_info["total_score"] / grade_info["times_graded"], 1) \
            if grade_info else None
        result.append({
            "id": s["id"],
            "filename": s["video_filename"],
            "start": s["start_time"],
            "end": s["end_time"],
            "duration": dur,
            "tags": s["tags"],
            "wide": s["wide"],
            "avg_grade": avg,
            "times_graded": grade_info["times_graded"] if grade_info else 0,
        })
    return jsonify(result)


@rating_bp.route("/rate/api/grade", methods=["POST"])
def api_grade():
    """Thumbs up (score=5) or thumbs down (score=1 + exclude)."""
    from db import set_scene_excluded
    data = request.json or {}
    scene_id = data.get("scene_id")
    action = data.get("action")  # "up" or "down"
    if not scene_id or action not in ("up", "down"):
        return jsonify({"error": "scene_id and action (up/down) required"}), 400
    if action == "up":
        save_grade(scene_id, 5)
    else:
        save_grade(scene_id, 1)
        set_scene_excluded(scene_id, True)
    return jsonify({"status": "ok", "action": action})


@rating_bp.route("/rate/api/tags")
def api_tags():
    scenes = get_all_scenes(include_ignored=True)
    tag_counts = {}
    for s in scenes:
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    return jsonify(dict(sorted(tag_counts.items())))


@rating_bp.route("/rate/api/clip/<int:scene_id>")
def api_clip(scene_id):
    """Extract and stream a scene's video clip."""
    from db import get_scene_by_id
    scene = get_scene_by_id(scene_id)
    if not scene:
        return "", 404

    # Cache extracted clips
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(
        f"clip:{scene['video_path']}@{scene['start_time']}@{scene['end_time']}".encode()
    ).hexdigest()
    clip_path = THUMB_DIR / f"clip_{key}.mp4"

    if not clip_path.exists():
        dur = scene["end_time"] - scene["start_time"]
        try:
            subprocess.run(
                ["ffmpeg", "-ss", f"{scene['start_time']:.2f}",
                 "-i", scene["video_path"],
                 "-t", f"{dur:.2f}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "96k",
                 "-y", str(clip_path)],
                capture_output=True, timeout=60,
            )
        except Exception:
            return "", 500

    if clip_path.exists():
        return send_file(str(clip_path), mimetype="video/mp4")
    return "", 500


RATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - Rate Scenes</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:flex;flex-direction:column;min-height:100vh;
}
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

.content{flex:1;display:flex;flex-direction:column;align-items:center;padding:24px}

/* -- Controls -- */
.controls{
  display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap;
}
.controls label{font-size:12px;color:#888;font-weight:600;text-transform:uppercase}
select{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 12px;font-size:13px;
}
select:focus{outline:none;border-color:#e53935}
.progress-text{font-size:13px;color:#888}

/* -- Rating card -- */
.rate-card{
  background:#141414;border:1px solid #2a2a2a;border-radius:12px;
  width:100%;max-width:400px;overflow:hidden;
}
.rate-card .thumb-wrap{
  position:relative;width:100%;aspect-ratio:9/16;background:#111;cursor:pointer;
}
.rate-card .thumb-wrap img{
  width:100%;height:100%;object-fit:cover;display:block;
}
.rate-card .thumb-wrap video{
  width:100%;height:100%;object-fit:cover;display:block;
}
.rate-card .play-overlay{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.35);transition:opacity .2s;
}
.rate-card .thumb-wrap:hover .play-overlay{background:rgba(0,0,0,.5)}
.play-circle{
  width:64px;height:64px;background:rgba(229,57,53,.9);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  transition:transform .15s;
}
.play-circle:hover{transform:scale(1.1)}
.play-circle svg{width:28px;height:28px;fill:#fff;margin-left:4px}
.rate-card .meta{padding:14px 16px}
.rate-card .meta .filename{font-size:12px;color:#666;margin-bottom:4px}
.rate-card .meta .tags{font-size:13px;color:#aaa;margin-bottom:4px}
.rate-card .meta .dur{font-size:12px;color:#555}
.rate-card .meta .prev-grade{
  font-size:12px;color:#818cf8;margin-top:4px;
}

/* -- Thumbs buttons -- */
.thumbs{
  display:flex;justify-content:center;gap:24px;padding:16px;
  border-top:1px solid #2a2a2a;
}
.thumb-btn{
  width:64px;height:64px;border-radius:50%;border:2px solid #444;
  background:#1a1a1a;cursor:pointer;transition:all .15s;
  display:flex;align-items:center;justify-content:center;
}
.thumb-btn:hover{transform:scale(1.15)}
.thumb-btn svg{width:28px;height:28px}
.thumb-btn.down{border-color:#ef5350}
.thumb-btn.down:hover{background:#ef5350}
.thumb-btn.down svg{fill:#ef5350}
.thumb-btn.down:hover svg{fill:#fff}
.thumb-btn.up{border-color:#4caf50}
.thumb-btn.up:hover{background:#4caf50}
.thumb-btn.up svg{fill:#4caf50}
.thumb-btn.up:hover svg{fill:#fff}

.skip-btn{
  display:block;margin:0 auto 20px;background:none;border:1px solid #333;
  color:#666;border-radius:6px;padding:6px 20px;font-size:12px;cursor:pointer;
}
.skip-btn:hover{color:#aaa;border-color:#555}

/* -- Empty / done -- */
.done-msg{
  text-align:center;padding:40px;color:#4caf50;font-size:18px;font-weight:600;
}

/* -- Keyboard hint -- */
.hint{text-align:center;font-size:11px;color:#444;margin-top:12px}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/builder">Builder</a>
    <a href="/analyze">Analyze</a>
    <a href="/library">Library</a>
    <a href="/wizard">AI Wizard</a>
    <a href="/rate" class="active">Rate</a>
  </nav>
</header>

<div class="content">
  <div class="controls">
    <label>Filter</label>
    <select id="tag-filter" onchange="loadScenes()">
      <option value="">All Scenes</option>
      <option value="__unrated__">Unrated Only</option>
    </select>
    <span class="progress-text" id="progress-text"></span>
  </div>

  <div id="card-area"></div>
  <div class="hint">Keyboard: Y or &rarr; thumbs up, N or &larr; thumbs down, Space to play, S to skip</div>
</div>

<script>
var scenes = [];
var currentIdx = 0;
var showRated = true;

async function loadScenes() {
  var tag = document.getElementById('tag-filter').value;
  var url = '/rate/api/scenes';
  if (tag && tag !== '__unrated__') {
    url += '?tag=' + encodeURIComponent(tag);
  }

  var data = await fetch(url).then(function(r){return r.json()});

  if (tag === '__unrated__') {
    scenes = data.filter(function(s) { return s.times_graded === 0; });
  } else {
    scenes = data;
  }

  // Shuffle for variety
  for (var i = scenes.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = scenes[i]; scenes[i] = scenes[j]; scenes[j] = tmp;
  }

  currentIdx = 0;
  renderCard();
}

function renderCard() {
  var area = document.getElementById('card-area');
  var prog = document.getElementById('progress-text');

  if (currentIdx >= scenes.length) {
    area.innerHTML = '<div class="done-msg">All scenes rated!</div>';
    prog.textContent = scenes.length + ' / ' + scenes.length;
    return;
  }

  var s = scenes[currentIdx];
  prog.textContent = (currentIdx + 1) + ' / ' + scenes.length;

  var tags = s.tags.length ? s.tags.join(', ') : 'no tags';

  area.innerHTML = '<div class="rate-card">'
    + '<div class="thumb-wrap" id="thumb-wrap" onclick="playScene(' + s.id + ')">'
    + '<img src="/api/thumbnail/' + s.id + '" loading="lazy"/>'
    + '<div class="play-overlay"><div class="play-circle">'
    + '<svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg>'
    + '</div></div>'
    + '</div>'
    + '<div class="meta">'
    + '<div class="filename">' + s.filename + ' [' + s.start.toFixed(1) + '-' + s.end.toFixed(1) + ']</div>'
    + '<div class="tags">' + tags + '</div>'
    + '<div class="dur">' + s.duration + 's' + (s.wide ? ' (wide)' : '') + '</div>'
    + '</div>'
    + '<div class="thumbs">'
    + '<button class="thumb-btn down" onclick="rate(\'down\')" title="Exclude this scene">'
    + '<svg viewBox="0 0 24 24"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>'
    + '</button>'
    + '<button class="thumb-btn up" onclick="rate(\'up\')" title="Keep this scene">'
    + '<svg viewBox="0 0 24 24"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>'
    + '</button>'
    + '</div>'
    + '</div>'
    + '<button class="skip-btn" onclick="skip()">Skip</button>';
}

async function rate(action) {
  var s = scenes[currentIdx];
  await fetch('/rate/api/grade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scene_id: s.id, action: action}),
  });
  currentIdx++;
  renderCard();
}

function skip() {
  currentIdx++;
  renderCard();
}

function playScene(sceneId) {
  var wrap = document.getElementById('thumb-wrap');
  if (wrap.querySelector('video')) return; // already playing
  wrap.innerHTML = '<video controls autoplay src="/rate/api/clip/' + sceneId + '"></video>';
  wrap.onclick = null;
  wrap.style.cursor = 'default';
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  var key = e.key;
  if (key === 'ArrowRight' || key === 'y' || key === 'Y') {
    rate('up');
  } else if (key === 'ArrowLeft' || key === 'n' || key === 'N') {
    rate('down');
  } else if (key === 's' || key === 'S') {
    skip();
  } else if (key === 'p' || key === 'P' || key === ' ') {
    e.preventDefault();
    if (currentIdx < scenes.length) playScene(scenes[currentIdx].id);
  }
});

// Load tags into filter
async function loadTags() {
  var tags = await fetch('/rate/api/tags').then(function(r){return r.json()});
  var sel = document.getElementById('tag-filter');
  for (var tag in tags) {
    var o = document.createElement('option');
    o.value = tag;
    o.textContent = tag + ' (' + tags[tag] + ')';
    sel.appendChild(o);
  }
}

loadTags();
loadScenes();
</script>
</body>
</html>"""
