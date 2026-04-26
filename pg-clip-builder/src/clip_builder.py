"""clip_builder.py — Clip Builder routes + UI for PeaceGrappler."""

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import (
    get_all_scenes, get_scene_by_id, set_scene_ignored,
    save_generated_video, get_generated_video, get_all_generated_videos,
)
from video import (
    ASSETS_DIR, AUDIO_EXTENSIONS, OUTPUT_DIR, THUMB_DIR, TRANSITIONS,
    VIDEO_EXTENSIONS, XFADE_DUR,
    build_music_track, concatenate_clips, extract_subclip,
    find_asset, generate_placeholder, get_video_duration, has_audio_stream,
    normalize_clip, overlay_music, overlay_music_track,
    process_split_section, process_track,
)

clip_builder_bp = Blueprint("clip_builder", __name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_music_files():
    music = []
    music_dir = ASSETS_DIR / "music"
    dirs = [music_dir, ASSETS_DIR] if music_dir.exists() else [ASSETS_DIR]
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in AUDIO_EXTENSIONS:
                music.append({"name": f.stem, "path": str(f)})
    return music


def _get_thumb_count(scene):
    dur = scene["end_time"] - scene["start_time"]
    if dur <= 5:
        return 1
    return math.ceil(dur / 5)


def _get_thumbnail_path(scene):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(
        f"{scene['video_path']}@{scene['start_time']}@{scene['end_time']}".encode()
    ).hexdigest()
    path = THUMB_DIR / f"{key}.jpg"

    if not path.exists():
        mid = scene["start_time"] + (scene["end_time"] - scene["start_time"]) / 2
        try:
            subprocess.run(
                ["ffmpeg", "-ss", f"{mid:.2f}", "-i", scene["video_path"],
                 "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "5",
                 "-y", str(path)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    return path if path.exists() else None


def _pregenerate_thumbnails():
    def _run():
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        scenes = get_all_scenes(include_ignored=True)
        generated = 0
        for scene in scenes:
            _get_thumbnail_path(scene)
            count = _get_thumb_count(scene)
            if count <= 1:
                continue
            dur = scene["end_time"] - scene["start_time"]
            interval = dur / count
            for i in range(count):
                key = hashlib.md5(
                    f"{scene['video_path']}@{scene['start_time']}@{scene['end_time']}@m{i}".encode()
                ).hexdigest()
                path = THUMB_DIR / f"{key}.jpg"
                if path.exists():
                    continue
                t = scene["start_time"] + interval * i + interval / 2
                try:
                    subprocess.run(
                        ["ffmpeg", "-ss", f"{t:.2f}", "-i", scene["video_path"],
                         "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "5",
                         "-y", str(path)],
                        capture_output=True, timeout=15,
                    )
                    generated += 1
                except Exception:
                    pass
        print(f"Thumbnail pre-generation complete: {generated} new thumbnails")
    threading.Thread(target=_run, daemon=True).start()


def _resolve_clip(item):
    """Resolve a timeline clip item (by scene id or video_file) to clip data dict."""
    sid = item.get("id", -1)
    if sid >= 0:
        scene = get_scene_by_id(sid)
        if scene:
            return {
                "video_file": scene["video_path"],
                "start": scene["start_time"],
                "end": scene["end_time"],
                "duration": round(scene["end_time"] - scene["start_time"], 1),
            }
    if item.get("video_file"):
        return {
            "video_file": item["video_file"],
            "start": item["start"],
            "end": item["end"],
            "duration": round(item["end"] - item["start"], 1),
        }
    return None


def _resolve_clip_for_track(item):
    """Resolve a clip item for process_track (split sections)."""
    return _resolve_clip(item)


# ── routes ───────────────────────────────────────────────────────────────────

@clip_builder_bp.route("/builder")
def builder_page():
    _pregenerate_thumbnails()
    return HTML_PAGE


@clip_builder_bp.route("/api/tags")
def api_tags():
    scenes = get_all_scenes(include_ignored=True)
    tag_counts = {}
    hidden_count = 0
    for s in scenes:
        if s["ignored"]:
            hidden_count += 1
            continue
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    if hidden_count > 0:
        tag_counts["hidden"] = hidden_count
    return jsonify(dict(sorted(tag_counts.items())))


@clip_builder_bp.route("/api/clips")
def api_clips():
    tag = request.args.get("tag", "")
    scenes = get_all_scenes(include_ignored=True, include_excluded=False)
    if tag == "hidden":
        clips = [s for s in scenes if s["ignored"]]
    elif tag:
        clips = [s for s in scenes if tag in s["tags"] and not s["ignored"]]
    else:
        clips = [s for s in scenes if not s["ignored"]]
    clips = sorted(clips, key=lambda s: s["end_time"] - s["start_time"])
    return jsonify([{
        "id": s["id"],
        "video_file": s["video_path"],
        "filename": s["video_filename"],
        "start": s["start_time"],
        "end": s["end_time"],
        "duration": round(s["end_time"] - s["start_time"], 1),
        "tags": s["tags"],
        "ignored": s["ignored"],
        "thumb_count": _get_thumb_count(s),
        "wide": s["wide"],
    } for s in clips])


@clip_builder_bp.route("/api/thumbnail/<int:scene_id>")
def api_thumbnail(scene_id):
    scene = get_scene_by_id(scene_id)
    if not scene:
        return "", 404
    path = _get_thumbnail_path(scene)
    if path and path.exists():
        return send_file(str(path), mimetype="image/jpeg")
    return "", 204


@clip_builder_bp.route("/api/thumbnail/<int:scene_id>/<int:thumb_idx>")
def api_thumbnail_multi(scene_id, thumb_idx):
    scene = get_scene_by_id(scene_id)
    if not scene:
        return "", 404
    count = _get_thumb_count(scene)
    if count <= 1 or thumb_idx < 0 or thumb_idx >= count:
        return "", 404

    dur = scene["end_time"] - scene["start_time"]
    interval = dur / count

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(
        f"{scene['video_path']}@{scene['start_time']}@{scene['end_time']}@m{thumb_idx}".encode()
    ).hexdigest()
    path = THUMB_DIR / f"{key}.jpg"

    if not path.exists():
        t = scene["start_time"] + interval * thumb_idx + interval / 2
        try:
            subprocess.run(
                ["ffmpeg", "-ss", f"{t:.2f}", "-i", scene["video_path"],
                 "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "5",
                 "-y", str(path)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    if path and path.exists():
        return send_file(str(path), mimetype="image/jpeg")
    return "", 204


@clip_builder_bp.route("/api/music")
def api_music():
    return jsonify(_load_music_files())


@clip_builder_bp.route("/api/transitions")
def api_transitions():
    return jsonify(TRANSITIONS)


@clip_builder_bp.route("/api/load-video", methods=["POST"])
def api_load_video():
    """Look up a generated video by filename and return its saved timeline."""
    data = request.json or {}
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"error": "No filename provided"}), 400

    # 1. Search generated_videos DB by matching filename
    all_gen = get_all_generated_videos()
    for g in all_gen:
        if Path(g["path"]).name == filename:
            return jsonify({
                "video": filename,
                "timeline": g["timeline"],
                "count": len([i for i in g["timeline"] if i.get("type") == "clip"]),
            })

    # 2. Fallback: check for companion .json sidecar next to a video in output/
    stem = Path(filename).stem
    for root, _, files in os.walk(str(OUTPUT_DIR)):
        for f in files:
            if f == filename:
                json_path = Path(root) / f"{stem}.json"
                if json_path.exists():
                    try:
                        sidecar = json.loads(json_path.read_text())
                        timeline = sidecar.get("timeline", [])
                        if timeline:
                            return jsonify({
                                "video": filename,
                                "timeline": timeline,
                                "count": len([i for i in timeline
                                              if i.get("type") == "clip"]),
                            })
                    except Exception:
                        pass

    return jsonify({"error": "No timeline found for this video. "
                    "Only previously generated videos can be loaded."}), 404


@clip_builder_bp.route("/api/open", methods=["POST"])
def api_open():
    data = request.json or {}
    path = data.get("path", "")
    reveal = data.get("reveal", False)
    if path and os.path.exists(path):
        if reveal:
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["open", path])
    return jsonify({"ok": True})


@clip_builder_bp.route("/api/hide", methods=["POST"])
def api_hide():
    data = request.json
    scene_id = data.get("id", -1)
    ignore = data.get("ignore", True)

    scene = get_scene_by_id(scene_id)
    if not scene:
        return jsonify({"error": "Invalid scene"}), 400

    set_scene_ignored(scene_id, ignore)

    # Rebuild tag counts
    scenes = get_all_scenes(include_ignored=True)
    tag_counts = {}
    hidden_count = 0
    for s in scenes:
        if s["ignored"]:
            hidden_count += 1
            continue
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    if hidden_count > 0:
        tag_counts["hidden"] = hidden_count

    return jsonify({"ok": True, "tags": dict(sorted(tag_counts.items()))})


@clip_builder_bp.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.json
    timeline = data.get("timeline", [])

    clips = []
    music_changes = []
    transition_map = {}
    pending_music = None
    pending_volume = 3
    pending_transition = None
    muted = False

    for item in timeline:
        itype = item.get("type", "")
        if itype == "transition":
            pending_transition = item.get("name", "fade")
        elif itype == "music":
            name = item.get("name", "")
            pending_music = name if name else None
            pending_volume = item.get("volume", 3)
        elif itype == "mute":
            muted = True
        elif itype == "unmute":
            muted = False
        elif itype == "placeholder":
            dur = item.get("duration", 5)
            color = item.get("color", "black")
            clip_data = {
                "type": "placeholder",
                "duration": dur,
                "color": color,
                "muted": muted,
            }
            if pending_music is not None:
                music_changes.append((len(clips), pending_music, pending_volume))
                pending_music = None
            if pending_transition is not None and len(clips) > 0:
                transition_map[len(clips) - 1] = pending_transition
                pending_transition = None
            clips.append(clip_data)
        elif itype == "split":
            if pending_music is not None:
                music_changes.append((len(clips), pending_music, pending_volume))
                pending_music = None
            if pending_transition is not None and len(clips) > 0:
                transition_map[len(clips) - 1] = pending_transition
                pending_transition = None
            clips.append({
                "type": "split",
                "top": item.get("top", []),
                "bottom": item.get("bottom", []),
            })
        elif itype == "clip":
            clip_data = _resolve_clip(item)
            if clip_data:
                clip_data = dict(clip_data)
                clip_data["muted"] = muted
                if pending_music is not None:
                    music_changes.append((len(clips), pending_music, pending_volume))
                    pending_music = None
                if pending_transition is not None and len(clips) > 0:
                    transition_map[len(clips) - 1] = pending_transition
                    pending_transition = None
                clips.append(clip_data)

    if not clips:
        return jsonify({"error": "No valid clips selected"}), 400

    music_list = _load_music_files()
    music_lookup = {m["name"]: m["path"] for m in music_list}

    today = datetime.now().strftime("%Y-%m-%d")
    date_dir = OUTPUT_DIR / today
    date_dir.mkdir(parents=True, exist_ok=True)

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

    total_dur = int(sum(c.get("duration", 0) for c in clips if c.get("type") != "split"))
    out_file = date_dir / f"hl-{total_dur}-{counter}.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        clip_paths = []
        intro_count = 0

        intro = find_asset("intro")
        if intro:
            intro_norm = os.path.join(tmp, "intro_norm.mp4")
            if normalize_clip(str(intro), intro_norm):
                clip_paths.append(intro_norm)
                intro_count = 1

        for i, clip in enumerate(clips):
            if clip.get("type") == "split":
                split_out = os.path.join(tmp, f"split_{i:03d}.mp4")
                top_items = clip.get("top", [])
                bottom_items = clip.get("bottom", [])
                if process_split_section(top_items, bottom_items, tmp,
                                         f"s{i}", split_out, _resolve_clip_for_track):
                    clip_paths.append(split_out)
            elif clip.get("type") == "placeholder":
                ph_out = os.path.join(tmp, f"placeholder_{i:03d}.mp4")
                dur = clip.get("duration", 5)
                color = clip.get("color", "black")
                try:
                    r = subprocess.run(
                        ["ffmpeg", "-y",
                         "-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:d={dur:.2f}:r=30",
                         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                         "-t", f"{dur:.2f}",
                         "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
                         "-pix_fmt", "yuv420p", "-movflags", "+faststart", ph_out],
                        capture_output=True, timeout=60,
                    )
                    if r.returncode == 0 and os.path.exists(ph_out):
                        clip_paths.append(ph_out)
                except Exception:
                    pass
            else:
                clip_out = os.path.join(tmp, f"clip_{i:03d}.mp4")
                if extract_subclip(clip["video_file"], clip["start"],
                                   clip["duration"], clip_out):
                    if clip.get("muted"):
                        muted_out = os.path.join(tmp, f"clip_{i:03d}_m.mp4")
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", clip_out,
                             "-c:v", "copy", "-an",
                             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-c:a", "aac", "-shortest", muted_out],
                            capture_output=True, timeout=30,
                        )
                        if os.path.exists(muted_out):
                            clip_paths.append(muted_out)
                        else:
                            clip_paths.append(clip_out)
                    else:
                        clip_paths.append(clip_out)

        outro_added = False
        outro = find_asset("outro")
        if outro:
            outro_norm = os.path.join(tmp, "outro_norm.mp4")
            if normalize_clip(str(outro), outro_norm):
                clip_paths.append(outro_norm)
                outro_added = True

        if len(clip_paths) < 2:
            return jsonify({"error": "Not enough clips could be extracted"}), 500

        # Build per-gap transition list
        n_paths = len(clip_paths)
        all_transitions = [None] * (n_paths - 1)
        if intro_count and n_paths > 1:
            all_transitions[0] = "fade"
        for gap_idx, trans_name in transition_map.items():
            path_gap = gap_idx + intro_count
            if path_gap < n_paths - 1:
                all_transitions[path_gap] = trans_name
        if outro_added and n_paths > 1:
            all_transitions[-1] = "fade"

        assembled = os.path.join(tmp, "assembled.mp4")
        if not concatenate_clips(clip_paths, assembled, all_transitions):
            return jsonify({"error": "Video assembly failed"}), 500

        video_dur = get_video_duration(assembled)

        # Build music track if any music markers exist
        has_music = any(mc[1] for mc in music_changes)

        if has_music and music_changes:
            path_durations = [get_video_duration(cp) for cp in clip_paths]

            clip_starts = [0.0]
            for j in range(1, len(clip_paths)):
                if all_transitions[j - 1] is not None:
                    pair_min = min(path_durations[j - 1], path_durations[j])
                    xf = min(XFADE_DUR, pair_min * 0.4)
                    if xf < 0.1:
                        xf = 0.0
                else:
                    xf = 0.0
                clip_starts.append(clip_starts[-1] + path_durations[j - 1] - xf)

            change_times = []
            for ci, mname, mvol in music_changes:
                path_idx = ci + intro_count
                if path_idx < len(clip_starts):
                    t = clip_starts[path_idx]
                else:
                    t = video_dur
                mpath = music_lookup.get(mname) if mname else None
                change_times.append((t, mpath, mvol))

            change_times.sort(key=lambda x: x[0])

            segments = []
            for k, (t, mpath, mvol) in enumerate(change_times):
                end_t = change_times[k + 1][0] if k + 1 < len(change_times) else video_dur
                seg_dur = end_t - t
                if seg_dur > 0:
                    segments.append({
                        "start": t, "duration": seg_dur,
                        "music": mpath, "volume": mvol,
                    })

            if change_times and change_times[0][0] > 0.05:
                segments.insert(0, {
                    "start": 0, "duration": change_times[0][0],
                    "music": None, "volume": 0,
                })

            music_track = os.path.join(tmp, "music_track.m4a")
            if build_music_track(segments, video_dur, music_track):
                if not overlay_music_track(
                    assembled, music_track, str(out_file), segments
                ):
                    shutil.copy2(assembled, str(out_file))
            else:
                shutil.copy2(assembled, str(out_file))
        else:
            shutil.copy2(assembled, str(out_file))

    final_dur = get_video_duration(str(out_file))

    # Save to database
    def _serialize(item):
        itype = item.get("type", "")
        if itype == "clip":
            sid = item.get("id", -1)
            if sid >= 0:
                scene = get_scene_by_id(sid)
                if scene:
                    return {"type": "clip", "video_file": scene["video_path"],
                            "start": scene["start_time"], "end": scene["end_time"]}
            elif item.get("video_file"):
                return {"type": "clip", "video_file": item["video_file"],
                        "start": item["start"], "end": item["end"]}
        elif itype == "music":
            return {"type": "music", "name": item.get("name", ""),
                    "volume": item.get("volume", 0)}
        elif itype == "transition":
            return {"type": "transition", "name": item.get("name", "")}
        elif itype == "mute":
            return {"type": "mute"}
        elif itype == "unmute":
            return {"type": "unmute"}
        elif itype == "placeholder":
            return {"type": "placeholder", "duration": item.get("duration", 5),
                    "color": item.get("color", "black")}
        elif itype == "split":
            return {
                "type": "split",
                "top": [x for x in (_serialize(ti) for ti in item.get("top", [])) if x],
                "bottom": [x for x in (_serialize(bi) for bi in item.get("bottom", [])) if x],
            }
        return None

    save_timeline = [x for x in (_serialize(i) for i in timeline) if x]
    save_generated_video(str(out_file), round(final_dur, 1), save_timeline)

    return jsonify({
        "path": str(out_file),
        "duration": round(final_dur, 1),
    })


# ── HTML page ────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler Clip Builder</title>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:flex;flex-direction:column;height:100vh;overflow:hidden;
}

