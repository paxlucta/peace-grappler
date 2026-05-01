"""rating.py — Scenes page for PeaceGrappler."""

import hashlib
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import get_all_scenes, get_scene_grades, save_grade
from video import THUMB_DIR

rating_bp = Blueprint("rating", __name__)


@rating_bp.route("/rate")
def rate_page():
    return SCENES_HTML


@rating_bp.route("/rate/api/scenes")
def api_scenes():
    """Return all scenes including excluded, with grade/status info."""
    scenes = get_all_scenes(include_ignored=True, include_excluded=True)
    grades = get_scene_grades()

    result = []
    for s in scenes:
        dur = round(s["end_time"] - s["start_time"], 1)
        grade_info = grades.get(s["id"])
        avg = round(grade_info["total_score"] / grade_info["times_graded"], 1) \
            if grade_info else None
        # Determine rating status
        if s.get("excluded"):
            status = "down"
        elif grade_info and avg >= 3:
            status = "up"
        else:
            status = "unrated"
        result.append({
            "id": s["id"],
            "filename": s["video_filename"],
            "start": s["start_time"],
            "end": s["end_time"],
            "duration": dur,
            "tags": s["tags"],
            "wide": s["wide"],
            "excluded": s.get("excluded", False),
            "status": status,
        })
    return jsonify(result)


@rating_bp.route("/rate/api/grade", methods=["POST"])
def api_grade():
    """Thumbs up (keep) or thumbs down (exclude)."""
    from db import set_scene_excluded
    data = request.json or {}
    scene_id = data.get("scene_id")
    action = data.get("action")  # "up" or "down"
    if not scene_id or action not in ("up", "down"):
        return jsonify({"error": "scene_id and action (up/down) required"}), 400
    if action == "up":
        save_grade(scene_id, 5)
        set_scene_excluded(scene_id, False)
    else:
        save_grade(scene_id, 1)
        set_scene_excluded(scene_id, True)
    return jsonify({"status": "ok", "action": action})


@rating_bp.route("/rate/api/clip/<int:scene_id>")
def api_clip(scene_id):
    """Extract and stream a scene's video clip."""
    from db import get_scene_by_id
    scene = get_scene_by_id(scene_id)
    if not scene:
        return "", 404

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


SCENES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - Scenes</title>
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

.content{flex:1;padding:16px 20px;overflow-y:auto}

