"""rating.py — Scenes page for PeaceGrappler."""

import hashlib
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import (
    get_all_scenes, get_scene_grades, get_scene_by_id,
    get_scene_ids_with_transcripts, get_transcripts_in_range,
    save_grade, search_scene_ids_by_text,
)
from video import THUMB_DIR

# ISO code → display name for the transcript modal header.
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

rating_bp = Blueprint("rating", __name__)


@rating_bp.route("/rate")
def rate_page():
    from chrome import inject_chrome
    return inject_chrome(SCENES_HTML, active="rate")


@rating_bp.route("/rate/api/scenes")
def api_scenes():
    """Return all scenes including excluded, with grade/status info."""
    scenes = get_all_scenes(include_ignored=True, include_excluded=True)
    grades = get_scene_grades()
    transcript_scene_ids = get_scene_ids_with_transcripts()

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
            "analyzer_provider": s.get("analyzer_provider") or "",
            "analyzer_model":    s.get("analyzer_model")    or "",
            "visual_analyzer_provider": s.get("visual_analyzer_provider") or "",
            "visual_analyzer_model":    s.get("visual_analyzer_model")    or "",
            "speech_analyzer_provider": s.get("speech_analyzer_provider") or "",
            "speech_analyzer_model":    s.get("speech_analyzer_model")    or "",
            "has_transcript": s["id"] in transcript_scene_ids,
        })
    return jsonify(result)


@rating_bp.route("/rate/api/grade", methods=["POST"])
def api_grade():
    """Thumbs up (keep) or thumbs down (exclude)."""
    from db import remove_scene_tag, set_scene_excluded
    data = request.json or {}
    scene_id = data.get("scene_id")
    action = data.get("action")  # "up" or "down"
    if not scene_id or action not in ("up", "down"):
        return jsonify({"error": "scene_id and action (up/down) required"}), 400
    if action == "up":
        save_grade(scene_id, 5)
        set_scene_excluded(scene_id, False)
        # An up-vote overrides any prior auto-hide so it can be used again
        # AND so the vote-learning pass sees a clean training signal.
        remove_scene_tag(scene_id, "auto-hidden")
    else:
        save_grade(scene_id, 1)
        set_scene_excluded(scene_id, True)
    return jsonify({"status": "ok", "action": action})


@rating_bp.route("/rate/api/search")
def api_search():
    """Loose text search over transcripts. Returns scene IDs whose time
    window contains a transcript row matching the query (all whitespace-
    separated tokens must appear in some row, case-insensitive)."""
    q = request.args.get("q", "").strip()
    ids = search_scene_ids_by_text(q) if q else set()
    return jsonify({"q": q, "scene_ids": sorted(ids)})


@rating_bp.route("/rate/api/scene/<int:scene_id>/transcript")
def api_scene_transcript(scene_id):
    """Return transcript segments overlapping the scene's time range, grouped
    by language. Used by the transcript icon on each scene card."""
    scene = get_scene_by_id(scene_id)
    if not scene:
        return jsonify({"error": "scene not found"}), 404
    groups = get_transcripts_in_range(
        scene["video_id"], scene["start_time"], scene["end_time"],
    )
    return jsonify({
        "scene_id":   scene_id,
        "start":      scene["start_time"],
        "end":        scene["end_time"],
        "filename":   scene["video_filename"],
        "groups":     [{
            "language":       g["language"],
            "is_translation": g["is_translation"],
            "label":          _lang_label(g["language"], g["is_translation"]),
            "segments":       g["segments"],
        } for g in groups],
    })


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
<title>ClipBuilder - Scenes</title>
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