/* -- Header -- */
header{
  background:#141414;border-bottom:1px solid #2a2a2a;
  padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;
}
header h1{font-size:18px;font-weight:600;color:#fff;white-space:nowrap}
header h1 span{color:#e53935}
.hctl{display:flex;align-items:center;gap:12px;flex:1}
select,button{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 12px;font-size:13px;cursor:pointer;
}
select:hover,button:hover{border-color:#666}
select:focus,button:focus{outline:none;border-color:#e53935}
#clip-count{font-size:13px;color:#888}
nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
  border:1px solid #444;border-radius:6px}
nav a:hover{color:#fff;border-color:#888}
nav a.active{color:#e53935;border-color:#e53935}

/* -- Main grid -- */
main{flex:1;overflow-y:auto;padding:12px}
#clip-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;
}
.clip-card{
  background:#1a1a1a;border-radius:8px;overflow:hidden;
  cursor:grab;transition:transform .15s,opacity .2s;position:relative;
}
.clip-card:hover{transform:translateY(-2px)}
.clip-card:active{cursor:grabbing}
.clip-card.in-tl{opacity:.3;pointer-events:none}
.clip-card.sortable-ghost{opacity:.4}
.clip-card .thumb{
  width:100%;aspect-ratio:9/16;object-fit:cover;display:block;background:#111;
}
.clip-card .dur{
  position:absolute;top:6px;right:6px;
  background:rgba(0,0,0,.75);color:#fff;font-size:11px;font-weight:600;
  padding:2px 6px;border-radius:4px;
}
.clip-card .info{padding:6px 8px}
.clip-card .fn{
  font-size:10px;color:#777;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.clip-card .tg{
  font-size:10px;color:#999;margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.thumb-dots{
  position:absolute;left:0;right:0;bottom:36px;
  display:flex;justify-content:center;gap:3px;
  pointer-events:none;opacity:0;transition:opacity .2s;z-index:1;
}
.clip-card:hover .thumb-dots.multi{opacity:1}
.thumb-dots .tdot{
  width:5px;height:5px;border-radius:50%;
  background:rgba(255,255,255,.4);transition:background .15s;
}
.thumb-dots .tdot.active{background:#fff}

/* -- Footer / Timeline -- */
footer{
  background:#141414;border-top:2px solid #e53935;
  padding:10px 16px 12px;flex-shrink:0;
}
.tl-hdr{
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:8px;font-size:13px;
}
.tl-hdr .lbl{color:#e53935;font-weight:600}
.tl-hdr .tot{color:#888}
#timeline{
  min-height:110px;background:#0d0d0d;border:2px dashed #333;border-radius:8px;
  display:flex;align-items:center;gap:6px;padding:8px;overflow-x:auto;
  transition:border-color .2s;
}
#timeline.drag-over{border-color:#e53935}
.tl-empty{color:#555;font-size:13px;margin:auto;pointer-events:none}
.tl-item{
  flex-shrink:0;width:64px;background:#1e1e1e;border-radius:6px;
  overflow:hidden;position:relative;cursor:grab;
}
.tl-item:active{cursor:grabbing}
.tl-item .thumb{width:100%;aspect-ratio:9/16;object-fit:cover;display:block}
.tl-item .dur{
  text-align:center;font-size:10px;padding:2px 0;background:#111;color:#ccc;
}
.tl-item .rm{
  position:absolute;top:2px;right:2px;
  background:rgba(229,57,53,.85);color:#fff;border:none;border-radius:50%;
  width:18px;height:18px;font-size:12px;line-height:16px;text-align:center;
  cursor:pointer;padding:0;display:none;
}
.tl-item:hover .rm{display:block}

/* -- Music labels -- */
.music-row{
  display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap;
}
.music-row .row-label{font-size:13px;color:#888;flex-shrink:0}
#music-labels{display:flex;gap:6px;flex-wrap:wrap}
.music-pill{
  display:inline-flex;align-items:center;gap:4px;
  padding:5px 12px;border-radius:16px;font-size:12px;font-weight:600;
  cursor:grab;user-select:none;white-space:nowrap;
  transition:transform .15s,box-shadow .15s;
}
.music-pill:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.4)}
.music-pill:active{cursor:grabbing}
.music-pill.sortable-ghost{opacity:.4}
.music-pill .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}

/* Music pills in timeline */
.tl-music{
  flex-shrink:0;display:flex;flex-direction:column;align-items:center;
  padding:6px 10px 4px;border-radius:12px;font-size:11px;font-weight:600;
  cursor:grab;position:relative;white-space:nowrap;
  align-self:center;gap:3px;
}
.tl-music:active{cursor:grabbing}
.tl-music-name{display:flex;align-items:center;gap:4px}
.tl-music-name .dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.tl-music-name .rm{
  margin-left:4px;background:none;color:inherit;border:none;
  font-size:14px;cursor:pointer;padding:0;line-height:1;opacity:.6;
}
.tl-music-name .rm:hover{opacity:1}
.tl-vol{display:flex;gap:2px;align-items:flex-end;height:14px}
.vol-bar{
  width:5px;border-radius:1px;cursor:pointer;
  transition:background .12s;opacity:.35;
}
.vol-bar.active{opacity:1}

/* -- Transition labels & timeline -- */
.trans-row{
  display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap;
}
.trans-row .row-label{font-size:13px;color:#888;flex-shrink:0}
#trans-labels{display:flex;gap:5px;flex-wrap:wrap}
.trans-pill{
  display:inline-flex;align-items:center;
  padding:4px 10px;border-radius:12px;font-size:11px;font-weight:600;
  cursor:grab;user-select:none;white-space:nowrap;
  background:#2a2a2a;color:#aaa;border:1px solid #444;
  transition:transform .15s,box-shadow .15s;
}
.trans-pill:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.4);color:#fff;border-color:#888}
.trans-pill:active{cursor:grabbing}
.trans-pill.sortable-ghost{opacity:.4}
.tl-trans{
  flex-shrink:0;display:flex;align-items:center;gap:4px;
  padding:4px 8px;border-radius:8px;font-size:10px;font-weight:600;
  cursor:grab;white-space:nowrap;align-self:center;
  background:#2a2a2a;color:#aaa;border:1px dashed #555;
}
.tl-trans:active{cursor:grabbing}
.tl-trans .rm{
  background:none;color:inherit;border:none;
  font-size:14px;cursor:pointer;padding:0;line-height:1;opacity:.6;
}
.tl-trans .rm:hover{opacity:1}

/* -- Split sections -- */
.tl-split .tl-item .thumb{aspect-ratio:16/9}
.tl-split{
  flex-shrink:0;display:flex;flex-direction:column;
  border:2px solid #2196f3;border-radius:8px;background:#111;
}
.tl-split-track{
  display:flex;align-items:center;gap:6px;padding:6px;min-height:94px;
}
.tl-split-top{border-bottom:1px dashed rgba(33,150,243,.4)}
.tl-split-empty{color:#444;font-size:10px;pointer-events:none;white-space:nowrap}

/* -- Mute/Unmute pills -- */
.tl-mute{
  flex-shrink:0;display:flex;align-items:center;
  padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;
  cursor:grab;white-space:nowrap;align-self:center;
  background:#2a2a2a;border:1px solid #555;
}
.tl-mute .rm{
  margin-left:4px;background:none;color:inherit;border:none;
  font-size:14px;cursor:pointer;padding:0;line-height:1;opacity:.6;
}
.tl-mute .rm:hover{opacity:1}

/* -- Placeholder -- */
.tl-placeholder{
  flex-shrink:0;width:48px;background:#222;border-radius:6px;
  overflow:hidden;position:relative;cursor:grab;border:1px dashed #555;
}
.tl-placeholder .ph-color{width:100%;height:40px;display:block}
.tl-placeholder .dur{text-align:center;font-size:10px;padding:2px 0;background:#111;color:#ccc}
.tl-placeholder .rm{
  position:absolute;top:2px;right:2px;
  background:rgba(229,57,53,.85);color:#fff;border:none;border-radius:50%;
  width:18px;height:18px;font-size:12px;line-height:16px;text-align:center;
  cursor:pointer;padding:0;display:none;
}
.tl-placeholder:hover .rm{display:block}

/* -- Effects row -- */
.effects-row{
  display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap;
}
.effects-row .row-label{font-size:13px;color:#888;flex-shrink:0}
#effects-labels{display:flex;gap:5px;flex-wrap:wrap}
.effects-pill{
  display:inline-flex;align-items:center;
  padding:4px 10px;border-radius:12px;font-size:11px;font-weight:600;
  cursor:grab;user-select:none;white-space:nowrap;
  background:#2a2a2a;color:#aaa;border:1px solid #444;
  transition:transform .15s,box-shadow .15s;
}
.effects-pill:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.4);color:#fff;border-color:#888}
.effects-pill:active{cursor:grabbing}
.effects-pill.sortable-ghost{opacity:.4}

/* -- Context menu -- */
.ctx-menu{
  position:fixed;z-index:200;background:#222;border:1px solid #444;
  border-radius:6px;padding:4px 0;min-width:120px;box-shadow:0 4px 16px rgba(0,0,0,.6);
}
.ctx-menu div{
  padding:6px 14px;font-size:13px;cursor:pointer;color:#e0e0e0;
}
.ctx-menu div:hover{background:#333}

/* -- Ignored clip styling -- */
.clip-card.ignored{opacity:.5}
.clip-card.ignored .thumb{filter:grayscale(.8)}
.clip-card .ignore-badge{
  position:absolute;top:6px;left:6px;
  background:rgba(229,57,53,.85);color:#fff;font-size:9px;font-weight:700;
  padding:1px 5px;border-radius:3px;display:none;
}
.clip-card.ignored .ignore-badge{display:block}
.wide-badge{
  position:absolute;top:6px;left:6px;
  width:20px;height:14px;border:1.5px solid rgba(255,255,255,.7);border-radius:2px;
  display:none;
}
.wide-badge::after{
  content:'';position:absolute;top:2px;left:4px;
  width:10px;height:6px;background:rgba(255,255,255,.7);border-radius:1px;
}
.clip-card.wide .wide-badge{display:block}
.clip-card.ignored .wide-badge{display:none}
.tl-item[data-wide] .wide-badge{display:block}
.tl-item .wide-badge{top:2px;left:2px;width:14px;height:10px}
.tl-item .wide-badge::after{top:1.5px;left:2.5px;width:7px;height:4px}

#clear-btn{
  background:none;color:#666;border:1px solid #444;padding:3px 10px;
  font-size:11px;border-radius:4px;transition:color .15s,border-color .15s;
}
#clear-btn:hover{color:#e53935;border-color:#e53935}

.controls{display:flex;align-items:center;gap:12px;margin-top:10px}
#gen-btn{
  background:#e53935;color:#fff;border:none;padding:8px 20px;font-weight:600;
  border-radius:6px;margin-left:auto;transition:background .2s;
}
#gen-btn:hover{background:#c62828}
#gen-btn:disabled{background:#555;cursor:not-allowed}

/* -- Overlays -- */
.overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:100;
  flex-direction:column;align-items:center;justify-content:center;
}
.overlay.active{display:flex}
.spinner{
  width:40px;height:40px;border:3px solid #333;border-top-color:#e53935;
  border-radius:50%;animation:spin .8s linear infinite;margin-bottom:16px;
}
@keyframes spin{to{transform:rotate(360deg)}}
.modal{
  background:#1e1e1e;border-radius:12px;padding:24px;text-align:center;
  max-width:420px;width:90%;
}
#seg-grid{
  display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:12px;
}
.seg-thumb{
  width:72px;cursor:pointer;border-radius:6px;overflow:hidden;
  border:3px solid transparent;transition:border-color .15s;position:relative;
}
.seg-thumb.selected{border-color:#2196f3}
.seg-thumb img{width:100%;aspect-ratio:9/16;object-fit:cover;display:block}
.seg-thumb .seg-label{
  text-align:center;font-size:9px;padding:2px 0;background:#111;color:#aaa;
}

.modal h2{margin-bottom:12px;color:#4caf50}
.modal p{color:#aaa;margin-bottom:16px;font-size:14px;word-break:break-all}
.modal button{margin:0 4px}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span> Clip Builder</h1>
  <div class="hctl">
    <select id="tag-filter" onchange="filterByTag()">
      <option value="">All Tags</option>
    </select>
    <span id="clip-count"></span>
  </div>
  <nav>
    <a href="/builder" class="active">Builder</a>
    <a href="/analyze">Analyze</a>
    <a href="/library">Library</a>
    <a href="/wizard">AI Wizard</a>
  </nav>
</header>

<main>
  <div id="clip-grid"></div>
</main>

<footer>
  <div class="tl-hdr">
    <span class="lbl">TIMELINE</span>
    <button onclick="triggerLoad()">Load</button>
    <input type="file" id="load-input" accept=".json,.mp4,.mov,.avi,.mkv,.webm,.m4v" style="display:none" onchange="handleLoadFile(event)">
    <button id="clear-btn" onclick="clearTimeline()">Clear</button>
    <span class="tot" id="tl-total">0 clips &mdash; 0.0s</span>
  </div>
  <div id="timeline"><span class="tl-empty">Drag clips here to build your sequence</span></div>
  <div class="music-row">
    <span class="row-label">Music:</span>
    <div id="music-labels"></div>
  </div>
  <div class="trans-row">
    <span class="row-label">Transitions:</span>
    <div id="trans-labels"></div>
  </div>
  <div class="effects-row">
    <span class="row-label">Effects:</span>
    <div id="effects-labels"></div>
  </div>
  <div class="controls">
    <button id="gen-btn" onclick="generateVideo()">Generate Video</button>
  </div>
</footer>

<div class="overlay" id="loading">
  <div class="spinner"></div>
  <div>Generating video&hellip;</div>
</div>

<div class="overlay" id="result-modal">
  <div class="modal">
    <h2>Video Ready!</h2>
    <p id="result-info"></p>
    <button onclick="openResult()">Open Video</button>
    <button onclick="closeResult()">Close</button>
  </div>
</div>

<div class="overlay" id="segment-modal">
  <div class="modal" style="max-width:600px">
    <h2 style="color:#2196f3">Segment Clip</h2>
    <p id="seg-info"></p>
    <div id="seg-grid"></div>
    <div style="margin-top:16px">
      <button onclick="addSelectedSegments()">Add Selected</button>
      <button onclick="closeSegmentModal()">Close</button>
    </div>
  </div>
</div>

<div class="overlay" id="load-modal">
  <div class="modal">
    <h2 style="color:#2196f3">Load Timeline</h2>
    <p id="load-info"></p>
    <button onclick="applyLoad('replace')">Replace</button>
    <button onclick="applyLoad('prepend')">Prepend</button>
    <button onclick="applyLoad('append')">Append</button>
    <button onclick="cancelLoad()">Cancel</button>
  </div>
</div>

<script>
var MUSIC_COLORS = [
  {bg:'#1b5e20',fg:'#a5d6a7',dot:'#4caf50'},
  {bg:'#0d47a1',fg:'#90caf9',dot:'#2196f3'},
  {bg:'#4a148c',fg:'#ce93d8',dot:'#ab47bc'},
  {bg:'#e65100',fg:'#ffcc80',dot:'#ff9800'},
  {bg:'#880e4f',fg:'#f48fb1',dot:'#e91e63'},
];
var NO_MUSIC_COLOR = {bg:'#333',fg:'#999',dot:'#666'};

var allClips = [];
var musicList = [];
var musicColorMap = {};
var gridSort = null;
var tlSort = null;
var resultPath = '';
var tlUid = 0;

async function init() {
  var tags = await fetch('/api/tags').then(function(r){return r.json()});
  var sel = document.getElementById('tag-filter');
  for (var tag in tags) {
    var o = document.createElement('option');
    o.value = tag; o.textContent = tag + ' (' + tags[tag] + ')';
    sel.appendChild(o);
  }

  allClips = await fetch('/api/clips').then(function(r){return r.json()});
  renderGrid();

  musicList = await fetch('/api/music').then(function(r){return r.json()});
  musicColorMap['No Music'] = NO_MUSIC_COLOR;
  for (var i = 0; i < musicList.length; i++) {
    musicColorMap[musicList[i].name] = MUSIC_COLORS[i % MUSIC_COLORS.length];
  }
  renderMusicLabels();
  renderTransitionLabels();
  renderEffectsLabels();
  setupTimeline();
}

function renderMusicLabels() {
  var container = document.getElementById('music-labels');
  var items = [{name:'No Music'}].concat(musicList);
  container.innerHTML = '';
  for (var i = 0; i < items.length; i++) {
    var m = items[i];
    var col = musicColorMap[m.name] || NO_MUSIC_COLOR;
    var pill = document.createElement('div');
    pill.className = 'music-pill';
    pill.dataset.music = m.name;
    pill.style.background = col.bg;
    pill.style.color = col.fg;
    pill.innerHTML = '<span class="dot" style="background:' + col.dot + '"></span>' + m.name;
    container.appendChild(pill);
  }

  new Sortable(container, {
    group: {name:'timeline',pull:'clone',put:false},
    sort: false,
    animation: 150,
  });
}

var TRANSITION_LIST = [
  'fade','fadeblack','fadewhite',
  'wipeleft','wiperight','wipeup','wipedown',
  'slideleft','slideright',
  'circlecrop','circleopen','circleclose',
  'radial','dissolve',
  'smoothleft','smoothright',
  'diagtl','diagbr',
  'horzopen','horzclose','vertopen','vertclose',
  'hlslice','hrslice',
  'zoomin',
  'coverleft','coverright',
  'revealleft','revealright',
  'pixelize',
];

function renderTransitionLabels() {
  var container = document.getElementById('trans-labels');
  container.innerHTML = '';
  for (var i = 0; i < TRANSITION_LIST.length; i++) {
    var name = TRANSITION_LIST[i];
    var pill = document.createElement('div');
    pill.className = 'trans-pill';
    pill.dataset.transition = name;
    pill.textContent = name;
    container.appendChild(pill);
  }
  new Sortable(container, {
    group: {name:'timeline', pull:'clone', put:false},
    sort: false,
    animation: 150,
  });
}

function renderEffectsLabels() {
  var container = document.getElementById('effects-labels');
  container.innerHTML = '';
  var effects = [
    {label:'Mute', key:'sound', val:'mute'},
    {label:'Unmute', key:'sound', val:'unmute'},
    {label:'Placeholder', key:'placeholder', val:'1'},
  ];
  for (var i = 0; i < effects.length; i++) {
    var eff = effects[i];
    var pill = document.createElement('div');
    pill.className = 'effects-pill';
    pill.dataset[eff.key] = eff.val;
    pill.textContent = eff.label;
    container.appendChild(pill);
  }
  new Sortable(container, {
    group: {name:'timeline', pull:'clone', put:false},
    sort: false,
    animation: 150,
  });
}

function makeTlTransition(name) {
  var el = document.createElement('div');
  el.className = 'tl-trans';
  el.dataset.transition = name;
  el.dataset.tltype = 'transition';
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.innerHTML = name
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.parentNode)">&times;</button>';
  return el;
}

function makeTlMute(isMute) {
  var el = document.createElement('div');
  el.className = 'tl-mute';
  el.dataset.tltype = isMute ? 'mute' : 'unmute';
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.style.color = isMute ? '#ef5350' : '#66bb6a';
  el.innerHTML = (isMute ? 'MUTE' : 'UNMUTE')
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.parentNode)">&times;</button>';
  return el;
}

function makeTlPlaceholder(duration, color) {
  duration = duration || 5;
  color = color || 'black';
  var el = document.createElement('div');
  el.className = 'tl-placeholder';
  el.dataset.tltype = 'placeholder';
  el.dataset.phDuration = duration;
  el.dataset.phColor = color;
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.innerHTML = '<span class="ph-color" style="background:' + color + '"></span>'
    + '<div class="dur">' + duration + 's</div>'
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.parentNode)">&times;</button>';
  el.addEventListener('click', function(e) {
    if (e.target.classList.contains('rm')) return;
    var newDur = prompt('Duration (seconds):', el.dataset.phDuration);
    if (newDur !== null && !isNaN(parseFloat(newDur)) && parseFloat(newDur) > 0) {
      el.dataset.phDuration = parseFloat(newDur);
      el.querySelector('.dur').textContent = parseFloat(newDur) + 's';
      syncTl();
    }
    var newColor = prompt('Color (CSS name or hex):', el.dataset.phColor);
    if (newColor !== null && newColor.trim()) {
      el.dataset.phColor = newColor.trim();
      el.querySelector('.ph-color').style.background = newColor.trim();
    }
  });
  return el;
}

function cardHTML(c) {
  var clipIds = getTlClipIds();
  var cls = 'clip-card';
  if (clipIds.indexOf(c.id) >= 0) cls += ' in-tl';
  if (c.ignored) cls += ' ignored';
  if (c.wide) cls += ' wide';
  var tags = c.tags.length > 3
    ? c.tags.slice(0,3).join(', ') + '\u2026'
    : c.tags.join(', ');
  var dots = '';
  if (c.thumb_count > 1) {
    dots = '<div class="thumb-dots multi">';
    for (var i = 0; i < c.thumb_count; i++) {
      dots += '<span class="tdot' + (i === 0 ? ' active' : '') + '"></span>';
    }
    dots += '</div>';
  }
  return '<div class="' + cls + '" data-id="' + c.id + '" data-tc="' + (c.thumb_count||1) + '" oncontextmenu="showCtx(event,' + c.id + ')">'
    + '<img class="thumb" src="/api/thumbnail/' + c.id + '" loading="lazy"/>'
    + '<span class="dur">' + c.duration + 's</span>'
    + '<span class="ignore-badge">HIDDEN</span>'
    + '<span class="wide-badge"></span>'
    + dots
    + '<div class="info">'
    + '<div class="fn" title="' + c.filename + '">' + c.filename.substring(0,25) + '</div>'
    + '<div class="tg" title="' + c.tags.join(', ') + '">' + tags + '</div>'
    + '</div></div>';
}

function renderGrid() {
  var tag = document.getElementById('tag-filter').value;
  var clips;
  if (tag === 'hidden') {
    clips = allClips.filter(function(c){return c.ignored});
  } else if (tag) {
    clips = allClips.filter(function(c){return c.tags.indexOf(tag)>=0 && !c.ignored});
  } else {
    clips = allClips.filter(function(c){return !c.ignored});
  }
  var grid = document.getElementById('clip-grid');
  grid.innerHTML = clips.map(cardHTML).join('');
  document.getElementById('clip-count').textContent = clips.length + ' clips';

  if (gridSort) gridSort.destroy();
  gridSort = new Sortable(grid, {
    group: {name:'timeline',pull:'clone',put:false},
    sort: false,
    animation: 150,
    filter: '.in-tl,.ignored',
  });
}

function handleTlAdd(evt, isSplitTrack) {
  var el = evt.item;
  if (el.dataset.music !== undefined) {
    var name = el.dataset.music;
    var newEl = makeTlMusic(name);
    el.replaceWith(newEl);
    syncTl();
    return;
  }
  if (el.dataset.transition !== undefined) {
    var name = el.dataset.transition;
    var newEl = makeTlTransition(name);
    el.replaceWith(newEl);
    syncTl();
    return;
  }
  if (el.dataset.sound !== undefined) {
    var isMute = el.dataset.sound === 'mute';
    var newEl = makeTlMute(isMute);
    el.replaceWith(newEl);
    syncTl();
    return;
  }
  if (el.dataset.placeholder !== undefined) {
    var newEl = makeTlPlaceholder();
    el.replaceWith(newEl);
    syncTl();
    return;
  }
  if (el.classList.contains('tl-item')) {
    syncTl();
    return;
  }
  var id = parseInt(el.dataset.id);
  if (isNaN(id) || getTlClipIds().indexOf(id) >= 0) { el.remove(); syncTl(); return; }
  var clip = allClips.find(function(c){return c.id===id});
  if (!clip) { el.remove(); syncTl(); return; }
  var tlEl = makeTlItem(clip);
  el.replaceWith(tlEl);
  syncTl();
}

function setupTimeline() {
  var tl = document.getElementById('timeline');
  tlSort = new Sortable(tl, {
    group: {name:'timeline',pull:true,put:function(to, from, el){
      return true;
    }},
    sort: true,
    animation: 150,
    onAdd: function(evt) {
      handleTlAdd(evt, false);
    },
    onSort: function() { syncTl(); },
    onRemove: function() { syncTl(); },
  });
}

function makeTlItem(c) {
  var el = document.createElement('div');
  el.className = 'tl-item';
  el.dataset.id = c.id;
  el.dataset.tltype = 'clip';
  if (c.wide) el.dataset.wide = '1';
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.innerHTML = '<img class="thumb" src="/api/thumbnail/' + c.id + '"/>'
    + '<div class="dur">' + c.duration + 's</div>'
    + '<span class="wide-badge"></span>'
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.parentNode)">&times;</button>';
  el.addEventListener('contextmenu', function(e) { showTlCtx(e, el); });
  return el;
}

function makeTlMusic(name) {
  var col = musicColorMap[name] || NO_MUSIC_COLOR;
  var el = document.createElement('div');
  el.className = 'tl-music';
  el.dataset.music = name;
  el.dataset.tltype = 'music';
  el.dataset.volume = (name === 'No Music') ? '0' : '3';
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.style.background = col.bg;
  el.style.color = col.fg;

  var nameRow = '<div class="tl-music-name">'
    + '<span class="dot" style="background:' + col.dot + '"></span>'
    + name
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.closest(\'.tl-music\'))">&times;</button>'
    + '</div>';

  var volRow = '';
  if (name !== 'No Music') {
    var bars = '';
    var heights = [4,6,8,11,14];
    for (var i = 0; i < 5; i++) {
      var active = i < 3 ? ' active' : '';
      bars += '<div class="vol-bar' + active + '" data-level="' + (i+1) + '"'
        + ' style="height:' + heights[i] + 'px;background:' + col.dot + '"'
        + ' onclick="event.stopPropagation();setVol(this)"></div>';
    }
    volRow = '<div class="tl-vol">' + bars + '</div>';
  }

  el.innerHTML = nameRow + volRow;
  return el;
}

function setVol(barEl) {
  var level = parseInt(barEl.dataset.level);
  var pill = barEl.closest('.tl-music');
  pill.dataset.volume = level;
  var bars = pill.querySelectorAll('.vol-bar');
  bars.forEach(function(b) {
    var bl = parseInt(b.dataset.level);
    b.classList.toggle('active', bl <= level);
  });
}

function rmTlEl(el) {
  var splitEl = el.closest('.tl-split');
  el.remove();
  if (splitEl) checkSplitEmpty(splitEl);
  syncTl();
}

function showTlCtx(e, el) {
  e.preventDefault();
  e.stopPropagation();
  hideCtx();
  if (!el.dataset.wide || el.closest('.tl-split')) return;
  var m = document.createElement('div');
  m.className = 'ctx-menu';
  m.style.left = e.clientX + 'px';
  m.style.top = e.clientY + 'px';
  var d = document.createElement('div');
  d.textContent = 'Split Timeline';
  d.onclick = function() {
    hideCtx();
    makeSplitSection(el);
  };
  m.appendChild(d);
  document.body.appendChild(m);
  ctxEl = m;
  var r = m.getBoundingClientRect();
  if (r.right > window.innerWidth) m.style.left = (window.innerWidth - r.width - 4) + 'px';
  if (r.bottom > window.innerHeight) m.style.top = (window.innerHeight - r.height - 4) + 'px';
}

function makeSplitSection(clipEl) {
  var tl = document.getElementById('timeline');
  var splitEl = document.createElement('div');
  splitEl.className = 'tl-split';
  splitEl.dataset.tltype = 'split';
  var uid = ++tlUid;
  splitEl.dataset.uid = uid;

  var topTrack = document.createElement('div');
  topTrack.className = 'tl-split-track tl-split-top';
  var bottomTrack = document.createElement('div');
  bottomTrack.className = 'tl-split-track tl-split-bottom';

  clipEl.parentNode.insertBefore(splitEl, clipEl);
  topTrack.appendChild(clipEl);

  var emptyLabel = document.createElement('span');
  emptyLabel.className = 'tl-split-empty';
  emptyLabel.textContent = 'drag wide clips here';
  bottomTrack.appendChild(emptyLabel);

  splitEl.appendChild(topTrack);
  splitEl.appendChild(bottomTrack);

  setupSplitTrack(topTrack, splitEl);
  setupSplitTrack(bottomTrack, splitEl);

  syncTl();
}

function setupSplitTrack(trackEl, splitEl) {
  new Sortable(trackEl, {
    group: {
      name: 'timeline',
      pull: true,
      put: function(to, from, el) {
        if (el.classList.contains('clip-card')) {
          if (!el.classList.contains('wide')) return false;
        }
        if (el.classList.contains('tl-item')) {
          if (!el.dataset.wide) return false;
        }
        if (el.classList.contains('tl-split')) return false;
        return true;
      }
    },
    sort: true,
    animation: 150,
    onAdd: function(evt) {
      var empty = trackEl.querySelector('.tl-split-empty');
      if (empty) empty.remove();
      handleTlAdd(evt, true);
    },
    onRemove: function() {
      checkSplitEmpty(splitEl);
      syncTl();
    },
    onSort: function() { syncTl(); },
  });
}

function checkSplitEmpty(splitEl) {
  var topTrack = splitEl.querySelector('.tl-split-top');
  var bottomTrack = splitEl.querySelector('.tl-split-bottom');
  if (!topTrack || !bottomTrack) return;

  var topItems = topTrack.querySelectorAll('[data-tltype]');
  var bottomItems = bottomTrack.querySelectorAll('[data-tltype]');

  if (!topItems.length && !topTrack.querySelector('.tl-split-empty')) {
    var sp = document.createElement('span');
    sp.className = 'tl-split-empty';
    sp.textContent = 'drag wide clips here';
    topTrack.appendChild(sp);
  }
  if (!bottomItems.length && !bottomTrack.querySelector('.tl-split-empty')) {
    var sp = document.createElement('span');
    sp.className = 'tl-split-empty';
    sp.textContent = 'drag wide clips here';
    bottomTrack.appendChild(sp);
  }

  if (!topItems.length && !bottomItems.length) {
    splitEl.remove();
  }
}

function getTrackDuration(trackEl) {
  var total = 0;
  trackEl.querySelectorAll('[data-tltype]').forEach(function(el) {
    if (el.dataset.tltype === 'clip') {
      if (el.dataset.id !== undefined) {
        var c = allClips.find(function(x){return x.id===parseInt(el.dataset.id)});
        if (c) total += c.duration;
      } else if (el.dataset.start !== undefined) {
        total += parseFloat(el.dataset.end) - parseFloat(el.dataset.start);
      }
    } else if (el.dataset.tltype === 'placeholder') {
      total += parseFloat(el.dataset.phDuration) || 5;
    }
  });
  return total;
}

function clearTimeline() {
  var items = document.querySelectorAll('#timeline [data-tltype]');
  if (!items.length) return;
  if (!window.confirm('Clear the entire timeline?')) return;
  items.forEach(function(el){ el.remove(); });
  syncTl();
}

// -- Segmenting --
var segmentState = {clip: null, selected: new Set()};

function showSegmentModal(clip) {
  segmentState.clip = clip;
  segmentState.selected = new Set();
  var count = clip.thumb_count;
  var interval = clip.duration / count;
  document.getElementById('seg-info').textContent =
    clip.filename + ' \u2014 ' + clip.duration + 's (' + count + ' segments)';
  var grid = document.getElementById('seg-grid');
  grid.innerHTML = '';
  for (var i = 0; i < count; i++) {
    var segStart = Math.round((clip.start + interval * i) * 10) / 10;
    var segEnd = Math.round(Math.min(clip.start + interval * (i + 1), clip.end) * 10) / 10;
    var div = document.createElement('div');
    div.className = 'seg-thumb';
    div.dataset.segIdx = i;
    div.innerHTML = '<img src="/api/thumbnail/' + clip.id + '/' + i + '"/>'
      + '<div class="seg-label">' + segStart.toFixed(1) + '\u2013' + segEnd.toFixed(1) + 's</div>';
    (function(idx, el) {
      el.onclick = function() {
        if (segmentState.selected.has(idx)) {
          segmentState.selected.delete(idx);
          el.classList.remove('selected');
        } else {
          segmentState.selected.add(idx);
          el.classList.add('selected');
        }
      };
    })(i, div);
    grid.appendChild(div);
  }
  document.getElementById('segment-modal').classList.add('active');
}

function addSelectedSegments() {
  if (!segmentState.clip || !segmentState.selected.size) return;
  var clip = segmentState.clip;
  var count = clip.thumb_count;
  var interval = clip.duration / count;
  var tl = document.getElementById('timeline');
  var ph = tl.querySelector('.tl-empty');
  if (ph) ph.remove();
  var indices = Array.from(segmentState.selected).sort(function(a,b){return a-b});
  for (var i = 0; i < indices.length; i++) {
    var idx = indices[i];
    var segStart = Math.round((clip.start + interval * idx) * 100) / 100;
    var segEnd = Math.round(Math.min(clip.start + interval * (idx + 1), clip.end) * 100) / 100;
    tl.appendChild(makeTlSegment(clip, idx, clip.video_file, segStart, segEnd));
  }
  closeSegmentModal();
  syncTl();
}

function closeSegmentModal() {
  document.getElementById('segment-modal').classList.remove('active');
  segmentState = {clip: null, selected: new Set()};
}

function makeTlSegment(parentClip, segIdx, videoFile, start, end) {
  var dur = Math.round((end - start) * 10) / 10;
  var el = document.createElement('div');
  el.className = 'tl-item';
  el.dataset.tltype = 'clip';
  el.dataset.videoFile = videoFile;
  el.dataset.start = start.toFixed(2);
  el.dataset.end = end.toFixed(2);
  el.dataset.parentId = parentClip.id;
  el.dataset.segIdx = segIdx;
  if (parentClip.wide) el.dataset.wide = '1';
  var uid = ++tlUid;
  el.dataset.uid = uid;
  el.innerHTML = '<img class="thumb" src="/api/thumbnail/' + parentClip.id + '/' + segIdx + '"/>'
    + '<div class="dur">' + dur + 's</div>'
    + '<span class="wide-badge"></span>'
    + '<button class="rm" onclick="event.stopPropagation();rmTlEl(this.parentNode)">&times;</button>';
  el.addEventListener('contextmenu', function(e) { showTlCtx(e, el); });
  return el;
}

function getTlClipIds() {
  var ids = [];
  document.querySelectorAll('#timeline .tl-item[data-id]').forEach(function(el) {
    ids.push(parseInt(el.dataset.id));
  });
  document.querySelectorAll('#timeline .tl-split .tl-item[data-id]').forEach(function(el) {
    var id = parseInt(el.dataset.id);
    if (ids.indexOf(id) < 0) ids.push(id);
  });
  return ids;
}

function syncTl() {
  var children = document.querySelectorAll('#timeline > [data-tltype]');
  var clipIds = getTlClipIds();

  var total = 0;
  var totalClips = 0;
  var splitCount = 0;

  document.querySelectorAll('#timeline > .tl-item[data-id]').forEach(function(el) {
    var c = allClips.find(function(x){return x.id===parseInt(el.dataset.id)});
    if (c) total += c.duration;
    totalClips++;
  });
  document.querySelectorAll('#timeline > .tl-item').forEach(function(el) {
    if (el.dataset.id === undefined && el.dataset.start !== undefined) {
      total += parseFloat(el.dataset.end) - parseFloat(el.dataset.start);
      totalClips++;
    }
  });
  document.querySelectorAll('#timeline > .tl-placeholder').forEach(function(el) {
    total += parseFloat(el.dataset.phDuration) || 5;
  });
  document.querySelectorAll('#timeline > .tl-split').forEach(function(splitEl) {
    splitCount++;
    var topTrack = splitEl.querySelector('.tl-split-top');
    var bottomTrack = splitEl.querySelector('.tl-split-bottom');
    var topDur = topTrack ? getTrackDuration(topTrack) : 0;
    var botDur = bottomTrack ? getTrackDuration(bottomTrack) : 0;
    total += Math.max(topDur, botDur);
    splitEl.querySelectorAll('.tl-item[data-id]').forEach(function() { totalClips++; });
    splitEl.querySelectorAll('.tl-item').forEach(function(el) {
      if (el.dataset.id === undefined && el.dataset.start !== undefined) totalClips++;
    });
  });

  var musicCount = document.querySelectorAll('#timeline .tl-music').length;
  var summary = totalClips + ' clip' + (totalClips !== 1 ? 's' : '');
  if (splitCount) summary += ' + ' + splitCount + ' split' + (splitCount !== 1 ? 's' : '');
  if (musicCount) summary += ' + ' + musicCount + ' music';
  summary += ' \u2014 ' + total.toFixed(1) + 's';
  document.getElementById('tl-total').textContent = summary;

  var hasItems = children.length > 0;
  var ph = document.querySelector('#timeline .tl-empty');
  if (hasItems && ph) ph.remove();
  if (!hasItems && !document.querySelector('#timeline .tl-empty')) {
    var sp = document.createElement('span');
    sp.className = 'tl-empty';
    sp.textContent = 'Drag clips and music here to build your sequence';
    document.getElementById('timeline').appendChild(sp);
  }

  document.querySelectorAll('#clip-grid .clip-card').forEach(function(card) {
    var cid = parseInt(card.dataset.id);
    card.classList.toggle('in-tl', clipIds.indexOf(cid) >= 0);
  });
}

function filterByTag() { renderGrid(); }

// -- Context menu (hide / unhide) --
var ctxEl = null;
function showCtx(e, id) {
  e.preventDefault();
  hideCtx();
  var clip = allClips.find(function(c){return c.id===id});
  if (!clip) return;
  var m = document.createElement('div');
  m.className = 'ctx-menu';
  m.style.left = e.clientX + 'px';
  m.style.top = e.clientY + 'px';
  var label = clip.ignored ? 'Unhide' : 'Hide';
  var d = document.createElement('div');
  d.textContent = label;
  d.onclick = function(){ hideCtx(); toggleIgnore(id, !clip.ignored); };
  m.appendChild(d);
  if (clip.duration > 10) {
    var s = document.createElement('div');
    s.textContent = 'Segment';
    s.onclick = function(){ hideCtx(); showSegmentModal(clip); };
    m.appendChild(s);
  }
  document.body.appendChild(m);
  ctxEl = m;
  var r = m.getBoundingClientRect();
  if (r.right > window.innerWidth) m.style.left = (window.innerWidth - r.width - 4) + 'px';
  if (r.bottom > window.innerHeight) m.style.top = (window.innerHeight - r.height - 4) + 'px';
}
function hideCtx() { if (ctxEl) { ctxEl.remove(); ctxEl = null; } }
document.addEventListener('click', hideCtx);
document.addEventListener('contextmenu', function(e) { e.preventDefault(); });

async function toggleIgnore(id, ignore) {
  var res = await fetch('/api/hide', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id, ignore:ignore}),
  });
  var data = await res.json();
  if (!data.ok) return;

  var clip = allClips.find(function(c){return c.id===id});
  if (clip) clip.ignored = ignore;

  var sel = document.getElementById('tag-filter');
  var cur = sel.value;
  sel.innerHTML = '<option value="">All Tags</option>';
  for (var tag in data.tags) {
    var o = document.createElement('option');
    o.value = tag; o.textContent = tag + ' (' + data.tags[tag] + ')';
    sel.appendChild(o);
  }
  sel.value = cur;

  renderGrid();
}

function serializeTlItem(el) {
  var t = el.dataset.tltype;
  if (t === 'clip') {
    if (el.dataset.videoFile) {
      return {type:'clip', video_file:el.dataset.videoFile,
        start:parseFloat(el.dataset.start), end:parseFloat(el.dataset.end)};
    } else {
      return {type:'clip', id:parseInt(el.dataset.id)};
    }
  } else if (t === 'music') {
    return {type:'music', name:el.dataset.music, volume:parseInt(el.dataset.volume)||0};
  } else if (t === 'transition') {
    return {type:'transition', name:el.dataset.transition};
  } else if (t === 'mute') {
    return {type:'mute'};
  } else if (t === 'unmute') {
    return {type:'unmute'};
  } else if (t === 'placeholder') {
    return {type:'placeholder', duration:parseFloat(el.dataset.phDuration)||5,
            color:el.dataset.phColor||'black'};
  } else if (t === 'split') {
    var topTrack = el.querySelector('.tl-split-top');
    var bottomTrack = el.querySelector('.tl-split-bottom');
    var topItems = [];
    var bottomItems = [];
    if (topTrack) topTrack.querySelectorAll('[data-tltype]').forEach(function(child) {
      var s = serializeTlItem(child);
      if (s) topItems.push(s);
    });
    if (bottomTrack) bottomTrack.querySelectorAll('[data-tltype]').forEach(function(child) {
      var s = serializeTlItem(child);
      if (s) bottomItems.push(s);
    });
    return {type:'split', top:topItems, bottom:bottomItems};
  }
  return null;
}

function buildTimeline() {
  var items = [];
  var children = document.querySelectorAll('#timeline > [data-tltype]');
  children.forEach(function(el) {
    var s = serializeTlItem(el);
    if (s) items.push(s);
  });
  return items;
}

async function generateVideo() {
  var timeline = buildTimeline();
  var hasClips = timeline.some(function(t){return t.type==='clip'});
  if (!hasClips) { alert('Add clips to the timeline first!'); return; }

  var btn = document.getElementById('gen-btn');
  btn.disabled = true;
  document.getElementById('loading').classList.add('active');

  try {
    var res = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({timeline: timeline}),
    });
    var data = await res.json();
    document.getElementById('loading').classList.remove('active');
    btn.disabled = false;

    if (data.error) {
      alert('Error: ' + data.error);
    } else {
      resultPath = data.path;
      document.getElementById('result-info').textContent =
        'Duration: ' + data.duration + 's\n' + data.path;
      document.getElementById('result-modal').classList.add('active');
    }
  } catch (e) {
    document.getElementById('loading').classList.remove('active');
    btn.disabled = false;
    alert('Error: ' + e.message);
  }
}