/* -- Tag filters -- */
.tag-bar{
  display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;
}
.tag-chip{
  padding:4px 10px;border-radius:14px;font-size:11px;font-weight:600;
  background:#1a1a1a;border:1px solid #333;cursor:pointer;
  transition:all .15s;user-select:none;color:#aaa;
}
.tag-chip:hover{border-color:#666;color:#fff}
.tag-chip.active{background:#e53935;border-color:#e53935;color:#fff}
.tag-chip .chip-count{opacity:.6;margin-left:2px}
.scene-count{font-size:13px;color:#666;margin-bottom:12px}

/* -- Scene grid -- */
.scene-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;
}
.scene-card{
  background:#1a1a1a;border-radius:8px;overflow:hidden;
  position:relative;transition:transform .15s,opacity .2s;
}
.scene-card:hover{transform:translateY(-2px)}
.scene-card.excluded{opacity:.35}
.scene-card .thumb{
  width:100%;aspect-ratio:9/16;object-fit:cover;display:block;background:#111;
  cursor:pointer;
}
.scene-card .play-overlay{
  position:absolute;top:0;left:0;right:0;bottom:56px;
  display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.3);opacity:0;transition:opacity .2s;cursor:pointer;
}
.scene-card:hover .play-overlay{opacity:1}
.play-circle{
  width:40px;height:40px;background:rgba(229,57,53,.9);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
}
.play-circle svg{width:18px;height:18px;fill:#fff;margin-left:2px}
.scene-card .dur-badge{
  position:absolute;top:6px;right:6px;
  background:rgba(0,0,0,.75);color:#fff;font-size:10px;font-weight:600;
  padding:2px 5px;border-radius:3px;
}
.scene-card .unrated-badge{
  position:absolute;top:6px;left:6px;
  background:rgba(255,152,0,.85);color:#fff;font-size:9px;font-weight:700;
  padding:1px 5px;border-radius:3px;
}
.scene-card .excluded-badge{
  position:absolute;top:6px;left:6px;
  background:rgba(229,57,53,.85);color:#fff;font-size:9px;font-weight:700;
  padding:1px 5px;border-radius:3px;
}
.scene-card .info{padding:4px 8px 2px}
.scene-card .fn{
  font-size:9px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.scene-card .tg{
  font-size:9px;color:#888;margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}

/* -- Thumbs row on each card -- */
.scene-card .vote-row{
  display:flex;justify-content:center;gap:8px;padding:6px;
  border-top:1px solid #222;
}
.vote-btn{
  width:32px;height:32px;border-radius:50%;border:1.5px solid #444;
  background:#111;cursor:pointer;transition:all .12s;
  display:flex;align-items:center;justify-content:center;
}
.vote-btn:hover{transform:scale(1.15)}
.vote-btn svg{width:16px;height:16px}
.vote-btn.vdown{border-color:#555}
.vote-btn.vdown svg{fill:#666}
.vote-btn.vdown:hover{background:#ef5350;border-color:#ef5350}
.vote-btn.vdown:hover svg{fill:#fff}
.vote-btn.vdown.active{background:#ef5350;border-color:#ef5350}
.vote-btn.vdown.active svg{fill:#fff}
.vote-btn.vup{border-color:#555}
.vote-btn.vup svg{fill:#666}
.vote-btn.vup:hover{background:#4caf50;border-color:#4caf50}
.vote-btn.vup:hover svg{fill:#fff}
.vote-btn.vup.active{background:#4caf50;border-color:#4caf50}
.vote-btn.vup.active svg{fill:#fff}

/* -- Video player overlay -- */
.player-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:100;
  flex-direction:column;align-items:center;justify-content:center;
}
.player-overlay.active{display:flex}
.player-wrap{position:relative;max-width:90vw;max-height:85vh}
.player-wrap video{max-width:90vw;max-height:85vh;border-radius:8px;background:#000}
.player-close{
  position:absolute;top:-36px;right:0;
  background:none;color:#fff;border:none;font-size:28px;
  cursor:pointer;padding:4px 8px;opacity:.7;
}
.player-close:hover{opacity:1}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/wizard">AI Wizard</a>
    <a href="/builder">Builder</a>
    <a href="/library">Library</a>
    <a href="/rate" class="active">Scenes</a>
    <a href="/analyze">Analyze</a>
  </nav>
</header>

<div class="content">
  <div class="tag-bar" id="tag-bar"></div>
  <div class="scene-count" id="scene-count"></div>
  <div class="scene-grid" id="scene-grid"></div>
</div>

<div class="player-overlay" id="player-overlay">
  <div class="player-wrap">
    <button class="player-close" onclick="closePlayer()">&times;</button>
    <video id="player-video" controls></video>
  </div>
</div>

<script>
var allScenes = [];
var activeTag = '';

async function init() {
  allScenes = await fetch('/rate/api/scenes').then(function(r){return r.json()});
  renderTagBar();
  renderGrid();
}

function renderTagBar() {
  var bar = document.getElementById('tag-bar');
  // Collect tags + counts
  var tagCounts = {};
  var unratedCount = 0;
  var hiddenCount = 0;
  for (var i = 0; i < allScenes.length; i++) {
    var s = allScenes[i];
    if (s.status === 'unrated') unratedCount++;
    if (s.excluded) hiddenCount++;
    for (var j = 0; j < s.tags.length; j++) {
      tagCounts[s.tags[j]] = (tagCounts[s.tags[j]] || 0) + 1;
    }
  }

  var html = '';
  // All chip
  html += '<span class="tag-chip' + (activeTag === '' ? ' active' : '') + '" onclick="setTag(\'\')">'
    + 'All <span class="chip-count">(' + allScenes.length + ')</span></span>';
  // Unrated chip
  if (unratedCount > 0) {
    html += '<span class="tag-chip' + (activeTag === '__unrated__' ? ' active' : '')
      + '" style="border-color:#ff9800;color:#ffb74d" onclick="setTag(\'__unrated__\')">'
      + 'Unrated <span class="chip-count">(' + unratedCount + ')</span></span>';
  }
  // Hidden chip
  if (hiddenCount > 0) {
    html += '<span class="tag-chip' + (activeTag === '__hidden__' ? ' active' : '')
      + '" style="border-color:#ef5350;color:#ef9a9a" onclick="setTag(\'__hidden__\')">'
      + 'Hidden <span class="chip-count">(' + hiddenCount + ')</span></span>';
  }
  // Regular tags
  var sortedTags = Object.keys(tagCounts).sort();
  for (var k = 0; k < sortedTags.length; k++) {
    var t = sortedTags[k];
    html += '<span class="tag-chip' + (activeTag === t ? ' active' : '') + '" onclick="setTag(\'' + t + '\')">'
      + t + ' <span class="chip-count">(' + tagCounts[t] + ')</span></span>';
  }
  bar.innerHTML = html;
}

function setTag(tag) {
  activeTag = tag;
  renderTagBar();
  renderGrid();
}

function getFiltered() {
  if (activeTag === '__unrated__') {
    return allScenes.filter(function(s) { return s.status === 'unrated'; });
  }
  if (activeTag === '__hidden__') {
    return allScenes.filter(function(s) { return s.excluded; });
  }
  if (activeTag) {
    return allScenes.filter(function(s) { return s.tags.indexOf(activeTag) >= 0 && !s.excluded; });
  }
  return allScenes.filter(function(s) { return !s.excluded; });
}

function renderGrid() {
  var filtered = getFiltered();
  document.getElementById('scene-count').textContent = filtered.length + ' scene' + (filtered.length !== 1 ? 's' : '');

  var grid = document.getElementById('scene-grid');
  var html = '';
  for (var i = 0; i < filtered.length; i++) {
    var s = filtered[i];
    var tags = s.tags.length > 3
      ? s.tags.slice(0,3).join(', ') + '\u2026'
      : s.tags.join(', ');
    var cls = 'scene-card' + (s.excluded ? ' excluded' : '');

    var badge = '';
    if (s.excluded) {
      badge = '<span class="excluded-badge">HIDDEN</span>';
    } else if (s.status === 'unrated') {
      badge = '<span class="unrated-badge">UNRATED</span>';
    }

    html += '<div class="' + cls + '" id="sc-' + s.id + '">'
      + '<img class="thumb" src="/api/thumbnail/' + s.id + '" loading="lazy" onclick="playScene(' + s.id + ')"/>'
      + '<div class="play-overlay" onclick="playScene(' + s.id + ')"><div class="play-circle">'
      + '<svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div></div>'
      + '<span class="dur-badge">' + s.duration + 's</span>'
      + badge
      + '<div class="info">'
      + '<div class="fn">' + s.filename + ' [' + s.start.toFixed(1) + '-' + s.end.toFixed(1) + ']</div>'
      + '<div class="tg">' + tags + '</div>'
      + '</div>'
      + '<div class="vote-row">'
      + '<button class="vote-btn vdown' + (s.status === 'down' ? ' active' : '') + '" onclick="vote(' + s.id + ',\'down\')" title="Hide scene">'
      + '<svg viewBox="0 0 24 24"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>'
      + '</button>'
      + '<button class="vote-btn vup' + (s.status === 'up' ? ' active' : '') + '" onclick="vote(' + s.id + ',\'up\')" title="Keep scene">'
      + '<svg viewBox="0 0 24 24"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>'
      + '</button>'
      + '</div>'
      + '</div>';
  }
  grid.innerHTML = html;
}

async function vote(sceneId, action) {
  await fetch('/rate/api/grade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scene_id: sceneId, action: action}),
  });
  // Update local state
  for (var i = 0; i < allScenes.length; i++) {
    if (allScenes[i].id === sceneId) {
      allScenes[i].status = action;
      allScenes[i].excluded = action === 'down';
      break;
    }
  }
  renderTagBar();
  renderGrid();
}

function playScene(sceneId) {
  var overlay = document.getElementById('player-overlay');
  var video = document.getElementById('player-video');
  video.src = '/rate/api/clip/' + sceneId;
  overlay.classList.add('active');
  video.play();
}

function closePlayer() {
  var overlay = document.getElementById('player-overlay');
  var video = document.getElementById('player-video');
  video.pause();
  video.src = '';
  overlay.classList.remove('active');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closePlayer();
});
document.getElementById('player-overlay').addEventListener('click', function(e) {
  if (e.target === this) closePlayer();
});

init();
</script>
</body>
</html>"""
