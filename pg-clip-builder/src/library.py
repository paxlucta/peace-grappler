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
    from chrome import inject_chrome
    return inject_chrome(LIBRARY_HTML, active="library")


@library_bp.route("/library/api/videos")
def library_videos():
    """Return all generated videos with metadata, caption, and feedback."""
    from db import get_db
    videos = get_all_generated_videos()
    conn = get_db()
    try:
        result = []
        for v in videos:
            path = Path(v["path"])
            # Extract tags from timeline clips
            tags = set()
            tl = v.get("timeline", [])
            # Normalize: new multitrack format stores a dict
            if isinstance(tl, dict):
                tl = tl.get("video_track", [])
            for item in tl:
                if isinstance(item, dict) and item.get("type") == "clip":
                    vf = item.get("video_file", "")
                    if vf:
                        tags.add(Path(vf).stem)
            # Get feedback
            fb_rows = conn.execute(
                "SELECT feedback FROM wizard_feedback "
                "WHERE generated_video_id=? ORDER BY created_at DESC",
                (v["id"],),
            ).fetchall()
            result.append({
                "id": v["id"],
                "filename": path.name,
                "path": v["path"],
                "duration": v["duration"],
                "generated_at": v["generated_at"],
                "exists": path.exists(),
                "tags": sorted(tags),
                "caption": v.get("caption", ""),
                "feedback": [r["feedback"] for r in fb_rows],
                "drive_link": v.get("drive_link") or "",
                "drive_file_id": v.get("drive_file_id") or "",
                "caption_provider": v.get("caption_provider") or "",
                "wizard_provider":  v.get("wizard_provider")  or "",
                "caption_model":    v.get("caption_model")    or "",
                "wizard_model":     v.get("wizard_model")     or "",
            })
        return jsonify(result)
    finally:
        conn.close()