function openResult() {
  if (resultPath) {
    fetch('/api/open', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({path: resultPath}),
    });
  }
  closeResult();
}

function closeResult() {
  document.getElementById('result-modal').classList.remove('active');
}

// -- Load timeline from JSON --
var pendingLoad = null;

function triggerLoad() {
  document.getElementById('load-input').click();
}

async function handleLoadVideo(file) {
  try {
    var res = await fetch('/api/load-video', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename: file.name}),
    });
    var data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    handleLoadData({timeline: data.timeline});
  } catch (err) {
    alert('Failed to load video: ' + err.message);
  }
}

function handleLoadData(data) {
  if (!data.timeline || !data.timeline.length) {
    alert('No timeline data found');
    return;
  }
  function resolveClipItem(item) {
    var fname = item.video_file.split('/').pop();
    var clip = allClips.find(function(c) {
      return Math.abs(c.start - item.start) < 0.01
        && Math.abs(c.end - item.end) < 0.01
        && c.filename === fname;
    });
    if (clip) return {type:'clip', id:clip.id, clip:clip};
    var parent = allClips.find(function(c) {
      return c.filename === fname
        && c.start <= item.start + 0.01
        && c.end >= item.end - 0.01;
    });
    if (parent) {
      var interval = parent.duration / parent.thumb_count;
      var segIdx = Math.round((item.start - parent.start) / interval);
      return {type:'segment', parent:parent, segIdx:segIdx,
        start:item.start, end:item.end, video_file:item.video_file};
    }
    return null;
  }
  function resolveTrackItems(trackItems) {
    var result = [];
    var miss = 0;
    for (var j = 0; j < trackItems.length; j++) {
      var ti = trackItems[j];
      if (ti.type === 'clip') {
        var r = resolveClipItem(ti);
        if (r) result.push(r); else miss++;
      } else { result.push(ti); }
    }
    return {items: result, missing: miss};
  }
  var resolved = [];
  var missing = 0;
  for (var i = 0; i < data.timeline.length; i++) {
    var item = data.timeline[i];
    if (item.type === 'split') {
      var topRes = resolveTrackItems(item.top || []);
      var botRes = resolveTrackItems(item.bottom || []);
      missing += topRes.missing + botRes.missing;
      resolved.push({type:'split', top:topRes.items, bottom:botRes.items});
    } else if (item.type === 'clip') {
      var r = resolveClipItem(item);
      if (r) resolved.push(r); else missing++;
    } else { resolved.push(item); }
  }
  var clipCount = resolved.filter(function(r){return r.type==='clip'||r.type==='segment'}).length;
  var musicCount = resolved.filter(function(r){return r.type==='music'}).length;
  var transCount = resolved.filter(function(r){return r.type==='transition'}).length;
  var splitCount = resolved.filter(function(r){return r.type==='split'}).length;
  var info = clipCount + ' clip' + (clipCount !== 1 ? 's' : '');
  if (splitCount) info += ', ' + splitCount + ' split' + (splitCount !== 1 ? 's' : '');
  if (transCount) info += ', ' + transCount + ' transition' + (transCount !== 1 ? 's' : '');
  if (musicCount) info += ', ' + musicCount + ' music';
  if (missing) info += ' (' + missing + ' not found)';
  pendingLoad = resolved;
  document.getElementById('load-info').textContent = info;
  var hasExisting = document.querySelectorAll('#timeline [data-tltype]').length > 0;
  if (hasExisting) {
    document.getElementById('load-modal').classList.add('active');
  } else {
    applyLoad('replace');
  }
}

