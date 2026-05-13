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
  display:flex;flex-direction:column;height:100vh;overflow:hidden;
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

.content{flex:1;padding:0;display:flex;flex-direction:column;min-height:0}

/* -- Mac-Finder-style 3-column scene browser (mirrors /builder) -- */
.bb-search-row{
  display:flex;align-items:center;gap:12px;
  padding:8px 20px;flex-shrink:0;
  background:#111;border-bottom:1px solid #2a2a2a;
}
.bb-search-row input{
  flex:1;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;
  border-radius:6px;padding:7px 12px;font-size:13px;outline:none;
}
.bb-search-row input:focus{border-color:#1976d2}
.bb-search-row .bb-search-status{font-size:11px;color:#1976d2;font-weight:600;white-space:nowrap}
.bb-search-row .bb-search-clear{
  background:#1a1a1a;border:1px solid #333;color:#888;
  border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer;
}
.bb-search-row .bb-search-clear:hover{color:#fff;border-color:#666}
.bb-cols{
  flex:1;display:grid;grid-template-columns:240px 220px 1fr;
  min-height:0;overflow:hidden;background:#0a0a0a;
}
.bb-col{
  display:flex;flex-direction:column;min-height:0;
  border-right:1px solid #1a1a1a;background:#101013;
}
.bb-col:last-child{border-right:none;background:#0a0a0a}
.bb-col-head{
  padding:8px 14px;border-bottom:1px solid #1f1f24;background:#141418;
  font-size:10px;color:#888;text-transform:uppercase;
  letter-spacing:.6px;font-weight:700;flex-shrink:0;
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
.bb-row.bb-row-unrated{color:#ffb74d}
.bb-row.bb-row-unrated.selected{background:#3a2a14;color:#ffd28a}
.bb-row.bb-row-hidden{color:#ef9a9a}
.bb-row.bb-row-hidden.selected{background:#3a1a1a;color:#ffc4c4}
.bb-row-name{flex:1;min-width:0;word-break:break-word}
.bb-row-count{color:#666;font-size:10px;flex-shrink:0;font-family:'SF Mono',Menlo,monospace}
.bb-row.selected .bb-row-count{color:#9ec0e8}
.bb-scenes-wrap{flex:1;overflow-y:auto;padding:12px;min-height:0}
.bb-scenes-head-info{font-size:11px;color:#666;font-weight:400;margin-left:8px;text-transform:none;letter-spacing:0}

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

/* -- File filter (multi-select dropdown, mirrors Builder/Wizard) -- */
.file-filter{position:relative;display:inline-block}
.file-filter > button{
  display:inline-flex;align-items:center;gap:6px;
  background:#1a1a1a;color:#e0e0e0;border:1px solid #333;
  border-radius:6px;padding:7px 10px;font-size:13px;cursor:pointer;
}
.file-filter > button:hover{border-color:#666}
.file-filter .ff-caret{font-size:10px;color:#888}
.ff-pop{
  display:none;position:absolute;top:calc(100% + 4px);left:0;z-index:200;
  width:max-content;min-width:320px;max-width:min(90vw,900px);
  background:#15151c;border:1px solid #2e2e3e;border-radius:8px;
  box-shadow:0 12px 32px rgba(0,0,0,.6);padding:10px;
}
.ff-pop.open{display:block}
.ff-actions{display:flex;gap:6px;margin-bottom:8px}
.ff-actions button{
  flex:1;font-size:11px;padding:5px 10px;background:#1a1a1a;
  border:1px solid #333;color:#ddd;border-radius:4px;cursor:pointer;
}
.ff-actions button:hover{border-color:#666;color:#fff}
#ff-search{
  width:100%;padding:6px 8px;background:#0c0c14;border:1px solid #2e2e3e;
  color:#eee;border-radius:4px;font-size:12px;margin-bottom:8px;
  box-sizing:border-box;outline:none;
}
#ff-search:focus{border-color:#1976d2}
.ff-list{max-height:280px;overflow-y:auto;display:flex;flex-direction:column}
.ff-item{
  display:flex;align-items:center;gap:8px;padding:5px 6px;
  border-radius:4px;cursor:pointer;font-size:12px;color:#ddd;
}
.ff-item:hover{background:#1a1a24}
.ff-item input{accent-color:#e53935;flex-shrink:0}
.ff-item .ff-name{flex:1;white-space:normal;word-break:break-word;line-height:1.35}
.ff-item .ff-count{color:#666;font-size:10px;flex-shrink:0}

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
  transition:width .2s;
}
/* Wider modal when the user picks "Both" so the two columns get room. */
.tx-modal.is-both{width:min(1080px,96vw)}
.tx-both-cols{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:8px}
.tx-both-cols > .tx-col{min-width:0}
.tx-col-label{
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  color:#1976d2;padding:6px 0;border-bottom:1px solid #1f1f1f;margin-bottom:4px;
}
.tx-col.is-xlat .tx-col-label{color:#4caf50}
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
.tx-body{padding:0 18px 18px;overflow-y:auto;flex:1}
.tx-toolbar{
  position:sticky;top:0;z-index:2;background:#161616;
  padding:10px 18px;margin:0 -18px;border-bottom:1px solid #242424;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
}
.tx-lang-toggle{display:inline-flex;background:#0c0c14;
  border:1px solid #2e2e3e;border-radius:6px;overflow:hidden}
.tx-lang-toggle button{
  background:transparent;border:none;color:#aaa;
  padding:6px 12px;font-size:11px;font-weight:600;cursor:pointer;
  text-transform:uppercase;letter-spacing:.5px;
}
.tx-lang-toggle button:hover:not(:disabled){color:#fff;background:#1a1a24}
.tx-lang-toggle button.active{background:#1976d2;color:#fff}
.tx-lang-toggle button:disabled{color:#444;cursor:not-allowed}
.tx-search-wrap{flex:1;min-width:160px;position:relative}
.tx-search{
  width:100%;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;
  border-radius:6px;font-size:12px;padding:6px 28px 6px 10px;outline:none;
}
.tx-search:focus{border-color:#1976d2}
.tx-search-count{
  position:absolute;right:8px;top:50%;transform:translateY(-50%);
  font-size:10px;color:#777;pointer-events:none;
}
.tx-group{margin-top:14px}
.tx-group:first-child{margin-top:10px}
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
.tx-seg-text mark{
  background:#ffeb3b;color:#111;border-radius:2px;padding:0 1px;
}
.tx-seg-text mark.active{background:#ff9800;color:#fff}
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
  <div class="bb-search-row">
    <input id="search-input" type="search"
           placeholder="Search scene transcripts… (e.g. a phrase the speaker said)"
           autocomplete="off">
    <span class="bb-search-status" id="search-status"></span>
    <button class="bb-search-clear" id="search-clear" onclick="clearSearch()" style="display:none">Clear</button>
  </div>
  <div class="bb-cols">
    <div class="bb-col">
      <div class="bb-col-head">Files</div>
      <div class="bb-list" id="bb-files-list"></div>
    </div>
    <div class="bb-col">
      <div class="bb-col-head">Tags</div>
      <div class="bb-list" id="bb-tags-list"></div>
    </div>
    <div class="bb-col">
      <div class="bb-col-head">Scenes
        <span class="bb-scenes-head-info" id="scene-count"></span>
      </div>
      <div class="bb-scenes-wrap">
        <div class="scene-grid" id="scene-grid"></div>
      </div>
    </div>
  </div>
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
    <div class="tx-body" id="tx-body">
      <div class="tx-toolbar" id="tx-toolbar" style="display:none">
        <div class="tx-lang-toggle" id="tx-lang-toggle">
          <button type="button" data-mode="native"  onclick="setTxMode('native')">Native</button>
          <button type="button" data-mode="english" onclick="setTxMode('english')">English</button>
          <button type="button" data-mode="both"    onclick="setTxMode('both')">Both</button>
        </div>
        <div class="tx-search-wrap">
          <input type="search" class="tx-search" id="tx-search"
                 placeholder="Search within transcript&hellip;"
                 oninput="onTxSearchInput()" autocomplete="off">
          <span class="tx-search-count" id="tx-search-count"></span>
        </div>
      </div>
      <div id="tx-content"></div>
    </div>
  </div>
</div>

<script>
var allScenes = [];
var searchQuery = '';        // current text query
var searchHitIds = null;     // null = no search active; Set of scene_ids when active
var searchTimer = null;

// Mac-Finder-style column state.
//   selectedFile: null = "All Files", else exact filename
//   selectedTag : null = "All Tags",  else a real tag or a pseudo-tag
//                 ('__unrated__' | '__hidden__')
var selectedFile = null;
var selectedTag  = null;

// Strip extension + turn underscores/dashes into spaces for display.
function _ffPretty(name) {
  var base = (name || '').replace(/\.[^.]+$/, '');
  return base.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function _bbEsc(s) {
  return (s || '').replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

async function init() {
  allScenes = await fetch('/rate/api/scenes').then(function(r){return r.json()});
  bbInit();
  renderGrid();
}

function bbInit() {
  bbRenderFilesCol();
  bbRenderTagsCol();
  // Delegated listeners — one per column. Avoids escaping filenames /
  // tags into inline onclick strings.
  document.getElementById('bb-files-list').addEventListener('click', function(e){
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectFile(row.getAttribute('data-fn'));
  });
  document.getElementById('bb-tags-list').addEventListener('click', function(e){
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectTag(row.getAttribute('data-tag'));
  });
}

function bbRenderFilesCol() {
  // Match the visibility rules of the default grid: hide excluded scenes
  // from the per-file counts unless the user is explicitly looking at the
  // Hidden pseudo-tag. Counts shouldn't include them otherwise.
  var includeExcluded = (selectedTag === '__hidden__');
  var counts = {};
  var totalAll = 0;
  for (var i = 0; i < allScenes.length; i++) {
    var s = allScenes[i];
    if (!includeExcluded && s.excluded) continue;
    var fn = s.filename || '';
    if (!fn) continue;
    counts[fn] = (counts[fn] || 0) + 1;
    totalAll++;
  }
  var files = Object.keys(counts).sort(function(a, b){
    return _ffPretty(a).localeCompare(_ffPretty(b));
  });
  var html = '';
  html += '<div class="bb-row bb-row-all' + (selectedFile===null?' selected':'') + '"'
       + ' data-fn=""><span class="bb-row-name">All Files</span>'
       + '<span class="bb-row-count">' + totalAll + '</span></div>';
  for (var i = 0; i < files.length; i++) {
    var f = files[i];
    var safe = _bbEsc(f);
    var pretty = _bbEsc(_ffPretty(f));
    html += '<div class="bb-row' + (selectedFile===f?' selected':'') + '"'
         + ' data-fn="' + safe + '" title="' + safe + '">'
         + '<span class="bb-row-name">' + pretty + '</span>'
         + '<span class="bb-row-count">' + counts[f] + '</span></div>';
  }
  document.getElementById('bb-files-list').innerHTML = html;
}

function bbRenderTagsCol() {
  // Tag list is computed against the currently-selected file (or all
  // scenes when no file is picked). Counts always exclude hidden scenes
  // — except for the dedicated Hidden row, which exists precisely so
  // the user can dig into them.
  var poolForTags = allScenes.filter(function(s){
    return !s.excluded
        && (selectedFile === null || s.filename === selectedFile);
  });
  var tagCounts = {};
  for (var i = 0; i < poolForTags.length; i++) {
    var ts = poolForTags[i].tags || [];
    for (var j = 0; j < ts.length; j++) {
      tagCounts[ts[j]] = (tagCounts[ts[j]] || 0) + 1;
    }
  }
  // Unrated / Hidden counts respect the file selection too.
  var unrated = 0, hidden = 0;
  for (var i = 0; i < allScenes.length; i++) {
    var s = allScenes[i];
    if (selectedFile && s.filename !== selectedFile) continue;
    if (s.excluded) { hidden++; continue; }
    if (s.status === 'unrated') unrated++;
  }
  var html = '';
  html += '<div class="bb-row bb-row-all' + (selectedTag===null?' selected':'') + '"'
       + ' data-tag=""><span class="bb-row-name">All Tags</span>'
       + '<span class="bb-row-count">' + poolForTags.length + '</span></div>';
  if (unrated > 0) {
    html += '<div class="bb-row bb-row-unrated' + (selectedTag==='__unrated__'?' selected':'') + '"'
         + ' data-tag="__unrated__"><span class="bb-row-name">Unrated</span>'
         + '<span class="bb-row-count">' + unrated + '</span></div>';
  }
  if (hidden > 0) {
    html += '<div class="bb-row bb-row-hidden' + (selectedTag==='__hidden__'?' selected':'') + '"'
         + ' data-tag="__hidden__"><span class="bb-row-name">Hidden</span>'
         + '<span class="bb-row-count">' + hidden + '</span></div>';
  }
  var tags = Object.keys(tagCounts).sort();
  for (var i = 0; i < tags.length; i++) {
    var t = tags[i];
    var safe = _bbEsc(t);
    html += '<div class="bb-row' + (selectedTag===t?' selected':'') + '"'
         + ' data-tag="' + safe + '">'
         + '<span class="bb-row-name">' + safe + '</span>'
         + '<span class="bb-row-count">' + tagCounts[t] + '</span></div>';
  }
  document.getElementById('bb-tags-list').innerHTML = html;
}

function bbSelectFile(fn) {
  selectedFile = fn || null;
  // If the previously-selected tag isn't present in the new file's
  // pool, fall back to All Tags so the grid never goes silently empty.
  if (selectedTag && selectedTag !== '__hidden__' && selectedTag !== '__unrated__') {
    var pool = allScenes.filter(function(s){
      return !s.excluded
          && (selectedFile === null || s.filename === selectedFile);
    });
    var has = pool.some(function(s){
      return (s.tags || []).indexOf(selectedTag) >= 0;
    });
    if (!has) selectedTag = null;
  }
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
}

function bbSelectTag(t) {
  selectedTag = t || null;
  // The Hidden pseudo-tag changes which scenes the files column counts,
  // so re-render that too. (No-op when switching between regular tags.)
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
}

function getFiltered() {
  var base;
  if (selectedTag === '__unrated__') {
    base = allScenes.filter(function(s) { return s.status === 'unrated' && !s.excluded; });
  } else if (selectedTag === '__hidden__') {
    base = allScenes.filter(function(s) { return s.excluded; });
  } else if (selectedTag) {
    // Auto-hidden tags refer to excluded scenes; surface them so the row
    // isn't empty when picked.
    var includeExcluded = (selectedTag === 'auto-hidden');
    base = allScenes.filter(function(s) {
      return s.tags.indexOf(selectedTag) >= 0
          && (includeExcluded || !s.excluded);
    });
  } else {
    base = allScenes.filter(function(s) { return !s.excluded; });
  }
  if (selectedFile) {
    base = base.filter(function(s) { return s.filename === selectedFile; });
  }
  if (searchHitIds) {
    base = base.filter(function(s) { return searchHitIds.has(s.id); });
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
  // After a vote, counts in both columns may shift (e.g. now hidden).
  bbRenderFilesCol();
  bbRenderTagsCol();
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

// -- Transcript modal state --
var _txData = null;            // last loaded {groups, ...}
var _txMode = 'both';           // 'native' | 'english' | 'both' — default both when both exist
var _txHasNative = false;
var _txHasEnglish = false;

async function openTranscript(sceneId) {
  var overlay = document.getElementById('tx-overlay');
  var content = document.getElementById('tx-content');
  var toolbar = document.getElementById('tx-toolbar');
  var title = document.getElementById('tx-title');
  var range = document.getElementById('tx-range');
  content.innerHTML = '<div class="tx-empty">Loading...</div>';
  toolbar.style.display = 'none';
  title.textContent = 'Transcript';
  range.textContent = '';
  // Reset the in-modal search so it doesn't carry over from a prior open.
  var s = document.getElementById('tx-search');
  if (s) s.value = '';
  document.getElementById('tx-search-count').textContent = '';
  overlay.classList.add('active');
  try {
    var r = await fetch('/rate/api/scene/' + sceneId + '/transcript');
    var data = await r.json();
    _txData = data;
    title.textContent = data.filename || 'Transcript';
    range.textContent = '[' + data.start.toFixed(1) + 's – ' + data.end.toFixed(1) + 's]';
    if (!data.groups || data.groups.length === 0) {
      content.innerHTML = '<div class="tx-empty">No transcript saved for this scene.<br>'
        + '<span style="font-size:11px">Run audio-mode analysis on this video to generate one.</span></div>';
      return;
    }
    // Detect which versions exist. is_translation=true is the
    // English-translated pass; is_translation=false is the original
    // (which may itself be English when the source spoke English).
    _txHasNative  = data.groups.some(function(g){ return !g.is_translation; });
    _txHasEnglish = data.groups.some(function(g){ return g.is_translation; })
                  || data.groups.some(function(g){
                       return !g.is_translation
                              && (g.language || '').toLowerCase() === 'en';
                     });
    // Default: native wins. If only English exists, show English.
    // Default: Both when we have both versions, otherwise whichever exists.
    _txMode = (_txHasNative && _txHasEnglish) ? 'both'
            : (_txHasNative ? 'native'
            : (_txHasEnglish ? 'english' : 'native'));
    syncLangToggle();
    toolbar.style.display = '';
    renderTxContent();
  } catch (e) {
    content.innerHTML = '<div class="tx-empty">Failed to load transcript.</div>';
  }
}

function syncLangToggle() {
  var btns = document.querySelectorAll('#tx-lang-toggle button');
  for (var i = 0; i < btns.length; i++) {
    var b = btns[i];
    var m = b.getAttribute('data-mode');
    var has = (m === 'native')  ? _txHasNative
            : (m === 'english') ? _txHasEnglish
            : (_txHasNative && _txHasEnglish);
    b.disabled = !has;
    b.classList.toggle('active', m === _txMode);
  }
}

function setTxMode(mode) {
  if (!_txData) return;
  _txMode = mode;
  syncLangToggle();
  renderTxContent();
}

function _txGroupsForMode() {
  if (!_txData || !_txData.groups) return [];
  if (_txMode === 'both') return _txData.groups;
  // 'native' → originals (is_translation=false). If a video's source was
  // English, the "native" view is its English original.
  if (_txMode === 'native') {
    return _txData.groups.filter(function(g){ return !g.is_translation; });
  }
  // 'english' → translated pass when present; otherwise English originals.
  var xlat = _txData.groups.filter(function(g){ return g.is_translation; });
  if (xlat.length) return xlat;
  return _txData.groups.filter(function(g){
    return !g.is_translation && (g.language || '').toLowerCase() === 'en';
  });
}

function _txHighlight(text, query) {
  // Escape HTML first; then re-find matches in the escaped string and
  // wrap them in <mark>. Returns {html, count}.
  var esc = escHtml(text);
  if (!query) return {html: esc, count: 0};
  var qEsc = escHtml(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  var re = new RegExp(qEsc, 'gi');
  var count = 0;
  var html = esc.replace(re, function(m){ count++; return '<mark>' + m + '</mark>'; });
  return {html: html, count: count};
}

// Build the inner HTML for one stack of groups (segments only — no column
// chrome). Returns {html, count} so the caller can sum highlight counts.
function _txBuildGroupsHtml(groups, q) {
  var totalHits = 0;
  var html = '';
  for (var i = 0; i < groups.length; i++) {
    var g = groups[i];
    html += '<div class="tx-group' + (g.is_translation ? ' is-xlat' : '') + '">';
    html += '<div class="tx-group-label">' + escHtml(g.label) + '</div>';
    for (var j = 0; j < g.segments.length; j++) {
      var seg = g.segments[j];
      var r = _txHighlight(seg.text, q);
      totalHits += r.count;
      html += '<div class="tx-seg">'
        + '<span class="tx-seg-time">' + fmtT(seg.start) + '</span>'
        + '<span class="tx-seg-text">' + r.html + '</span>'
        + '</div>';
    }
    html += '</div>';
  }
  return {html: html, count: totalHits};
}

function renderTxContent() {
  var content = document.getElementById('tx-content');
  if (!content || !_txData) return;
  // Toggle a wide modal class so the side-by-side view has room.
  var modal = document.querySelector('.tx-modal');
  if (modal) modal.classList.toggle('is-both', _txMode === 'both');

  var q = (document.getElementById('tx-search') || {}).value || '';
  var totalHits = 0;
  var html = '';

  if (_txMode === 'both') {
    // Split groups into native (left) and English (right). The "English"
    // side prefers true translations; if none exist, fall back to English
    // originals so the column isn't empty when the source was English.
    var nativeGroups = _txData.groups.filter(function(g){ return !g.is_translation; });
    var engGroups = _txData.groups.filter(function(g){ return g.is_translation; });
    if (!engGroups.length) {
      engGroups = _txData.groups.filter(function(g){
        return !g.is_translation && (g.language || '').toLowerCase() === 'en';
      });
    }
    if (!nativeGroups.length && !engGroups.length) {
      content.innerHTML = '<div class="tx-empty">No transcript in this view.</div>';
      document.getElementById('tx-search-count').textContent = '';
      return;
    }
    var L = _txBuildGroupsHtml(nativeGroups, q);
    var R = _txBuildGroupsHtml(engGroups,    q);
    totalHits = L.count + R.count;
    var nLang = (nativeGroups[0] && nativeGroups[0].language)
                ? ' [' + escHtml(nativeGroups[0].language) + ']' : '';
    var eLang = (engGroups[0] && engGroups[0].language
                 && engGroups[0].language.toLowerCase() !== 'en')
                ? ' (translated from ' + escHtml(engGroups[0].language) + ')'
                : '';
    html = '<div class="tx-both-cols">'
      +     '<div class="tx-col">'
      +       '<div class="tx-col-label">Native' + nLang + '</div>'
      +       (L.html || '<div class="tx-empty">No native transcript.</div>')
      +     '</div>'
      +     '<div class="tx-col is-xlat">'
      +       '<div class="tx-col-label">English' + eLang + '</div>'
      +       (R.html || '<div class="tx-empty">No English transcript.</div>')
      +     '</div>'
      +   '</div>';
  } else {
    var groups = _txGroupsForMode();
    if (!groups.length) {
      content.innerHTML = '<div class="tx-empty">No transcript in this view.</div>';
      document.getElementById('tx-search-count').textContent = '';
      return;
    }
    var built = _txBuildGroupsHtml(groups, q);
    html = built.html;
    totalHits = built.count;
  }

  content.innerHTML = html;
  var countEl = document.getElementById('tx-search-count');
  if (q) {
    countEl.textContent = totalHits + ' match' + (totalHits !== 1 ? 'es' : '');
    var first = content.querySelector('mark');
    if (first) {
      first.classList.add('active');
      first.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  } else {
    countEl.textContent = '';
  }
}

function onTxSearchInput() {
  renderTxContent();
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