@library_bp.route("/library/api/delete/<int:video_id>", methods=["POST"])
def library_delete_video(video_id):
    """Delete a generated video from DB and disk."""
    import os
    from db import get_db
    videos = get_all_generated_videos()
    video = next((v for v in videos if v["id"] == video_id), None)
    if not video:
        return jsonify({"error": "Not found"}), 404
    # Delete file from disk
    path = Path(video["path"])
    if path.exists():
        try:
            os.remove(str(path))
        except Exception:
            pass
    # Delete thumbnail
    thumb = _get_video_thumbnail(video["path"])
    if thumb and thumb.exists():
        try:
            os.remove(str(thumb))
        except Exception:
            pass
    # Delete from DB
    conn = get_db()
    try:
        conn.execute("DELETE FROM generated_videos WHERE id=?", (video_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


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
<title>ClipBuilder - Generated Videos</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:flex;flex-direction:column;height:100vh;overflow:hidden;
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

/* -- Folders column + video grid -- */
.content{flex:1;display:grid;grid-template-columns:200px 1fr;
  min-height:0;overflow:hidden;background:#0a0a0a;padding:0}
.bb-col{display:flex;flex-direction:column;min-height:0;
  border-right:1px solid #1a1a1a;background:#101013}
.bb-col:last-child{border-right:none;background:#0a0a0a}
.bb-col-head{
  padding:0 14px;border-bottom:1px solid #1f1f24;background:#141418;
  font-size:10px;color:#888;text-transform:uppercase;
  letter-spacing:.6px;font-weight:700;flex-shrink:0;
  height:34px;display:flex;align-items:center;justify-content:space-between;
}
.bb-col-head .bb-col-head-action{
  border:1px solid #2e2e3e;border-radius:4px;padding:0;
  width:18px;height:18px;cursor:pointer;flex-shrink:0;
  font-size:0;color:transparent;
  background:transparent center / 10px 10px no-repeat;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath stroke='%23aaa' stroke-width='2' stroke-linecap='round' d='M8 3v10M3 8h10'/%3E%3C/svg%3E");
}
.bb-col-head .bb-col-head-action:hover{
  border-color:#1976d2;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath stroke='%23fff' stroke-width='2' stroke-linecap='round' d='M8 3v10M3 8h10'/%3E%3C/svg%3E");
}
.bb-list{flex:1;overflow-y:auto;min-height:0}
.bb-row{
  display:flex;align-items:center;justify-content:space-between;
  gap:8px;padding:8px 12px;cursor:pointer;font-size:12px;color:#ccc;
  border-bottom:1px solid #161620;line-height:1.3;
}
.bb-row:hover{background:#1a1a24;color:#fff}
.bb-row.selected{background:#1f2a3a;color:#fff}
.bb-row.bb-row-all{font-weight:600;border-bottom:1px solid #2a2a3a}
.bb-row.bb-row-drag-ghost{opacity:.35;background:#1f2a3a}
.bb-row.bb-row-smart{color:#9ec0e8}
.bb-row.bb-row-smart.selected{background:#1f2a3a}
.bb-row .bb-row-smart-icon{
  width:14px;height:14px;flex-shrink:0;opacity:.7;
  display:inline-flex;align-items:center;justify-content:center;
}
.bb-row.bb-drop-target{outline:2px dashed #1976d2;outline-offset:-2px;background:#16223a}
.bb-row .bb-row-icon-btn{
  background:transparent;border:none;color:#666;cursor:pointer;
  padding:2px 4px;border-radius:3px;font-size:12px;line-height:1;
  opacity:0;transition:opacity .1s;
}
.bb-row:hover .bb-row-icon-btn{opacity:1}
.bb-row .bb-row-icon-btn:hover{color:#fff;background:#2a3548}
.bb-row .bb-row-icon-btn.bb-row-del:hover{background:#3a1a1a;color:#ef5350}
.bb-row-name{flex:1;min-width:0;word-break:break-word}
.bb-row-count{color:#666;font-size:10px;flex-shrink:0;font-family:'SF Mono',Menlo,monospace}
.bb-row.selected .bb-row-count{color:#9ec0e8}
.bb-videos-wrap{flex:1;overflow-y:auto;padding:16px;min-height:0}
.video-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;
}
.video-card[draggable="true"]{cursor:grab}
.video-card[draggable="true"]:active{cursor:grabbing}
.video-card{
  background:#1a1a1a;border-radius:10px;overflow:hidden;
  transition:transform .15s,box-shadow .15s;
}
.video-card:hover{transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.5)}
.video-card .thumb-wrap{
  position:relative;width:100%;aspect-ratio:9/16;background:#111;overflow:hidden;
}
.video-card .thumb-wrap video{
  width:100%;height:100%;object-fit:cover;display:block;background:#000;
}
.video-card .thumb-wrap .play-btn{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.4);opacity:0;transition:opacity .2s;cursor:pointer;
}
.video-card:hover .play-btn{opacity:1}
.video-card.is-playing .play-btn{display:none}
.play-btn .play-icon{
  width:56px;height:56px;background:rgba(229,57,53,.9);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  transition:transform .15s,background .15s;
}
.play-btn .play-icon:hover{transform:scale(1.1);background:#e53935}
.play-btn .play-icon svg{width:24px;height:24px;fill:#fff;margin-left:3px}
.video-card .card-ai-badges{
  position:absolute;bottom:8px;left:8px;display:flex;gap:3px;
  padding:2px 4px;border-radius:4px;
  background:rgba(0,0,0,.55);align-items:center;
}
/* Multi-select checkbox: first item in the toolbar (.actions row), no
   border — just a bare checkbox sized to read at the same scale as the
   circular vote-style buttons next to it. */
.video-card .card-check{
  width:32px;height:32px;
  display:inline-flex;align-items:center;justify-content:center;
  cursor:pointer;padding:0;flex-shrink:0;background:transparent;border:none;
}
.video-card .card-check input{
  width:16px;height:16px;margin:0;cursor:pointer;accent-color:#e53935;
}
.video-card.selected{outline:2px solid #e53935;outline-offset:-2px}

/* -- Bulk-action buttons in the toolbar -- */
.bulk-actions{display:flex;gap:6px;align-items:center}
.bulk-btn{
  background:#1a1a1a;color:#ddd;border:1px solid #333;border-radius:6px;
  padding:6px 12px;font-size:12px;cursor:pointer;
}
.bulk-btn:hover{border-color:#666;color:#fff}
.bulk-btn.danger{border-color:#e53935;color:#e53935}
.bulk-btn.danger:hover{background:#e53935;color:#fff}
.bulk-btn[hidden]{display:none}
/* Duration pill — top-right, matching the /builder clip-card .dur badge. */
.video-card .dur-badge{
  position:absolute;top:6px;right:6px;z-index:2;
  background:rgba(0,0,0,.75);color:#fff;font-size:10px;font-weight:600;
  padding:2px 5px;border-radius:3px;
}
.video-card .card-del{
  position:absolute;top:6px;right:6px;
  width:24px;height:24px;border-radius:50%;
  background:rgba(0,0,0,.6);border:1px solid rgba(255,255,255,.2);
  color:#fff;font-size:16px;line-height:22px;text-align:center;
  cursor:pointer;display:none;padding:0;z-index:2;
}
.video-card:hover .card-del{display:block}
.video-card .card-del:hover{background:#e53935;border-color:#e53935}
.video-card .meta{padding:10px 12px;cursor:pointer}
.video-card .meta-row{
  display:flex;align-items:center;gap:8px;
}
.video-card .meta .date{flex:1}
.video-card .actions{display:flex;gap:8px;align-items:center}
/* Toolbar buttons: pixel-identical to /builder's .clip-card .vote-btn —
   32×32 circle, 1.5px dark border, dark fill, scale(1.15) on hover. */
.video-card .act-btn{
  width:32px;height:32px;border-radius:50%;border:1.5px solid #444;
  background:#111;color:#666;cursor:pointer;transition:all .12s;
  display:flex;align-items:center;justify-content:center;padding:0;
}
.video-card .act-btn:hover{transform:scale(1.15);border-color:#888;color:#fff}
.video-card .act-btn.danger:hover{background:#e53935;border-color:#e53935;color:#fff}
.video-card .act-btn svg{width:16px;height:16px;fill:currentColor}

/* Share picker popup (rendered into document.body, positioned at runtime). */
.share-pop{
  position:absolute;z-index:300;background:#15151c;
  border:1px solid #2e2e3e;border-radius:8px;
  box-shadow:0 12px 32px rgba(0,0,0,.6);
  padding:4px;min-width:170px;display:none;
}
.share-pop.open{display:block}
.share-pop button{
  display:flex;align-items:center;gap:10px;width:100%;
  background:transparent;border:none;color:#ddd;
  padding:8px 10px;font-size:12px;text-align:left;cursor:pointer;border-radius:5px;
}
.share-pop button:hover{background:#1a1a24;color:#fff}
.share-pop svg{width:14px;height:14px;flex-shrink:0;fill:currentColor}
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
  align-items:center;justify-content:center;
}
.player-overlay.active{display:flex}
.player-layout{
  display:flex;gap:24px;max-width:95vw;max-height:90vh;align-items:flex-start;
}
.player-video-col{position:relative;flex-shrink:0}
.player-video-col video{
  max-width:50vw;max-height:85vh;border-radius:8px;background:#000;display:block;
}
.player-close{
  position:absolute;top:-36px;right:0;
  background:none;color:#fff;border:none;font-size:28px;
  cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s;
}
.player-close:hover{opacity:1}
.player-detail{
  width:340px;max-height:85vh;overflow-y:auto;flex-shrink:0;
}
.player-detail .pd-filename{font-size:15px;font-weight:600;color:#fff;margin-bottom:4px}
.player-detail .pd-meta{font-size:12px;color:#666;margin-bottom:12px}
.player-detail .pd-section{margin-bottom:14px}
.player-detail .pd-label{
  font-size:11px;color:#e53935;font-weight:600;text-transform:uppercase;margin-bottom:4px;
}
.player-detail .pd-caption{
  font-size:13px;color:#ccc;white-space:pre-wrap;line-height:1.5;
  background:#1a1a1a;border-radius:6px;padding:10px;
}
.player-detail .pd-no-caption{font-size:12px;color:#555;font-style:italic}
.player-detail .pd-tags{display:flex;gap:4px;flex-wrap:wrap}
.player-detail .pd-tags .ptag{
  font-size:10px;color:#888;background:#222;padding:2px 7px;border-radius:8px;
}
.player-detail .pd-feedback{
  font-size:12px;color:#818cf8;font-style:italic;margin-top:4px;
}
.player-detail .pd-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.pd-btn{
  display:inline-flex;align-items:center;gap:6px;
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:7px 14px;font-size:12px;cursor:pointer;transition:all .15s;
}
.pd-btn:hover{border-color:#666;color:#fff}
.pd-btn svg{width:16px;height:16px;fill:currentColor}
.pd-btn.ig{border-color:#c13584;color:#c13584}
.pd-btn.ig:hover{background:#c13584;color:#fff}
.pd-btn.ig:hover svg{fill:#fff}
.pd-btn.copy-btn.copied{border-color:#4caf50;color:#4caf50}
.pd-btn.del-btn{border-color:#e53935;color:#e53935}
.pd-btn.del-btn:hover{background:#e53935;color:#fff}
.pd-btn.del-btn:hover svg{fill:#fff}

@media (max-width:800px) {
  .player-layout{flex-direction:column;align-items:center}
  .player-video-col video{max-width:90vw}
  .player-detail{width:90vw}
}
</style>
</head>
<body>

<!-- pg-chrome -->

<div class="toolbar">
  <label>Sort by</label>
  <select id="sort-select" onchange="applyFilters();_pgSaveState()">
    <option value="date-desc">Newest First</option>
    <option value="date-asc">Oldest First</option>
    <option value="dur-desc">Longest First</option>
    <option value="dur-asc">Shortest First</option>
  </select>
  <div class="bulk-actions">
    <button type="button" class="bulk-btn" onclick="selectAllVisible()">Select all</button>
    <button type="button" class="bulk-btn" onclick="deselectAll()">Deselect all</button>
    <button type="button" class="bulk-btn danger" id="bulk-delete-btn"
            onclick="deleteSelected()" hidden>Delete videos</button>
  </div>
  <span class="video-count" id="video-count"></span>
</div>

<div class="content">
  <div class="bb-col">
    <div class="bb-col-head">
      Folders
      <button type="button" class="bb-col-head-action" title="New folder"
              onclick="bbCreateFolder()">+</button>
    </div>
    <div class="bb-list" id="bb-folders-list"></div>
  </div>
  <div class="bb-col">
    <div class="bb-col-head">Videos</div>
    <div class="bb-videos-wrap">
      <div class="video-grid" id="video-grid"></div>
      <div class="empty" id="empty-state" style="display:none">
        <h2>No reels yet</h2>
        <p>Generate one from the <a href="/wizard">AI</a> or
           <a href="/builder">Manual</a> builder and it'll show up here.</p>
      </div>
    </div>
  </div>
</div>

<div class="player-overlay" id="player-overlay">
  <div class="player-layout">
    <div class="player-video-col">
      <button class="player-close" onclick="closePlayer()">&times;</button>
      <video id="player-video" controls></video>
    </div>
    <div class="player-detail" id="player-detail"></div>
  </div>
</div>

<script>
var allVideos = [];
var selectedIds = new Set();
var lastFilteredIds = [];   // ids currently shown (drives Select-all scope)

// Folder column state (scope=library on the backend).
var bbFolders = { smart: [], user: [], memberships: {} };
var bbFolderFiles = null;     // Set<filename> for the currently-selected folder
var selectedFolder = 'all';

function _bbEsc(s) {
  return (s || '').replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

// Persisted UI state (folder + sort). Re-applied on every init so a
// user can jump between Generated Videos and other tabs without losing
// their place.
var _PG_STATE_KEY = 'pg.library.state';
function _pgSaveState() {
  try {
    localStorage.setItem(_PG_STATE_KEY, JSON.stringify({
      folder: selectedFolder,
      sort:   (document.getElementById('sort-select') || {}).value || '',
    }));
  } catch (e) {}
}
function _pgLoadState() {
  try {
    var s = JSON.parse(localStorage.getItem(_PG_STATE_KEY) || '{}');
    if (s && s.folder) selectedFolder = s.folder;
    if (s && s.sort) {
      var sel = document.getElementById('sort-select');
      if (sel) sel.value = s.sort;
    }
  } catch (e) {}
}

async function init() {
  var res = await fetch('/library/api/videos');
  allVideos = await res.json();

  _pgLoadState();
  await bbReloadFolders();
  bbRenderFolderCol();
  _bbWireColumnHandlers();
  applyFilters();
}

// ── Folder column ──────────────────────────────────────────────────────────

async function bbReloadFolders() {
  try {
    var r = await fetch('/api/folders/list?scope=library');
    bbFolders = await r.json();
  } catch (e) {
    bbFolders = { smart: [], user: [], memberships: {} };
  }
  _bbRecomputeFolderFiles();
}

function _bbRecomputeFolderFiles() {
  var all = (bbFolders.smart || []).concat(bbFolders.user || []);
  var match = all.find(function(f){ return f.id === selectedFolder; });
  if (!match) {
    selectedFolder = 'all';
    match = all.find(function(f){ return f.id === 'all'; });
  }
  bbFolderFiles = new Set(match ? match.files : []);
}

function bbRenderFolderCol() {
  var html = '';
  function rowHtml(f, isSmart) {
    var safe = _bbEsc(f.id);
    var name = _bbEsc(f.name);
    var sel  = selectedFolder === f.id ? ' selected' : '';
    var smartCls = isSmart ? ' bb-row-smart' : '';
    var dropAttr = isSmart ? '' : ' data-droptarget="1"';
    var actions = '';
    if (!isSmart) {
      actions = '<button class="bb-row-icon-btn" title="Rename" data-act="rename"'
              + ' data-fid="' + safe + '">&#9998;</button>'
              + '<button class="bb-row-icon-btn bb-row-del" title="Delete" data-act="delete"'
              + ' data-fid="' + safe + '">&times;</button>';
    }
    var icon = isSmart
      ? (f.id === 'all'   ? '<span class="bb-row-smart-icon">&#9776;</span>'
        : f.id === 'today' ? '<span class="bb-row-smart-icon">&#9728;</span>'
        : '<span class="bb-row-smart-icon">&#9733;</span>')
      : '<span class="bb-row-smart-icon">&#128193;</span>';
    return '<div class="bb-row' + sel + smartCls + '"'
         + ' data-fid="' + safe + '"' + dropAttr + '>'
         + icon
         + '<span class="bb-row-name">' + name + '</span>'
         + actions
         + '<span class="bb-row-count">' + (f.files ? f.files.length : 0) + '</span>'
         + '</div>';
  }
  (bbFolders.smart || []).forEach(function(f){ html += rowHtml(f, true); });
  (bbFolders.user || []).forEach(function(f){ html += rowHtml(f, false); });
  var list = document.getElementById('bb-folders-list');
  list.innerHTML = html;
  list.querySelectorAll('.bb-row-icon-btn').forEach(function(b){
    b.addEventListener('click', function(e){
      e.stopPropagation();
      var fid = b.getAttribute('data-fid');
      var act = b.getAttribute('data-act');
      if (act === 'rename') bbRenameFolder(fid);
      else if (act === 'delete') bbDeleteFolder(fid);
    });
  });
  _bbWireFolderSort(list);
}

var _bbFolderSort = null;
function _bbWireFolderSort(list) {
  if (_bbFolderSort) { try { _bbFolderSort.destroy(); } catch (e) {} _bbFolderSort = null; }
  if (typeof Sortable === 'undefined') return;
  _bbFolderSort = new Sortable(list, {
    animation: 120,
    draggable: '.bb-row',
    filter: '.bb-row-smart',
    preventOnFilter: false,
    ghostClass: 'bb-row-drag-ghost',
    onMove: function(evt) {
      if (!evt.related || !evt.dragged) return true;
      if (evt.dragged.classList.contains('bb-row-smart')) return false;
      if (evt.related.classList.contains('bb-row-smart')) return false;
      return true;
    },
    onEnd: function() {
      var order = [];
      list.querySelectorAll('.bb-row:not(.bb-row-smart)').forEach(function(el){
        var fid = el.getAttribute('data-fid');
        if (fid) order.push(fid);
      });
      if (bbFolders && Array.isArray(bbFolders.user)) {
        var byId = {};
        bbFolders.user.forEach(function(f){ byId[f.id] = f; });
        bbFolders.user = order.map(function(id){ return byId[id]; }).filter(Boolean);
      }
      fetch('/api/folders/reorder', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({order: order}),
      }).catch(function(){});
    },
  });
}

function _bbWireColumnHandlers() {
  var foldersList = document.getElementById('bb-folders-list');
  foldersList.addEventListener('click', function(e){
    if (e.target.closest('.bb-row-icon-btn')) return;
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectFolder(row.getAttribute('data-fid'));
  });
  foldersList.addEventListener('dragover', _bbFolderDragOver);
  foldersList.addEventListener('dragleave', _bbFolderDragLeave);
  foldersList.addEventListener('drop', _bbFolderDrop);
}

function bbSelectFolder(fid) {
  if (!fid) return;
  selectedFolder = fid;
  _pgSaveState();
  _bbRecomputeFolderFiles();
  bbRenderFolderCol();
  applyFilters();
}

async function bbCreateFolder() {
  var name = (window.prompt('New folder name:') || '').trim();
  if (!name) return;
  var r = await fetch('/api/folders', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name, scope: 'library'}),
  });
  if (!r.ok) { alert('Could not create folder.'); return; }
  await bbReloadFolders();
  bbRenderFolderCol();
}

async function bbRenameFolder(fid) {
  var current = ((bbFolders.user || []).find(function(f){return f.id===fid}) || {}).name || '';
  var name = (window.prompt('Rename folder:', current) || '').trim();
  if (!name || name === current) return;
  var r = await fetch('/api/folders/' + encodeURIComponent(fid) + '/rename', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name, scope: 'library'}),
  });
  if (!r.ok) { alert('Could not rename.'); return; }
  await bbReloadFolders();
  bbRenderFolderCol();
}

async function bbDeleteFolder(fid) {
  if (!confirm('Delete this folder? Videos inside will return to All Videos.')) return;
  var r = await fetch('/api/folders/' + encodeURIComponent(fid) + '?scope=library',
                     {method:'DELETE'});
  if (!r.ok) { alert('Could not delete.'); return; }
  if (selectedFolder === fid) selectedFolder = 'all';
  await bbReloadFolders();
  bbRenderFolderCol();
  applyFilters();
}

function _bbFolderDragOver(e) {
  var row = e.target.closest('.bb-row[data-droptarget="1"]');
  if (!row) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  row.classList.add('bb-drop-target');
}

function _bbFolderDragLeave(e) {
  var row = e.target.closest('.bb-row');
  if (row) row.classList.remove('bb-drop-target');
}

async function _bbFolderDrop(e) {
  var row = e.target.closest('.bb-row[data-droptarget="1"]');
  if (!row) return;
  e.preventDefault();
  row.classList.remove('bb-drop-target');
  var fn = e.dataTransfer.getData('application/x-pg-video')
        || e.dataTransfer.getData('text/plain');
  if (!fn) return;
  var fid = row.getAttribute('data-fid');
  var r = await fetch('/api/folders/membership', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({filename: fn, folder_id: fid, scope: 'library'}),
  });
  if (!r.ok) { alert('Move failed.'); return; }
  await bbReloadFolders();
  bbRenderFolderCol();
  applyFilters();
}


function applyFilters() {
  var sort = document.getElementById('sort-select').value;
  var filtered = allVideos.filter(function(v) { return v.exists; });

  // Folder filter
  if (bbFolderFiles) {
    filtered = filtered.filter(function(v) {
      return bbFolderFiles.has(v.filename);
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

  lastFilteredIds = filtered.map(function(v){ return v.id; });
  // Drop selections that are no longer visible.
  var visible = new Set(lastFilteredIds);
  selectedIds.forEach(function(id){ if (!visible.has(id)) selectedIds.delete(id); });

  renderGrid(filtered);
  document.getElementById('video-count').textContent = filtered.length + ' video' + (filtered.length !== 1 ? 's' : '');
  updateBulkUI();
}

function toggleSelect(id, on) {
  if (on) selectedIds.add(id); else selectedIds.delete(id);
  var card = document.querySelector('.video-card[data-id="' + id + '"]');
  if (card) card.classList.toggle('selected', on);
  updateBulkUI();
}

function selectAllVisible() {
  for (var i = 0; i < lastFilteredIds.length; i++) selectedIds.add(lastFilteredIds[i]);
  syncCheckboxes();
}

function deselectAll() {
  selectedIds.clear();
  syncCheckboxes();
}

function syncCheckboxes() {
  var boxes = document.querySelectorAll('.card-check input[data-id]');
  for (var i = 0; i < boxes.length; i++) {
    var id = parseInt(boxes[i].getAttribute('data-id'), 10);
    var on = selectedIds.has(id);
    boxes[i].checked = on;
    var card = boxes[i].closest('.video-card');
    if (card) card.classList.toggle('selected', on);
  }
  updateBulkUI();
}

function updateBulkUI() {
  var btn = document.getElementById('bulk-delete-btn');
  if (!btn) return;
  var n = selectedIds.size;
  btn.hidden = (n === 0);
  btn.textContent = n > 1 ? ('Delete ' + n + ' videos') : 'Delete video';
}

async function deleteSelected() {
  var ids = Array.from(selectedIds);
  if (!ids.length) return;
  var msg = ids.length === 1
    ? 'Delete this video permanently?'
    : 'Delete ' + ids.length + ' videos permanently?';
  if (!confirm(msg)) return;
  var btn = document.getElementById('bulk-delete-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  var failed = 0;
  for (var i = 0; i < ids.length; i++) {
    try {
      var r = await fetch('/library/api/delete/' + ids[i], {method:'POST'});
      var d = await r.json();
      if (!d.ok) failed++;
      else allVideos = allVideos.filter(function(v){ return v.id !== ids[i]; });
    } catch (e) { failed++; }
  }
  selectedIds.clear();
  if (btn) { btn.disabled = false; }
  applyFilters();
  if (failed) alert(failed + ' video' + (failed !== 1 ? 's' : '') + ' could not be deleted.');
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

    var wizModel = v.wizard_model || '';
    var capModel = v.caption_model || '';
    var wizBadge = (window.pgAiBadge && v.wizard_provider)
      ? window.pgAiBadge(v.wizard_provider, {
          size: 14, model: wizModel,
          title: 'Reel composed by ' + v.wizard_provider
                + (wizModel ? ' · ' + wizModel : ''),
        }) : '';
    var capBadge = (window.pgAiBadge && v.caption_provider
                     && (v.caption_provider !== v.wizard_provider
                         || capModel !== wizModel))
      ? window.pgAiBadge(v.caption_provider, {
          size: 12, model: capModel,
          title: 'Caption by ' + v.caption_provider
                + (capModel ? ' · ' + capModel : ''),
        }) : '';
    var isSel = selectedIds.has(v.id);
    var safeName = escHtml(v.filename);
    html += '<div class="video-card' + (isSel ? ' selected' : '') + '"'
      + ' draggable="true"'
      + ' data-id="' + v.id + '" data-fn="' + safeName + '">'
      + '<div class="thumb-wrap">'
      + '<video data-id="' + v.id + '" preload="none" playsinline'
      +   ' poster="/library/api/thumbnail/' + v.id + '"'
      +   ' src="/library/api/video/' + v.id + '"></video>'
      + '<div class="play-btn" onclick="playInline(' + v.id + ')">'
      + '<div class="play-icon"><svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div>'
      + '</div>'
      + '<span class="dur-badge">' + formatDuration(v.duration) + '</span>'
      + ((wizBadge || capBadge) ? '<span class="card-ai-badges">' + wizBadge + capBadge + '</span>' : '')
      + '</div>'
      + '<div class="meta" onclick="playVideo(' + v.id + ',\'' + safeName + '\')">'
      + '<div class="meta-row">'
      +   '<div class="date">' + formatDate(v.generated_at) + '</div>'
      +   '<div class="actions" onclick="event.stopPropagation()">'
      +     '<label class="card-check" title="Select">'
      +       '<input type="checkbox" data-id="' + v.id + '"' + (isSel ? ' checked' : '')
      +         ' onchange="toggleSelect(' + v.id + ', this.checked)">'
      +     '</label>'
      +     '<button class="act-btn" title="Share" onclick="shareToggle(event,' + v.id + ')">'
      +       '<svg viewBox="0 0 24 24"><path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92s2.92-1.31 2.92-2.92-1.31-2.92-2.92-2.92z"/></svg>'
      +     '</button>'
      +     '<button class="act-btn" title="Edit in Builder"'
      +       ' onclick="editInBuilder(\'' + safeName + '\')">'
      +       '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>'
      +     '</button>'
      +     '<button class="act-btn danger" title="Delete" onclick="deleteVideo(' + v.id + ')">'
      +       '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>'
      +     '</button>'
      +   '</div>'
      + '</div>'
      + '<div class="card-tags">' + tagsHtml + '</div>'
      + '</div>'
      + '</div>';
  }
  grid.innerHTML = html;
  // Bind dragstart once, idempotent: removeEventListener with the same
  // ref then re-add. Cards re-render often; the listener stays on the
  // (stable) grid container so we only need to do this once-per-render.
  if (!grid._pgDragBound) {
    grid.addEventListener('dragstart', function(e){
      var card = e.target.closest('.video-card[draggable="true"]');
      if (!card) return;
      var fn = card.getAttribute('data-fn');
      if (!fn) return;
      e.dataTransfer.setData('text/plain', fn);
      e.dataTransfer.setData('application/x-pg-video', fn);
      e.dataTransfer.effectAllowed = 'move';
    });
    grid._pgDragBound = true;
  }
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function playInline(id) {
  // Pause any other inline videos currently playing.
  document.querySelectorAll('.video-card video').forEach(function(el){
    var card = el.closest('.video-card');
    if (!card || parseInt(card.getAttribute('data-id'),10) === id) return;
    if (!el.paused) el.pause();
    el.removeAttribute('controls');
    card.classList.remove('is-playing');
  });
  var card = document.querySelector('.video-card[data-id="' + id + '"]');
  if (!card) return;
  var vid = card.querySelector('video');
  if (!vid) return;
  vid.setAttribute('controls', '');
  card.classList.add('is-playing');
  var p = vid.play();
  if (p && typeof p.catch === 'function') p.catch(function(){});
}

function playVideo(id, filename) {
  var v = allVideos.find(function(x) { return x.id === id; });
  if (!v) return;

  var overlay = document.getElementById('player-overlay');
  var video = document.getElementById('player-video');
  var detail = document.getElementById('player-detail');

  video.src = '/library/api/video/' + id;

  // Build detail panel
  var _wzm = v.wizard_model || '';
  var wizBadge = (window.pgAiBadge && v.wizard_provider)
    ? ' ' + window.pgAiBadge(v.wizard_provider, {
        size: 13, model: _wzm,
        title: 'Reel composed by ' + v.wizard_provider
              + (_wzm ? ' · ' + _wzm : ''),
      }) : '';
  var html = '<div class="pd-filename">' + escHtml(v.filename) + '</div>'
    + '<div class="pd-meta">' + formatDuration(v.duration) + ' &middot; ' + formatDate(v.generated_at) + wizBadge + '</div>';

  // Tags
  if (v.tags.length) {
    html += '<div class="pd-section"><div class="pd-label">Tags</div><div class="pd-tags">';
    for (var i = 0; i < v.tags.length; i++) {
      html += '<span class="ptag">' + escHtml(v.tags[i]) + '</span>';
    }
    html += '</div></div>';
  }

  // Caption
  var _cpm = v.caption_model || '';
  var capBadge = (window.pgAiBadge && v.caption_provider)
    ? ' ' + window.pgAiBadge(v.caption_provider, {
        size: 12, model: _cpm,
        title: 'Caption written by ' + v.caption_provider
              + (_cpm ? ' · ' + _cpm : ''),
      }) : '';
  html += '<div class="pd-section"><div class="pd-label">Caption' + capBadge + '</div>';
  if (v.caption) {
    html += '<div class="pd-caption" id="pd-caption-text">' + escHtml(v.caption) + '</div>';
  } else {
    html += '<div class="pd-no-caption">No caption generated</div>';
  }
  html += '</div>';

  // Feedback
  if (v.feedback && v.feedback.length) {
    html += '<div class="pd-section"><div class="pd-label">Feedback</div>';
    for (var j = 0; j < v.feedback.length; j++) {
      html += '<div class="pd-feedback">"' + escHtml(v.feedback[j]) + '"</div>';
    }
    html += '</div>';
  }

  // Action buttons
  html += '<div class="pd-actions">';
  if (v.caption) {
    html += '<button class="pd-btn copy-btn" id="copy-cap-btn" onclick="copyCaption()">'
      + '<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>'
      + 'Copy Caption</button>';
  }
  html += '<button class="pd-btn" onclick="editInBuilder(\'' + escHtml(v.filename) + '\')">'
    + '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>'
    + 'Edit in Builder</button>';
  html += '<button class="pd-btn ig" onclick="postToInstagram(' + v.id + ')">'
    + '<svg viewBox="0 0 24 24"><path d="M7.8 2h8.4C19.4 2 22 4.6 22 7.8v8.4a5.8 5.8 0 0 1-5.8 5.8H7.8C4.6 22 2 19.4 2 16.2V7.8A5.8 5.8 0 0 1 7.8 2m-.2 2A3.6 3.6 0 0 0 4 7.6v8.8C4 18.39 5.61 20 7.6 20h8.8a3.6 3.6 0 0 0 3.6-3.6V7.6C20 5.61 18.39 4 16.4 4H7.6m9.65 1.5a1.25 1.25 0 0 1 1.25 1.25A1.25 1.25 0 0 1 17.25 8 1.25 1.25 0 0 1 16 6.75a1.25 1.25 0 0 1 1.25-1.25M12 7a5 5 0 0 1 5 5 5 5 0 0 1-5 5 5 5 0 0 1-5-5 5 5 0 0 1 5-5m0 2a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/></svg>'
    + 'Post to Instagram</button>';
  html += '<button class="pd-btn" onclick="emailFromLibrary(' + v.id + ',\'' + escHtml(v.filename) + '\')">'
    + '<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'
    + 'Email</button>';
  if (window.PG_FEATURES && window.PG_FEATURES.drive) {
    if (v.drive_link) {
      html += '<a class="pd-btn" href="' + v.drive_link + '" target="_blank" rel="noopener">'
        + '<svg viewBox="0 0 24 24"><path d="M7.71 3.5L1.15 15l3.42 6h7.85L8.85 14H22l-3.42-6h-7.86z" fill="#4285F4"/></svg>'
        + 'Open in Drive</a>';
    } else {
      html += '<button class="pd-btn" id="drive-up-' + v.id + '" onclick="uploadToDrive(' + v.id + ')">'
        + '<svg viewBox="0 0 24 24"><path d="M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z"/></svg>'
        + 'Upload to Drive</button>';
    }
  }
  html += '<button class="pd-btn del-btn" onclick="deleteVideo(' + v.id + ')">'
    + '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>'
    + 'Delete</button>';
  html += '</div>';

  detail.innerHTML = html;
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

function editInBuilder(filename) {
  window.location.href = '/builder?load=' + encodeURIComponent(filename);
}

function deleteVideo(id) {
  if (!confirm('Delete this video permanently?')) return;
  fetch('/library/api/delete/' + id, {method:'POST'})
    .then(function(r){return r.json()})
    .then(function(d) {
      if (d.ok) {
        allVideos = allVideos.filter(function(v){return v.id !== id});
        closePlayer();
        applyFilters();
      } else {
        alert('Delete failed');
      }
    });
}

function copyCaption() {
  var el = document.getElementById('pd-caption-text');
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(function() {
    var btn = document.getElementById('copy-cap-btn');
    btn.classList.add('copied');
    btn.innerHTML = btn.querySelector('svg').outerHTML + ' Copied!';
    setTimeout(function() {
      btn.classList.remove('copied');
      btn.innerHTML = btn.querySelector('svg').outerHTML + ' Copy Caption';
    }, 2000);
  });
}

function postToInstagram(videoId) {
  var v = allVideos.find(function(x) { return x.id === videoId; });
  if (!v) return;
  // Copy caption to clipboard
  if (v.caption) {
    navigator.clipboard.writeText(v.caption);
  }
  // Reveal video in Finder for easy upload
  fetch('/api/open', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: v.path, reveal: true}),
  });
  // Open Meta Business Suite (works from desktop for Reels publishing)
  window.open('https://business.facebook.com/latest/content_calendar', '_blank');
  alert((v.caption ? 'Caption copied to clipboard!\\n\\n' : '')
    + '1. Meta Business Suite opened (use Create Reel)\\n'
    + '2. Video revealed in Finder\\n'
    + '3. Upload the video and paste the caption (Cmd+V)');
}

async function uploadToDrive(videoId) {
  var btn = document.getElementById('drive-up-' + videoId);
  // Pre-flight: check Drive is configured
  var sc = await (await fetch('/drive/api/status')).json();
  if (!sc.has_token) {
    if (confirm('Google Drive is not connected. Open Drive settings?')) {
      window.location = '/drive';
    }
    return;
  }
  if (!sc.outbox_folder_id) {
    if (confirm('No Drive outbox folder configured. Open Drive settings?')) {
      window.location = '/drive';
    }
    return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = btn.querySelector('svg').outerHTML + ' Uploading...'; }
  var r = await fetch('/drive/api/push/' + videoId, {method:'POST'});
  if (r.status === 409) { if (btn) btn.disabled = false; return; }
  var poll = async function() {
    var s = await (await fetch('/drive/api/push/' + videoId + '/status')).json();
    if (window.pgLog && s.log && s.log.length) { window.pgLog('[drive] ' + s.log[s.log.length-1]); }
    if (!s.done) { setTimeout(poll, 1200); return; }
    if (s.ok && s.link) {
      // Update local cache and re-render
      var v = allVideos.find(function(x){return x.id===videoId});
      if (v) { v.drive_link = s.link; }
      // Re-open the player to refresh action buttons
      if (v) playVideo(v.id, v.filename);
    } else if (btn) {
      btn.disabled = false;
      btn.innerHTML = btn.querySelector('svg').outerHTML + ' Upload failed';
    }
  };
  poll();
}

// -- Share picker (per-card) --
var SHARE_TARGETS = [
  {key:'email',     label:'Email',
   svg:'<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'},
  {key:'imessage',  label:'iMessage',
   svg:'<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.04 2 11c0 2.52 1.16 4.79 3.03 6.39-.11 1.16-.5 2.62-1.43 3.61 1.71-.16 3.36-.91 4.71-1.91 1.16.34 2.4.52 3.69.52 5.52 0 10-4.04 10-9S17.52 2 12 2z"/></svg>'},
  {key:'instagram', label:'Instagram',
   svg:'<svg viewBox="0 0 24 24"><path d="M7.8 2h8.4C19.4 2 22 4.6 22 7.8v8.4a5.8 5.8 0 0 1-5.8 5.8H7.8C4.6 22 2 19.4 2 16.2V7.8A5.8 5.8 0 0 1 7.8 2m-.2 2A3.6 3.6 0 0 0 4 7.6v8.8C4 18.39 5.61 20 7.6 20h8.8a3.6 3.6 0 0 0 3.6-3.6V7.6C20 5.61 18.39 4 16.4 4H7.6m9.65 1.5a1.25 1.25 0 0 1 1.25 1.25A1.25 1.25 0 0 1 17.25 8 1.25 1.25 0 0 1 16 6.75a1.25 1.25 0 0 1 1.25-1.25M12 7a5 5 0 0 1 5 5 5 5 0 0 1-5 5 5 5 0 0 1-5-5 5 5 0 0 1 5-5m0 2a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/></svg>'},
  {key:'youtube',   label:'YouTube',
   svg:'<svg viewBox="0 0 24 24"><path d="M23 12s0-3.6-.46-5.33a2.78 2.78 0 0 0-1.96-1.96C18.85 4.25 12 4.25 12 4.25s-6.85 0-8.58.46A2.78 2.78 0 0 0 1.46 6.67C1 8.4 1 12 1 12s0 3.6.46 5.33c.25.94.97 1.66 1.96 1.91 1.73.46 8.58.46 8.58.46s6.85 0 8.58-.46c.99-.25 1.71-.97 1.96-1.91C23 15.6 23 12 23 12zM9.75 15.5v-7l6 3.5-6 3.5z"/></svg>'},
  {key:'tiktok',    label:'TikTok',
   svg:'<svg viewBox="0 0 24 24"><path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-5.2 1.74 2.89 2.89 0 0 1 2.31-4.64 2.93 2.93 0 0 1 .88.13V9.4a6.84 6.84 0 0 0-1-.05A6.33 6.33 0 0 0 5.8 20.1a6.34 6.34 0 0 0 10.86-4.43V9.07a8.32 8.32 0 0 0 4.86 1.55V7.18a4.85 4.85 0 0 1-1.93-.49z"/></svg>'},
];

function shareToggle(ev, id) {
  ev.stopPropagation();
  var existing = document.getElementById('share-pop');
  if (existing && existing.classList.contains('open')
      && existing.dataset.id === String(id)) {
    existing.classList.remove('open');
    return;
  }
  var pop = existing;
  if (!pop) {
    pop = document.createElement('div');
    pop.id = 'share-pop';
    pop.className = 'share-pop';
    document.body.appendChild(pop);
  }
  pop.dataset.id = String(id);
  var html = '';
  for (var i = 0; i < SHARE_TARGETS.length; i++) {
    var t = SHARE_TARGETS[i];
    html += '<button onclick="shareTo(\'' + t.key + '\',' + id + ')">'
      + t.svg + '<span>' + t.label + '</span></button>';
  }
  pop.innerHTML = html;
  pop.classList.add('open');
  // Position below the button, right-aligned to it.
  var btn = ev.currentTarget;
  var r = btn.getBoundingClientRect();
  var popW = pop.offsetWidth || 170;
  var left = window.scrollX + r.right - popW;
  if (left < 8) left = 8;
  pop.style.top = (window.scrollY + r.bottom + 4) + 'px';
  pop.style.left = left + 'px';
}

document.addEventListener('click', function(e) {
  var pop = document.getElementById('share-pop');
  if (!pop || !pop.classList.contains('open')) return;
  if (pop.contains(e.target)) return;
  pop.classList.remove('open');
});

async function shareTo(target, id) {
  var pop = document.getElementById('share-pop');
  if (pop) pop.classList.remove('open');
  var v = allVideos.find(function(x){ return x.id === id; });
  if (!v) return;

  if (target === 'email')     { emailFromLibrary(id, v.filename); return; }
  if (target === 'instagram') { postToInstagram(id);              return; }

  // For iMessage / YouTube / TikTok: copy caption, reveal file in Finder,
  // open the app/site so the user can drop the file into the upload area.
  if (v.caption) {
    try { await navigator.clipboard.writeText(v.caption); } catch (e) {}
  }
  fetch('/api/open', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: v.path, reveal: true}),
  });
  var urls = {
    imessage: 'messages://',
    youtube:  'https://studio.youtube.com/',
    tiktok:   'https://www.tiktok.com/upload',
  };
  var labels = {imessage:'Messages', youtube:'YouTube Studio', tiktok:'TikTok'};
  if (urls[target]) window.open(urls[target], '_blank');
  alert((v.caption ? 'Caption copied to clipboard.\n\n' : '')
    + labels[target] + ' opened. Drag the video from Finder into the upload area.');
}

async function emailFromLibrary(videoId, filename) {
  await fetch('/wizard/api/email/' + videoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subject: ((window.PG_APP && window.PG_APP.brand) || 'Video')
                                    + ' Video - ' + filename}),
  });
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