/* -- Transcript search -- */
.search-row{display:flex;gap:8px;margin-bottom:12px;align-items:center}
.search-input{
  flex:1;background:#1a1a1a;border:1px solid #333;border-radius:6px;
  color:#e0e0e0;font-size:13px;padding:7px 10px;outline:none;
  transition:border-color .15s;
}
.search-input:focus{border-color:#1976d2}
.search-input::placeholder{color:#555}
.search-clear{
  background:#1a1a1a;border:1px solid #333;color:#888;
  border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer;
}
.search-clear:hover{color:#fff;border-color:#666}
.search-status{font-size:11px;color:#1976d2;font-weight:600;white-space:nowrap}

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
.scene-card .scene-ai-badge{
  position:absolute;bottom:62px;right:6px;
  display:inline-flex;align-items:center;
  background:rgba(0,0,0,.7);padding:3px;border-radius:4px;
}
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
  border-top:1px solid #222;align-items:center;position:relative;
}
/* Transcript button anchors to the far right without shifting the
   centered up/down vote pair. */
.scene-card .vote-row .vote-btn-end{
  position:absolute;right:6px;top:50%;transform:translateY(-50%);
}
.scene-card .vote-row .vote-btn-end:hover{transform:translateY(-50%) scale(1.15)}
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
.vote-btn.vtxt{border-color:#555}
.vote-btn.vtxt svg{fill:#888}
.vote-btn.vtxt:hover{background:#1976d2;border-color:#1976d2}
.vote-btn.vtxt:hover svg{fill:#fff}

/* -- Transcript modal -- */
.tx-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:110;
  align-items:center;justify-content:center;
}
.tx-overlay.active{display:flex}
.tx-modal{
  background:#161616;border:1px solid #2a2a2a;border-radius:10px;
  width:min(640px,92vw);max-height:80vh;display:flex;flex-direction:column;
  box-shadow:0 12px 40px rgba(0,0,0,.6);
}
.tx-head{
  padding:14px 18px;border-bottom:1px solid #242424;
  display:flex;align-items:center;gap:10px;
}
.tx-head h3{font-size:14px;font-weight:600;color:#fff;flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tx-head .tx-range{font-size:11px;color:#888}
.tx-close{
  background:none;border:none;color:#888;font-size:22px;
  cursor:pointer;padding:0 4px;line-height:1;
}
.tx-close:hover{color:#fff}
.tx-body{padding:8px 18px 18px;overflow-y:auto;flex:1}
.tx-group{margin-top:14px}
.tx-group:first-child{margin-top:6px}
.tx-group-label{
  font-size:10px;font-weight:700;color:#1976d2;text-transform:uppercase;
  letter-spacing:.5px;margin-bottom:6px;
}
.tx-group.is-xlat .tx-group-label{color:#4caf50}
.tx-seg{
  display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #1f1f1f;
  font-size:13px;line-height:1.45;
}
.tx-seg:last-child{border-bottom:none}
.tx-seg-time{
  color:#666;font-size:10px;font-family:'SF Mono',Menlo,monospace;
  flex-shrink:0;min-width:54px;padding-top:2px;
}
.tx-seg-text{color:#ddd;flex:1}
.tx-empty{color:#666;font-size:13px;text-align:center;padding:20px 0}

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

<!-- pg-chrome -->

<div class="content">
  <div class="search-row">
    <input id="search-input" class="search-input" type="search"
           placeholder="Search transcript text — e.g. &quot;genitive case&quot; or a phrase the speaker said"
           autocomplete="off">
    <span class="search-status" id="search-status"></span>
    <button class="search-clear" id="search-clear" onclick="clearSearch()" style="display:none">Clear</button>
  </div>
  <div class="search-row" style="margin-top:-4px">
    <label for="file-filter" style="font-size:12px;color:#888;flex-shrink:0">Filter by file:</label>
    <select id="file-filter" class="search-input" onchange="setFileFilter(this.value)"
            style="flex:1;cursor:pointer">
      <option value="">All files</option>
    </select>
  </div>
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

<div class="tx-overlay" id="tx-overlay">
  <div class="tx-modal">
    <div class="tx-head">
      <h3 id="tx-title">Transcript</h3>
      <span class="tx-range" id="tx-range"></span>
      <button class="tx-close" onclick="closeTranscript()">&times;</button>
    </div>
    <div class="tx-body" id="tx-body"></div>
  </div>
</div>

<script>
var allScenes = [];
var activeTag = '';
var activeFile = '';         // filename selected in the file-filter dropdown; '' = all
var searchQuery = '';        // current text query
var searchHitIds = null;     // null = no search active; Set of scene_ids when active
var searchTimer = null;

async function init() {
  allScenes = await fetch('/rate/api/scenes').then(function(r){return r.json()});
  renderFileFilter();
  renderTagBar();
  renderGrid();
}

function renderFileFilter() {
  var sel = document.getElementById('file-filter');
  if (!sel) return;
  // Count scenes per source file so the dropdown shows useful context.
  var counts = {};
  for (var i = 0; i < allScenes.length; i++) {
    var fn = allScenes[i].filename || '';
    counts[fn] = (counts[fn] || 0) + 1;
  }
  var names = Object.keys(counts).sort(function(a, b){
    return a.localeCompare(b);
  });
  var html = '<option value="">All files (' + allScenes.length + ')</option>';
  for (var k = 0; k < names.length; k++) {
    var n = names[k];
    html += '<option value="' + n.replace(/"/g, '&quot;') + '">'
          + n + ' (' + counts[n] + ')</option>';
  }
  sel.innerHTML = html;
  sel.value = activeFile;
}

function setFileFilter(name) {
  activeFile = name || '';
  renderTagBar();   // counts may shrink to scenes in this file
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
  var base;
  if (activeTag === '__unrated__') {
    base = allScenes.filter(function(s) { return s.status === 'unrated'; });
  } else if (activeTag === '__hidden__') {
    base = allScenes.filter(function(s) { return s.excluded; });
  } else if (activeTag) {
    // For "hidden-by-design" tags (auto-hidden), show the excluded scenes
    // they refer to — otherwise the chip would always be empty.
    var includeExcluded = (activeTag === 'auto-hidden');
    base = allScenes.filter(function(s) {
      return s.tags.indexOf(activeTag) >= 0 && (includeExcluded || !s.excluded);
    });
  } else {
    base = allScenes.filter(function(s) { return !s.excluded; });
  }
  if (searchHitIds) {
    base = base.filter(function(s) { return searchHitIds.has(s.id); });
  }
  if (activeFile) {
    base = base.filter(function(s) { return s.filename === activeFile; });
  }
  return base;
}

function renderGrid() {
  var filtered = getFiltered();
  document.getElementById('scene-count').textContent = filtered.length + ' scene' + (filtered.length !== 1 ? 's' : '');

  var grid = document.getElementById('scene-grid');
  if (filtered.length === 0) {
    var msg;
    if (allScenes.length === 0) {
      msg = '<h2 style="font-size:18px;color:#888;margin-bottom:6px">No scenes yet</h2>'
        + '<p style="color:#666;font-size:13px">Run analysis on the '
        + '<a href="/analyze" style="color:#1976d2">Input Videos</a> page to break videos into scenes.</p>';
    } else if (searchHitIds) {
      msg = '<p style="color:#666;font-size:13px">No scenes match your search.</p>';
    } else {
      msg = '<p style="color:#666;font-size:13px">No scenes match this filter.</p>';
    }
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:48px 0">'
      + msg + '</div>';
    return;
  }
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

    // Brand badge + model in tooltip. We prefer the visual-mode pair
    // when present (most scenes came from visual analysis), falling
    // back to speech, then the legacy single-provider column.
    var sceneProv = s.visual_analyzer_provider
                 || s.speech_analyzer_provider
                 || s.analyzer_provider || '';
    var sceneModel = s.visual_analyzer_model
                  || s.speech_analyzer_model
                  || s.analyzer_model || '';
    var aiBadge = (window.pgAiBadge && sceneProv)
      ? '<span class="scene-ai-badge">'
        + window.pgAiBadge(sceneProv, {
            size: 13,
            model: sceneModel,
            title: 'Tagged by ' + sceneProv
                  + (sceneModel ? ' · ' + sceneModel : ''),
          }) + '</span>'
      : '';
    html += '<div class="' + cls + '" id="sc-' + s.id + '">'
      + '<img class="thumb" src="/api/thumbnail/' + s.id + '" loading="lazy" onclick="playScene(' + s.id + ')"/>'
      + '<div class="play-overlay" onclick="playScene(' + s.id + ')"><div class="play-circle">'
      + '<svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div></div>'
      + '<span class="dur-badge">' + s.duration + 's</span>'
      + aiBadge
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
      + (s.has_transcript
          ? '<button class="vote-btn vtxt vote-btn-end" onclick="openTranscript(' + s.id + ')" title="Show transcript">'
            + '<svg viewBox="0 0 24 24"><path d="M4 4h16v2H4V4zm0 4h16v2H4V8zm0 4h10v2H4v-2zm0 4h16v2H4v-2zm0 4h10v2H4v-2z"/></svg>'
            + '</button>'
          : '')
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

function fmtT(s) {
  var m = Math.floor(s / 60);
  var sec = (s - m * 60).toFixed(1);
  if (sec.length === 3) sec = '0' + sec;
  return m + ':' + sec;
}

function escHtml(s) {
  return (s || '').replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function onSearchInput(value) {
  searchQuery = value;
  if (searchTimer) clearTimeout(searchTimer);
  document.getElementById('search-clear').style.display = value ? '' : 'none';
  if (!value.trim()) {
    searchHitIds = null;
    document.getElementById('search-status').textContent = '';
    renderGrid();
    return;
  }
  document.getElementById('search-status').textContent = 'Searching...';
  searchTimer = setTimeout(async function() {
    try {
      var r = await fetch('/rate/api/search?q=' + encodeURIComponent(searchQuery));
      var data = await r.json();
      searchHitIds = new Set(data.scene_ids || []);
      document.getElementById('search-status').textContent =
        searchHitIds.size + ' scene' + (searchHitIds.size !== 1 ? 's' : '') + ' match';
      renderGrid();
    } catch (e) {
      document.getElementById('search-status').textContent = 'search failed';
    }
  }, 250);
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  onSearchInput('');
}

async function openTranscript(sceneId) {
  var overlay = document.getElementById('tx-overlay');
  var body = document.getElementById('tx-body');
  var title = document.getElementById('tx-title');
  var range = document.getElementById('tx-range');
  body.innerHTML = '<div class="tx-empty">Loading...</div>';
  title.textContent = 'Transcript';
  range.textContent = '';
  overlay.classList.add('active');
  try {
    var r = await fetch('/rate/api/scene/' + sceneId + '/transcript');
    var data = await r.json();
    title.textContent = data.filename || 'Transcript';
    range.textContent = '[' + data.start.toFixed(1) + 's – ' + data.end.toFixed(1) + 's]';
    if (!data.groups || data.groups.length === 0) {
      body.innerHTML = '<div class="tx-empty">No transcript saved for this scene.<br>'
        + '<span style="font-size:11px">Run audio-mode analysis on this video to generate one.</span></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < data.groups.length; i++) {
      var g = data.groups[i];
      html += '<div class="tx-group' + (g.is_translation ? ' is-xlat' : '') + '">';
      html += '<div class="tx-group-label">' + escHtml(g.label) + '</div>';
      for (var j = 0; j < g.segments.length; j++) {
        var seg = g.segments[j];
        html += '<div class="tx-seg">'
          + '<span class="tx-seg-time">' + fmtT(seg.start) + '</span>'
          + '<span class="tx-seg-text">' + escHtml(seg.text) + '</span>'
          + '</div>';
      }
      html += '</div>';
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<div class="tx-empty">Failed to load transcript.</div>';
  }
}

function closeTranscript() {
  document.getElementById('tx-overlay').classList.remove('active');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { closePlayer(); closeTranscript(); }
});
document.getElementById('player-overlay').addEventListener('click', function(e) {
  if (e.target === this) closePlayer();
});
document.getElementById('tx-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeTranscript();
});

document.getElementById('search-input').addEventListener('input', function(e) {
  onSearchInput(e.target.value);
});
init();
</script>
</body>
</html>"""
