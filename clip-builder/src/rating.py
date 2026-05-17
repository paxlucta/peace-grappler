"""rating.py — Scenes page for ClipBuilder."""

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
            "favorite": s.get("favorite", False),
            "status": status,
            "analyzer_provider": s.get("analyzer_provider") or "",
            "analyzer_model":    s.get("analyzer_model")    or "",
            "visual_analyzer_provider": s.get("visual_analyzer_provider") or "",
            "visual_analyzer_model":    s.get("visual_analyzer_model")    or "",
            "speech_analyzer_provider": s.get("speech_analyzer_provider") or "",
            "speech_analyzer_model":    s.get("speech_analyzer_model")    or "",
            "has_transcript": s["id"] in transcript_scene_ids,
            # Per-scene crop overrides (Builder gear → Scene Settings) —
            # surfaced so the /rate scene card can overlay the crop region
            # on its thumbnail.
            "crop_x_frac": s.get("crop_x_frac"),
            "free_crops": _parse_scene_free_crops(s.get("free_crops")),
        })
    return jsonify(result)


def _parse_scene_free_crops(raw):
    if not raw:
        return None
    try:
        import json as _json
        v = _json.loads(raw)
        return v if isinstance(v, list) and v else None
    except Exception:
        return None


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


@rating_bp.route("/rate/api/favorite", methods=["POST"])
def api_favorite():
    """Toggle the heart-icon favorite on a scene.

    Body: ``{scene_id, favorite?}``. If ``favorite`` is omitted, flips
    the stored value. Returns the new state."""
    from db import set_scene_favorite, get_scene_by_id
    data = request.json or {}
    scene_id = data.get("scene_id")
    if not scene_id:
        return jsonify({"error": "scene_id required"}), 400
    if "favorite" in data:
        new_state = bool(data.get("favorite"))
    else:
        cur = get_scene_by_id(scene_id)
        if not cur:
            return jsonify({"error": "scene not found"}), 404
        new_state = not bool(cur.get("favorite") or False)
    set_scene_favorite(scene_id, new_state)
    return jsonify({"ok": True, "scene_id": scene_id, "favorite": new_state})


