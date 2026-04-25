"""library.py — Video library routes for PeaceGrappler."""

import hashlib
import json
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import get_all_generated_videos
from video import OUTPUT_DIR, THUMB_DIR

library_bp = Blueprint("library", __name__)


def _get_video_thumbnail(video_path):
    """Generate or return cached thumbnail for a generated video."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"gen:{video_path}".encode()).hexdigest()
    path = THUMB_DIR / f"gen_{key}.jpg"

    if not path.exists() and Path(video_path).exists():
        try:
            # grab frame at 2 seconds (or 0.5s for very short videos)
            duration = 2.0
            subprocess.run(
                ["ffmpeg", "-ss", f"{duration:.2f}", "-i", video_path,
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4",
                 "-y", str(path)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    return path if path.exists() else None


def _extract_tags_from_timeline(timeline):
    """Pull unique tags from clips in a timeline."""
    tags = set()
    for item in timeline:
        if item.get("type") == "clip":
            video_file = item.get("video_file", "")
            if video_file:
                tags.add(Path(video_file).stem)
    return sorted(tags)


# ── routes ───────────────────────────────────────────────────────────────────

@library_bp.route("/library")
def library_page():
    return LIBRARY_HTML


@library_bp.route("/library/api/videos")
def library_videos():
    """Return all generated videos with metadata."""
    videos = get_all_generated_videos()
    result = []
    for v in videos:
        path = Path(v["path"])
        # Extract tags from timeline clips
        tags = set()
        for item in v.get("timeline", []):
            if item.get("type") == "clip":
                vf = item.get("video_file", "")
                if vf:
                    tags.add(Path(vf).stem)
        result.append({
            "id": v["id"],
            "filename": path.name,
            "path": v["path"],
            "duration": v["duration"],
            "generated_at": v["generated_at"],
            "exists": path.exists(),
            "tags": sorted(tags),
        })
    return jsonify(result)


@library_bp.route("/library/api/video/<int:video_id>")
def library_stream_video(video_id):
    """Stream a generated video file."""
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return "", 404
    path = Path(video["path"])
    if not path.exists():
        return "", 404
    return send_file(str(path), mimetype="video/mp4")


@library_bp.route("/library/api/thumbnail/<int:video_id>")
def library_thumbnail(video_id):
    """Return thumbnail for a generated video."""
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return "", 404
    thumb = _get_video_thumbnail(video["path"])
    if thumb and thumb.exists():
        return send_file(str(thumb), mimetype="image/jpeg")
    return "", 204


# ── HTML ─────────────────────────────────────────────────────────────────────

LIBRARY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - Library</title>
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

/* -- Toolbar -- */
.toolbar{
  padding:16px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  border-bottom:1px solid #1a1a1a;
}
.toolbar label{font-size:12px;color:#888;font-weight:600;text-transform:uppercase}
select{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 12px;font-size:13px;cursor:pointer;
}
select:hover{border-color:#666}
select:focus{outline:none;border-color:#e53935}
.video-count{font-size:13px;color:#888;margin-left:auto}

/* -- Tag filter chips -- */
.tag-filters{
  padding:12px 20px 0;display:flex;gap:6px;flex-wrap:wrap;
}
.tag-chip{
  padding:4px 10px;border-radius:14px;font-size:11px;font-weight:600;
  background:#1a1a1a;border:1px solid #333;cursor:pointer;
  transition:all .15s;user-select:none;color:#aaa;
}
.tag-chip:hover{border-color:#666;color:#fff}
.tag-chip.active{background:#e53935;border-color:#e53935;color:#fff}

/* -- Video grid -- */
.content{flex:1;padding:16px 20px;overflow-y:auto}
.video-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;
}
.video-card{
  background:#1a1a1a;border-radius:10px;overflow:hidden;
  transition:transform .15s,box-shadow .15s;cursor:pointer;
}
.video-card:hover{transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.5)}
.video-card .thumb-wrap{
  position:relative;width:100%;aspect-ratio:9/16;background:#111;overflow:hidden;
}
.video-card .thumb-wrap img{
  width:100%;height:100%;object-fit:cover;display:block;
}
.video-card .thumb-wrap .play-btn{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.4);opacity:0;transition:opacity .2s;
}
.video-card:hover .play-btn{opacity:1}
.play-btn .play-icon{
  width:56px;height:56px;background:rgba(229,57,53,.9);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  transition:transform .15s,background .15s;
}
.play-btn .play-icon:hover{transform:scale(1.1);background:#e53935}
.play-btn .play-icon svg{width:24px;height:24px;fill:#fff;margin-left:3px}
.video-card .dur-badge{
  position:absolute;bottom:8px;right:8px;
  background:rgba(0,0,0,.8);color:#fff;font-size:12px;font-weight:600;
  padding:2px 8px;border-radius:4px;
}
.video-card .meta{padding:10px 12px}
.video-card .meta .filename{
  font-size:13px;font-weight:600;color:#e0e0e0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.video-card .meta .date{font-size:11px;color:#666;margin-top:2px}
.video-card .meta .card-tags{
  margin-top:6px;display:flex;gap:4px;flex-wrap:wrap;
}
.video-card .meta .card-tags .ctag{
  font-size:10px;color:#888;background:#222;padding:1px 6px;border-radius:8px;
}

/* -- Empty state -- */
.empty{
  text-align:center;padding:60px 20px;color:#555;
}
.empty h2{font-size:20px;margin-bottom:8px;color:#666}
.empty p{font-size:14px}

/* -- Video player overlay -- */
.player-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:100;
  flex-direction:column;align-items:center;justify-content:center;
}
.player-overlay.active{display:flex}
.player-wrap{
  position:relative;max-width:90vw;max-height:85vh;
}
.player-wrap video{
  max-width:90vw;max-height:85vh;border-radius:8px;background:#000;
}
.player-close{
  position:absolute;top:-36px;right:0;
  background:none;color:#fff;border:none;font-size:28px;
  cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s;
}
.player-close:hover{opacity:1}
.player-info{
  margin-top:12px;text-align:center;font-size:13px;color:#888;
}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/builder">Builder</a>
    <a href="/analyze">Analyze</a>
    <a href="/generate">Generate</a>
    <a href="/library" class="active">Library</a>
    <a href="/wizard">AI Wizard</a>
  </nav>
</header>

<div class="toolbar">
  <label>Sort by</label>
  <select id="sort-select" onchange="applyFilters()">
    <option value="date-desc">Newest First</option>
    <option value="date-asc">Oldest First</option>
    <option value="dur-desc">Longest First</option>
    <option value="dur-asc">Shortest First</option>
  </select>
  <span class="video-count" id="video-count"></span>
</div>

<div class="tag-filters" id="tag-filters"></div>

<div class="content">
  <div class="video-grid" id="video-grid"></div>
  <div class="empty" id="empty-state" style="display:none">
    <h2>No Videos Yet</h2>
    <p>Generate videos from the Builder or Generator to see them here.</p>
  </div>
</div>

<div class="player-overlay" id="player-overlay">
  <div class="player-wrap">
    <button class="player-close" onclick="closePlayer()">&times;</button>
    <video id="player-video" controls></video>
    <div class="player-info" id="player-info"></div>
  </div>
</div>

<script>
var allVideos = [];
var allTags = [];
var activeTag = '';

async function init() {
  var res = await fetch('/library/api/videos');
  allVideos = await res.json();

  // Collect all unique tags
  var tagSet = {};
  for (var i = 0; i < allVideos.length; i++) {
    var tags = allVideos[i].tags;
    for (var j = 0; j < tags.length; j++) {
      tagSet[tags[j]] = (tagSet[tags[j]] || 0) + 1;
    }
  }
  allTags = Object.keys(tagSet).sort();
  renderTagFilters(tagSet);
  applyFilters();
}

function renderTagFilters(tagCounts) {
  var container = document.getElementById('tag-filters');
  container.innerHTML = '';

  if (allTags.length === 0) return;

  // "All" chip
  var allChip = document.createElement('span');
  allChip.className = 'tag-chip active';
  allChip.textContent = 'All';
  allChip.dataset.tag = '';
  allChip.onclick = function() { setActiveTag(''); };
  container.appendChild(allChip);

  for (var i = 0; i < allTags.length; i++) {
    var tag = allTags[i];
    var chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.textContent = tag + ' (' + (tagCounts[tag] || 0) + ')';
    chip.dataset.tag = tag;
    chip.onclick = (function(t) {
      return function() { setActiveTag(t); };
    })(tag);
    container.appendChild(chip);
  }
}

function setActiveTag(tag) {
  activeTag = tag;
  var chips = document.querySelectorAll('.tag-chip');
  for (var i = 0; i < chips.length; i++) {
    if (chips[i].dataset.tag === tag) {
      chips[i].classList.add('active');
    } else {
      chips[i].classList.remove('active');
    }
  }
  applyFilters();
}

function applyFilters() {
  var sort = document.getElementById('sort-select').value;
  var filtered = allVideos.filter(function(v) { return v.exists; });

  // Tag filter
  if (activeTag) {
    filtered = filtered.filter(function(v) {
      return v.tags.indexOf(activeTag) >= 0;
    });
  }

  // Sort
  filtered.sort(function(a, b) {
    if (sort === 'date-desc') return (b.generated_at || '').localeCompare(a.generated_at || '');
    if (sort === 'date-asc') return (a.generated_at || '').localeCompare(b.generated_at || '');
    if (sort === 'dur-desc') return b.duration - a.duration;
    if (sort === 'dur-asc') return a.duration - b.duration;
    return 0;
  });

  renderGrid(filtered);
  document.getElementById('video-count').textContent = filtered.length + ' video' + (filtered.length !== 1 ? 's' : '');
}

function formatDuration(sec) {
  if (!sec || sec <= 0) return '0s';
  var m = Math.floor(sec / 60);
  var s = Math.round(sec % 60);
  if (m > 0) return m + ':' + (s < 10 ? '0' : '') + s;
  return s + 's';
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  try {
    var d = new Date(dateStr + 'Z');
    return d.toLocaleDateString(undefined, {year:'numeric',month:'short',day:'numeric'})
      + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});
  } catch(e) { return dateStr; }
}

function renderGrid(videos) {
  var grid = document.getElementById('video-grid');
  var empty = document.getElementById('empty-state');

  if (!videos.length) {
    grid.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  var html = '';
  for (var i = 0; i < videos.length; i++) {
    var v = videos[i];
    var tagsHtml = '';
    var showTags = v.tags.slice(0, 4);
    for (var j = 0; j < showTags.length; j++) {
      tagsHtml += '<span class="ctag">' + showTags[j] + '</span>';
    }
    if (v.tags.length > 4) {
      tagsHtml += '<span class="ctag">+' + (v.tags.length - 4) + '</span>';
    }

    html += '<div class="video-card" onclick="playVideo(' + v.id + ',\'' + escHtml(v.filename) + '\')">'
      + '<div class="thumb-wrap">'
      + '<img src="/library/api/thumbnail/' + v.id + '" loading="lazy" alt=""/>'
      + '<div class="play-btn">'
      + '<div class="play-icon"><svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div>'
      + '</div>'
      + '<span class="dur-badge">' + formatDuration(v.duration) + '</span>'
      + '</div>'
      + '<div class="meta">'
      + '<div class="filename">' + escHtml(v.filename) + '</div>'
      + '<div class="date">' + formatDate(v.generated_at) + '</div>'
      + '<div class="card-tags">' + tagsHtml + '</div>'
      + '</div>'
      + '</div>';
  }
  grid.innerHTML = html;
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function playVideo(id, filename) {
  var overlay = document.getElementById('player-overlay');
  var video = document.getElementById('player-video');
  var info = document.getElementById('player-info');

  video.src = '/library/api/video/' + id;
  info.textContent = filename;
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

// Close on escape or clicking outside
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