function handleLoadFile(e) {
  var file = e.target.files[0];
  if (!file) return;
  var ext = file.name.split('.').pop().toLowerCase();
  var videoExts = ['mp4','mov','avi','mkv','webm','m4v'];
  if (videoExts.indexOf(ext) >= 0) {
    handleLoadVideo(file);
    e.target.value = '';
    return;
  }
  var reader = new FileReader();
  reader.onload = function(ev) {
    try {
      handleLoadData(JSON.parse(ev.target.result));
    } catch (err) {
      alert('Invalid JSON file: ' + err.message);
    }
  };
  reader.readAsText(file);
  e.target.value = '';
}

function applyLoad(mode) {
  document.getElementById('load-modal').classList.remove('active');
  if (!pendingLoad) return;

  var tl = document.getElementById('timeline');

  if (mode === 'replace') {
    tl.querySelectorAll('[data-tltype]').forEach(function(el){ el.remove(); });
    var ph = tl.querySelector('.tl-empty');
    if (ph) ph.remove();
  }

  function createElFromItem(item) {
    if (item.type === 'clip' && item.clip) {
      return makeTlItem(item.clip);
    } else if (item.type === 'segment') {
      return makeTlSegment(item.parent, item.segIdx, item.video_file, item.start, item.end);
    } else if (item.type === 'transition') {
      return makeTlTransition(item.name);
    } else if (item.type === 'mute') {
      return makeTlMute(true);
    } else if (item.type === 'unmute') {
      return makeTlMute(false);
    } else if (item.type === 'placeholder') {
      return makeTlPlaceholder(item.duration || 5, item.color || 'black');
    } else if (item.type === 'music') {
      var el = makeTlMusic(item.name);
      el.dataset.volume = item.volume || 0;
      var bars = el.querySelectorAll('.vol-bar');
      bars.forEach(function(b) {
        var bl = parseInt(b.dataset.level);
        b.classList.toggle('active', bl <= (item.volume || 0));
      });
      return el;
    } else if (item.type === 'split') {
      var splitEl = document.createElement('div');
      splitEl.className = 'tl-split';
      splitEl.dataset.tltype = 'split';
      var uid = ++tlUid;
      splitEl.dataset.uid = uid;

      var topTrack = document.createElement('div');
      topTrack.className = 'tl-split-track tl-split-top';
      var bottomTrack = document.createElement('div');
      bottomTrack.className = 'tl-split-track tl-split-bottom';

      var topItems = item.top || [];
      var bottomItems = item.bottom || [];

      for (var ti = 0; ti < topItems.length; ti++) {
        var te = createElFromItem(topItems[ti]);
        if (te) topTrack.appendChild(te);
      }
      for (var bi = 0; bi < bottomItems.length; bi++) {
        var be = createElFromItem(bottomItems[bi]);
        if (be) bottomTrack.appendChild(be);
      }

      if (!topTrack.children.length) {
        var sp = document.createElement('span');
        sp.className = 'tl-split-empty';
        sp.textContent = 'drag wide clips here';
        topTrack.appendChild(sp);
      }
      if (!bottomTrack.children.length) {
        var sp = document.createElement('span');
        sp.className = 'tl-split-empty';
        sp.textContent = 'drag wide clips here';
        bottomTrack.appendChild(sp);
      }

      splitEl.appendChild(topTrack);
      splitEl.appendChild(bottomTrack);
      setupSplitTrack(topTrack, splitEl);
      setupSplitTrack(bottomTrack, splitEl);
      return splitEl;
    }
    return null;
  }

  var newEls = [];
  for (var i = 0; i < pendingLoad.length; i++) {
    var item = pendingLoad[i];
    var el = createElFromItem(item);
    if (el) newEls.push(el);
  }

  if (mode === 'prepend') {
    var first = tl.querySelector('[data-tltype]');
    for (var i = 0; i < newEls.length; i++) {
      tl.insertBefore(newEls[i], first);
    }
  } else {
    for (var i = 0; i < newEls.length; i++) {
      tl.appendChild(newEls[i]);
    }
  }

  pendingLoad = null;
  syncTl();
}