@rating_bp.route("/rate/api/transcript/purge-duplicates", methods=["POST"])
def api_transcript_purge_duplicates():
    """Drop duplicate transcript groups for a video. Body: ``{video_id}``.

    Picks the freshest group per ``is_translation`` slot (judged by the
    largest row id) and deletes the rest. Used by the "Purge duplicates"
    button in the transcript modal to clean up legacy rows where the
    same translation pass got saved with mismatched ``language`` values
    in different runs."""
    from db import purge_duplicate_transcripts
    data = request.json or {}
    try:
        video_id = int(data.get("video_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "video_id required"}), 400
    res = purge_duplicate_transcripts(video_id)
    return jsonify({"ok": True, **res})


@rating_bp.route("/rate/api/transcript/<int:transcript_id>", methods=["POST"])
def api_transcript_edit(transcript_id):
    """In-place edit of a transcript row's text. Body: ``{text}``.

    The pristine original is preserved on the first edit so the user
    can revert later. Returns the post-edit row (incl. ``original_text``
    + the ``edited`` flag the UI uses to render diffs)."""
    from db import update_transcript_text, get_transcript_by_id
    data = request.json or {}
    new_text = (data.get("text") or "").strip()
    if not new_text:
        return jsonify({"error": "text required"}), 400
    if not update_transcript_text(transcript_id, new_text):
        return jsonify({"error": "transcript not found"}), 404
    row = get_transcript_by_id(transcript_id)
    return jsonify({
        "ok": True,
        "id": row["id"],
        "text": row["text"],
        "original_text": row.get("original_text"),
        "edited": row.get("original_text") is not None
                  and row["original_text"] != row["text"],
    })


@rating_bp.route("/rate/api/transcript/<int:transcript_id>/revert",
                 methods=["POST"])
def api_transcript_revert(transcript_id):
    """Restore the transcript row to its pristine original."""
    from db import revert_transcript_text, get_transcript_by_id
    if not revert_transcript_text(transcript_id):
        return jsonify({"error": "nothing to revert"}), 400
    row = get_transcript_by_id(transcript_id)
    return jsonify({
        "ok": True,
        "id": row["id"] if row else transcript_id,
        "text": row["text"] if row else "",
        "original_text": None,
        "edited": False,
    })


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
  flex:1;display:grid;grid-template-columns:200px 240px 220px 1fr;
  min-height:0;overflow:hidden;background:#0a0a0a;
}
.bb-col{
  display:flex;flex-direction:column;min-height:0;
  border-right:1px solid #1a1a1a;background:#101013;
}
.bb-col:last-child{border-right:none;background:#0a0a0a}
.bb-col-head{
  padding:0 14px;border-bottom:1px solid #1f1f24;background:#141418;
  font-size:10px;color:#888;text-transform:uppercase;
  letter-spacing:.6px;font-weight:700;flex-shrink:0;
  height:34px;display:flex;align-items:center;justify-content:space-between;
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

/* Folders column extras (mirrors /builder) */
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
.bb-row[draggable="true"]{cursor:grab}
.bb-row[draggable="true"]:active{cursor:grabbing}

/* -- Heart icon (file rows + scene cards) -- */
.pg-heart{
  background:transparent;border:none;padding:0;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;flex-shrink:0;color:#555;
}
.pg-heart svg{width:14px;height:14px;fill:currentColor}
.pg-heart:hover{color:#ef9a9a}
.pg-heart.on{color:#ef5350}
.pg-heart.on:hover{color:#e53935}
.bb-row .pg-heart{margin:0 2px}
/* Heart anchors to the LEFT edge of the vote-row (mirror of the
   transcript button on the right) so the thumbs-down / thumbs-up pair
   stays centered between them. */
.scene-card .vote-row .pg-heart{
  width:32px;height:32px;border-radius:50%;border:1.5px solid #444;
  background:#111;color:#666;
  display:flex;align-items:center;justify-content:center;
  transition:all .12s;cursor:pointer;padding:0;
}
.scene-card .vote-row .pg-heart:hover{
  transform:scale(1.15);color:#ef9a9a;border-color:#ef9a9a;
}
.scene-card .vote-row .pg-heart.on{
  background:#ef5350;border-color:#ef5350;color:#fff;
}
.scene-card .vote-row .pg-heart svg{width:16px;height:16px;fill:currentColor}

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
/* Crop flag — a small badge that sits to the left of the duration pill
   on scenes that have a per-scene crop saved (Builder gear → Scene
   Settings). Just an indicator; no region preview on the thumbnail. */
.scene-card .crop-flag{
  position:absolute;top:7px;right:38px;z-index:2;
  width:18px;height:18px;border-radius:3px;
  border:1px solid #2a2a2a;background:#181818;color:#90caf9;
  display:inline-flex;align-items:center;justify-content:center;
}
.scene-card .crop-flag svg{width:12px;height:12px;display:block}
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
/* AI provider icon — bottom-right of the thumbnail, hugged just above
   the vote-row so it's clearly inside the thumbnail area. Fixed 24px
   circle that centers whatever icon the pgAiBadge helper renders. The
   helper sizes its inner SVG via inline styles, so we don't override
   it here — we just give it a roomy centered container. */
.scene-card .scene-ai-badge{
  position:absolute;bottom:52px;right:6px;z-index:2;
  width:26px;height:26px;
  display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.7);padding:0;border-radius:50%;
}
.scene-card .dur-badge{
  position:absolute;top:6px;right:6px;
  background:rgba(0,0,0,.75);color:#fff;font-size:10px;font-weight:600;
  padding:2px 5px;border-radius:3px;
}
.scene-card .excluded-badge{
  position:absolute;top:6px;left:6px;
  background:rgba(229,57,53,.85);color:#fff;font-size:9px;font-weight:700;
  padding:1px 5px;border-radius:3px;
}
/* Wide-source indicator at top-left. A small 16:9 rectangle outline
   inside a dark pill, so it reads as "this source was widescreen."
   Hidden when the scene is excluded so the HIDDEN tag wins that corner. */
.scene-card .wide-badge{
  position:absolute;top:6px;left:6px;z-index:1;
  width:24px;height:18px;border-radius:4px;
  background:rgba(0,0,0,.7);
  display:none;align-items:center;justify-content:center;
  pointer-events:none;
}
.scene-card .wide-badge svg{
  width:14px;height:8px;
  fill:none;stroke:rgba(255,255,255,.9);stroke-width:1.5;
}
.scene-card.wide .wide-badge{display:flex}
.scene-card.excluded .wide-badge{display:none}
.scene-card .info{padding:4px 8px 2px}
.scene-card .fn{
  font-size:9px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.scene-card .tg{
  font-size:9px;color:#888;margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}

/* -- Thumbs row on each card -- */
/* Two flush clusters — heart+down on the left, transcript+gear on the
   right. Mirrors /builder's .clip-card .vote-row layout exactly. */
.scene-card .vote-row{
  display:flex;justify-content:space-between;gap:8px;padding:6px;
  border-top:1px solid #222;align-items:center;
}
.scene-card .vote-row .vote-left-cluster,
.scene-card .vote-row .vote-right-cluster{
  display:flex;gap:6px;align-items:center;
}
.scene-card .vote-row .vote-left-cluster .vote-btn:hover,
.scene-card .vote-row .vote-right-cluster .vote-btn:hover{transform:scale(1.15)}
.vote-btn.vgear{border-color:#555}
.vote-btn.vgear svg{fill:#888}
.vote-btn.vgear:hover{background:#555;border-color:#888}
.vote-btn.vgear:hover svg{fill:#fff}
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
      <div class="bb-col-head">
        Folders
        <button type="button" class="bb-col-head-action" title="New folder"
                onclick="bbCreateFolder()">+</button>
      </div>
      <div class="bb-list" id="bb-folders-list"></div>
    </div>
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
          <button type="button" data-mode="both"    onclick="setTxMode('both')" style="display:none">Both</button>
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
//   selectedFolder : smart-folder id or user-folder id; default "all".
//   selectedFile   : null = "All Files" within the folder, else filename.
//   selectedTag    : null = "All Tags",  else real tag or pseudo-tag
//                    ('__unrated__' | '__hidden__').
var selectedFolder = 'all';
var selectedFile = null;
var selectedTag  = null;
var bbFolders = { smart: [], user: [], memberships: {} };
var bbFolderFiles = null;   // Set<filename> for the currently-selected folder
var bbFileFavorites = new Set();   // Set<filename> from the favorites smart folder

// Shared heart-icon SVG (filled/outline are switched via .pg-heart.on
// in CSS — same icon, just recolored / re-filled).
var PG_HEART_SVG =
  '<svg viewBox="0 0 24 24"><path d="M12 21s-7-4.35-9.5-9.13C.9 8.5 2.5 5 6 5c2 0 3.4 1.1 6 4 2.6-2.9 4-4 6-4 3.5 0 5.1 3.5 3.5 6.87C19 16.65 12 21 12 21z"/></svg>';

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

// Persist column selections + search query so coming back from another
// tab restores the same view. Key is page-scoped.
var _PG_STATE_KEY = 'pg.rate.state';
function _pgSaveState() {
  try {
    localStorage.setItem(_PG_STATE_KEY, JSON.stringify({
      folder: selectedFolder,
      file:   selectedFile,
      tag:    selectedTag,
      search: (document.getElementById('search-input') || {}).value || '',
    }));
  } catch (e) {}
}
function _pgLoadState() {
  try {
    var s = JSON.parse(localStorage.getItem(_PG_STATE_KEY) || '{}');
    if (s && typeof s === 'object') {
      if (s.folder) selectedFolder = s.folder;
      if ('file' in s) selectedFile = s.file || null;
      if ('tag'  in s) selectedTag  = s.tag  || null;
      if (s.search) {
        var i = document.getElementById('search-input');
        if (i) i.value = s.search;
      }
    }
  } catch (e) {}
}

async function init() {
  allScenes = await fetch('/rate/api/scenes').then(function(r){return r.json()});
  _pgLoadState();
  await bbInit();
  renderGrid();
  // Re-fire transcript search if it was active on this page last visit.
  var sq = (document.getElementById('search-input') || {}).value || '';
  if (sq.trim()) onSearchInput(sq);
  // Auto-open the transcript modal when the URL carries
  // ?focus_transcript=<scene_id> — used by /builder's transcript
  // button so the rich modal stays single-sourced here.
  try {
    var params = new URLSearchParams(window.location.search);
    var ft = parseInt(params.get('focus_transcript'), 10);
    if (!isNaN(ft) && ft > 0) {
      openTranscript(ft);
      params.delete('focus_transcript');
      var qs = params.toString();
      var clean = window.location.pathname + (qs ? '?' + qs : '');
      window.history.replaceState({}, '', clean);
    }
  } catch (e) {}
}

async function bbInit() {
  await bbReloadFolders();
  bbRenderFolderCol();
  bbRenderFilesCol();
  bbRenderTagsCol();
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

  var filesList = document.getElementById('bb-files-list');
  filesList.addEventListener('click', function(e){
    var heart = e.target.closest('.pg-heart[data-act="fav-file"]');
    if (heart) {
      e.stopPropagation();
      bbToggleFileFavorite(heart.getAttribute('data-fn'));
      return;
    }
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectFile(row.getAttribute('data-fn'));
  });
  filesList.addEventListener('dragstart', function(e){
    var row = e.target.closest('.bb-row[draggable="true"]');
    if (!row) return;
    var fn = row.getAttribute('data-fn');
    if (!fn) return;
    e.dataTransfer.setData('text/plain', fn);
    e.dataTransfer.setData('application/x-pg-file', fn);
    e.dataTransfer.effectAllowed = 'move';
  });
  document.getElementById('bb-tags-list').addEventListener('click', function(e){
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectTag(row.getAttribute('data-tag'));
  });
}

async function bbReloadFolders() {
  try {
    var r = await fetch('/api/folders/list');
    bbFolders = await r.json();
  } catch (e) {
    bbFolders = { smart: [], user: [], memberships: {} };
  }
  bbFileFavorites = new Set(bbFolders.favorites || []);
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

function bbSelectFolder(fid) {
  if (!fid) return;
  selectedFolder = fid;
  _bbRecomputeFolderFiles();
  if (selectedFile && bbFolderFiles && !bbFolderFiles.has(selectedFile)) {
    selectedFile = null;
  }
  _pgSaveState();
  bbRenderFolderCol();
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
}

async function bbCreateFolder() {
  var name = (window.prompt('New folder name:') || '').trim();
  if (!name) return;
  var r = await fetch('/api/folders', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name: name}),
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
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name: name}),
  });
  if (!r.ok) { alert('Could not rename.'); return; }
  await bbReloadFolders();
  bbRenderFolderCol();
}

async function bbDeleteFolder(fid) {
  if (!confirm('Delete this folder? Files inside will return to All Files.')) return;
  var r = await fetch('/api/folders/' + encodeURIComponent(fid), {method:'DELETE'});
  if (!r.ok) { alert('Could not delete.'); return; }
  if (selectedFolder === fid) selectedFolder = 'all';
  await bbReloadFolders();
  bbRenderFolderCol();
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
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
  var fn = e.dataTransfer.getData('application/x-pg-file')
        || e.dataTransfer.getData('text/plain');
  if (!fn) return;
  var fid = row.getAttribute('data-fid');
  var r = await fetch('/api/folders/membership', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({filename: fn, folder_id: fid}),
  });
  if (!r.ok) { alert('Move failed.'); return; }
  await bbReloadFolders();
  bbRenderFolderCol();
  bbRenderFilesCol();
  renderGrid();
}

function bbRenderFilesCol() {
  // Match the visibility rules of the default grid: hide excluded scenes
  // from the per-file counts unless the user is explicitly looking at the
  // Hidden pseudo-tag. Counts shouldn't include them otherwise.
  var includeExcluded = (selectedTag === '__hidden__');
  var folderSet = bbFolderFiles;   // null = no folder restriction
  var counts = {};
  var totalAll = 0;
  for (var i = 0; i < allScenes.length; i++) {
    var s = allScenes[i];
    if (!includeExcluded && s.excluded) continue;
    var fn = s.filename || '';
    if (!fn) continue;
    if (folderSet && !folderSet.has(fn)) continue;
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
    // draggable=true so the row can be dropped on a user folder.
    var favCls = bbFileFavorites.has(f) ? ' on' : '';
    html += '<div class="bb-row' + (selectedFile===f?' selected':'') + '"'
         + ' draggable="true"'
         + ' data-fn="' + safe + '" title="' + safe + '">'
         + '<button class="pg-heart' + favCls + '" data-fn="' + safe + '"'
         + ' data-act="fav-file" title="Toggle favorite">' + PG_HEART_SVG + '</button>'
         + '<span class="bb-row-name">' + pretty + '</span>'
         + '<span class="bb-row-count">' + counts[f] + '</span></div>';
  }
  document.getElementById('bb-files-list').innerHTML = html;
}

async function bbToggleFileFavorite(filename) {
  var on = !bbFileFavorites.has(filename);
  // Optimistic: flip locally, then sync with the server.
  if (on) bbFileFavorites.add(filename);
  else bbFileFavorites.delete(filename);
  bbRenderFilesCol();
  try {
    await fetch('/api/folders/favorite', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: filename, favorite: on, scope: 'source'}),
    });
  } catch (e) {}
  // Refresh folder list so the Favorites smart folder's count updates.
  await bbReloadFolders();
  bbRenderFolderCol();
  bbRenderFilesCol();
  renderGrid();   // Favorites folder selection may have changed which scenes show
}

async function bbToggleSceneFavorite(sceneId) {
  var s = allScenes.find(function(x){ return x.id === sceneId; });
  if (!s) return;
  var on = !s.favorite;
  s.favorite = on;   // optimistic
  // Update any rendered card without a full re-render.
  var btn = document.querySelector('.scene-card[id="sc-' + sceneId + '"] .pg-heart');
  if (btn) btn.classList.toggle('on', on);
  try {
    await fetch('/rate/api/favorite', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({scene_id: sceneId, favorite: on}),
    });
  } catch (e) {}
}

function bbRenderTagsCol() {
  // Tag list is computed against the currently-selected file (or all
  // scenes when no file is picked). Counts always exclude hidden scenes
  // — except for the dedicated Hidden row, which exists precisely so
  // the user can dig into them.
  var folderSet = bbFolderFiles;
  var poolForTags = allScenes.filter(function(s){
    if (s.excluded) return false;
    if (folderSet && !folderSet.has(s.filename)) return false;
    return (selectedFile === null || s.filename === selectedFile);
  });
  var tagCounts = {};
  for (var i = 0; i < poolForTags.length; i++) {
    var ts = poolForTags[i].tags || [];
    for (var j = 0; j < ts.length; j++) {
      tagCounts[ts[j]] = (tagCounts[ts[j]] || 0) + 1;
    }
  }
  // Unrated / Hidden counts respect the folder + file selection too.
  var unrated = 0, hidden = 0;
  for (var i = 0; i < allScenes.length; i++) {
    var s = allScenes[i];
    if (folderSet && !folderSet.has(s.filename)) continue;
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
  _pgSaveState();
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
}

function bbSelectTag(t) {
  selectedTag = t || null;
  _pgSaveState();
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
  if (bbFolderFiles) {
    base = base.filter(function(s) { return bbFolderFiles.has(s.filename); });
  }
  if (selectedFile) {
    base = base.filter(function(s) { return s.filename === selectedFile; });
  }
  if (searchHitIds) {
    base = base.filter(function(s) { return searchHitIds.has(s.id); });
  }
  return base;
}

// Small crop icon sitting just to the left of the duration pill on
// scenes whose per-scene crop has been customized via the gear popup.
// Stroke-based crop glyph — matches /builder's timeline layer sidebar
// .th-btn.crop-btn so the indicator reads identically across surfaces.
var CROP_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
  + '<path d="M6 2v14a2 2 0 0 0 2 2h14"/>'
  + '<path d="M2 6h14a2 2 0 0 1 2 2v14"/></svg>';
function cropFlagHTML(scene) {
  var hasCrop = (Array.isArray(scene.free_crops) && scene.free_crops.length)
             || (typeof scene.crop_x_frac === 'number');
  if (!hasCrop) return '';
  return '<span class="crop-flag" title="Custom crop applied">' + CROP_ICON_SVG + '</span>';
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
    var cls = 'scene-card'
      + (s.excluded ? ' excluded' : '')
      + (s.wide ? ' wide' : '');

    // No more "UNRATED" badge — unrated state is conveyed by neither
    // vote button being active. HIDDEN stays because the down-vote
    // result is hide-from-results, which is non-obvious otherwise.
    var badge = s.excluded
      ? '<span class="excluded-badge">HIDDEN</span>'
      : '';

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
            // 12 × PG_AI_BADGE_SCALE(2) = 24px glyph, which fits snug
            // inside the 26px dark circle around it.
            size: 12,
            model: sceneModel,
            title: 'Tagged by ' + sceneProv
                  + (sceneModel ? ' · ' + sceneModel : ''),
          }) + '</span>'
      : '';
    var heartCls = s.favorite ? ' on' : '';
    html += '<div class="' + cls + '" id="sc-' + s.id + '">'
      + '<img class="thumb" src="/api/thumbnail/' + s.id + '" loading="lazy" onclick="playScene(' + s.id + ')"/>'
      + '<div class="play-overlay" onclick="playScene(' + s.id + ')"><div class="play-circle">'
      + '<svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div></div>'
      + '<span class="wide-badge" title="Widescreen source">'
      +   '<svg viewBox="0 0 16 9"><rect x="0.75" y="0.75" width="14.5" height="7.5" rx="1.5"/></svg>'
      + '</span>'
      + cropFlagHTML(s)
      + '<span class="dur-badge">' + s.duration + 's</span>'
      + aiBadge
      + badge
      + '<div class="vote-row">'
      + '<div class="vote-left-cluster">'
      +   '<button class="pg-heart' + heartCls + '" onclick="bbToggleSceneFavorite(' + s.id + ')" title="Toggle favorite">' + PG_HEART_SVG + '</button>'
      +   '<button class="vote-btn vdown' + (s.status === 'down' ? ' active' : '') + '" onclick="vote(' + s.id + ',\'down\')" title="Hide scene">'
      +     '<svg viewBox="0 0 24 24"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>'
      +   '</button>'
      + '</div>'
      + '<div class="vote-right-cluster">'
      +   (s.has_transcript
            ? '<button class="vote-btn vtxt" onclick="openTranscript(' + s.id + ')" title="Show transcript">'
              + '<svg viewBox="0 0 24 24"><path d="M4 4h16v2H4V4zm0 4h16v2H4V8zm0 4h10v2H4v-2zm0 4h16v2H4v-2zm0 4h10v2H4v-2z"/></svg>'
              + '</button>'
            : '')
      +   '<button class="vote-btn vgear" onclick="openSceneSettings(' + s.id + ')" title="Scene settings"><svg viewBox="0 0 24 24"><path d="M19.43 12.98c.04-.32.07-.65.07-.98s-.03-.66-.07-.98l2.11-1.65a.5.5 0 0 0 .12-.64l-2-3.46a.5.5 0 0 0-.61-.22l-2.49 1a7.03 7.03 0 0 0-1.69-.98l-.38-2.65A.488.488 0 0 0 14 2h-4a.488.488 0 0 0-.49.42l-.38 2.65c-.61.25-1.17.58-1.69.98l-2.49-1a.5.5 0 0 0-.61.22l-2 3.46a.5.5 0 0 0 .12.64L4.57 11.02c-.04.32-.07.65-.07.98s.03.66.07.98l-2.11 1.65a.5.5 0 0 0-.12.64l2 3.46c.14.24.42.34.66.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.05.24.25.42.49.42h4c.24 0 .44-.18.49-.42l.38-2.65c.61-.25 1.17-.58 1.69-.98l2.49 1c.24.1.52 0 .66-.22l2-3.46a.5.5 0 0 0-.12-.64l-2.11-1.65zM12 15.5A3.5 3.5 0 0 1 8.5 12 3.5 3.5 0 0 1 12 8.5a3.5 3.5 0 0 1 3.5 3.5 3.5 3.5 0 0 1-3.5 3.5z"/></svg></button>'
      + '</div>'
      + '</div>'
      + '</div>';
  }
  grid.innerHTML = html;
}

async function vote(sceneId, action) {
  // Down = hide. Confirm before hiding (mirrors /builder's vote flow) so
  // an accidental click on the small thumb-down can't silently drop the
  // scene out of the grid.
  if (action === 'down') {
    if (!window.confirm('Hide this scene from the grid?')) return;
  }
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

// The full scene-settings popup (crop + trim) lives on /builder; jump
// there with a query param that auto-opens it for this scene.
function openSceneSettings(sceneId) {
  window.location.href = '/builder?scene_settings=' + sceneId;
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
    _pgSaveState();
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
      _pgSaveState();
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
var _txMode = 'english';        // 'native' | 'english' | 'both' — default English when present
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
    // Default: English when available (whether or not a Native version
    // exists), else fall back to Native.
    _txMode = _txHasEnglish ? 'english' : 'native';
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