function cancelLoad() {
  document.getElementById('load-modal').classList.remove('active');
  pendingLoad = null;
}

init();

// -- Hover thumbnail cycling --
var hoverState = {card:null, interval:null, origSrc:null};

document.getElementById('clip-grid').addEventListener('mouseover', function(e) {
  var card = e.target.closest('.clip-card');
  if (!card) { stopHoverCycle(); return; }
  if (card === hoverState.card) return;
  stopHoverCycle();
  var tc = parseInt(card.dataset.tc) || 1;
  if (tc <= 1) return;
  var id = parseInt(card.dataset.id);
  var img = card.querySelector('.thumb');
  var dotsEls = card.querySelectorAll('.tdot');
  hoverState.card = card;
  hoverState.origSrc = img.src;
  for (var i = 0; i < tc; i++) { (new Image()).src = '/api/thumbnail/' + id + '/' + i; }
  var idx = 0;
  img.src = '/api/thumbnail/' + id + '/' + idx;
  updateDots(dotsEls, idx);
  hoverState.interval = setInterval(function() {
    idx = (idx + 1) % tc;
    img.src = '/api/thumbnail/' + id + '/' + idx;
    updateDots(dotsEls, idx);
  }, 1000);
});

document.getElementById('clip-grid').addEventListener('mouseleave', function() {
  stopHoverCycle();
});

function stopHoverCycle() {
  if (hoverState.interval) clearInterval(hoverState.interval);
  if (hoverState.card && hoverState.origSrc) {
    var img = hoverState.card.querySelector('.thumb');
    if (img) img.src = hoverState.origSrc;
    var dots = hoverState.card.querySelectorAll('.tdot');
    if (dots.length) updateDots(dots, 0);
  }
  hoverState = {card:null, interval:null, origSrc:null};
}

function updateDots(dots, activeIdx) {
  for (var i = 0; i < dots.length; i++) {
    dots[i].classList.toggle('active', i === activeIdx);
  }
}
</script>
</body>
</html>"""
