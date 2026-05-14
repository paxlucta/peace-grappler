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
    add_multiple_text_overlays, build_music_track, composite_layered_segment,
    concatenate_clips, extract_subclip, extract_wide_subclip, find_asset,
    generate_placeholder, get_video_duration, has_audio_stream, is_wide_video,
    normalize_clip, overlay_music, overlay_music_track, pad_clip_to_duration,
    process_split_section, process_track, stack_wide_videos,
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
                "video_id":   scene["video_id"],
                "start": scene["start_time"],
                "end": scene["end_time"],
                "duration": round(scene["end_time"] - scene["start_time"], 1),
            }
    if item.get("video_file"):
        return {
            "video_file": item["video_file"],
            "video_id":   None,
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
    from chrome import inject_chrome
    _pregenerate_thumbnails()
    return inject_chrome(HTML_PAGE, active="builder")


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
        "favorite": s.get("favorite", False),
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


@clip_builder_bp.route("/api/scene-frame/<int:scene_id>")
def api_scene_frame(scene_id):
    """Extract a single 360-px-wide JPEG frame at *offset* seconds into
    the scene (offset from ``scene.start_time``). Used by the text-overlay
    editor's "Show video background" mode to preview how the overlay will
    sit on top of the actual scene at that moment. Cached on disk by
    (path, start, end, offset) so dragging the playhead is cheap."""
    try:
        offset = float(request.args.get("t", "0"))
    except (TypeError, ValueError):
        offset = 0.0
    scene = get_scene_by_id(scene_id)
    if not scene:
        return "", 404
    dur = max(0.0, float(scene["end_time"]) - float(scene["start_time"]))
    if offset < 0:
        offset = 0.0
    if offset > max(0.0, dur - 0.05):
        offset = max(0.0, dur - 0.05)
    t = float(scene["start_time"]) + offset

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(
        f"sf:{scene['video_path']}@{scene['start_time']}@{scene['end_time']}"
        f"@o{offset:.2f}".encode()
    ).hexdigest()
    path = THUMB_DIR / f"{key}.jpg"
    if not path.exists():
        try:
            subprocess.run(
                ["ffmpeg", "-ss", f"{t:.2f}", "-i", scene["video_path"],
                 "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "5",
                 "-y", str(path)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
    if path.exists():
        return send_file(str(path), mimetype="image/jpeg")
    return "", 204


@clip_builder_bp.route("/api/music")
def api_music():
    return jsonify(_load_music_files())


@clip_builder_bp.route("/api/transitions")
def api_transitions():
    return jsonify(TRANSITIONS)


@clip_builder_bp.route("/api/fonts")
def api_fonts():
    """Return available fonts: defaults + any .ttf/.otf in assets/fonts."""
    fonts = [
        {"name": "Helvetica", "file": None},
        {"name": "Arial", "file": None},
        {"name": "Geneva", "file": None},
    ]
    fonts_dir = ASSETS_DIR / "fonts"
    if fonts_dir.is_dir():
        for f in sorted(fonts_dir.iterdir()):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                fonts.append({"name": f.stem, "file": f.name})
    return jsonify(fonts)


@clip_builder_bp.route("/api/font/<path:filename>")
def api_font_file(filename):
    """Serve a font file from assets/fonts for CSS @font-face."""
    font_path = ASSETS_DIR / "fonts" / filename
    if font_path.is_file():
        return send_file(str(font_path))
    return "", 404


@clip_builder_bp.route("/api/text-presets", methods=["GET"])
def api_text_presets_list():
    from db import list_text_presets
    return jsonify(list_text_presets())


@clip_builder_bp.route("/api/text-presets", methods=["POST"])
def api_text_presets_save():
    """Save a text overlay preset. Body: {name?, boxes: [...]}.

    Persists every box (text + style + position + size). The thumbnail is
    composed server-side using the same Pillow renderer as the actual video
    pipeline so the gallery preview matches output.
    """
    import io
    import json as _json
    from PIL import Image
    from db import save_text_preset
    from video import _render_text_image

    data = request.get_json(force=True) or {}
    boxes = data.get("boxes")
    if boxes is None:
        # Legacy single-box body — wrap it.
        boxes = [data.get("box")] if data.get("box") else []
    name = (data.get("name") or "").strip()

    # Render thumbnail at the editor's aspect ratio (1080x1920) but small.
    THUMB_W, THUMB_H = 270, 480
    out = Image.new("RGBA", (THUMB_W, THUMB_H), (10, 10, 12, 255))
    for box in boxes:
        if not box or not box.get("text"):
            continue
        try:
            layer = _render_text_image(
                box["text"], THUMB_W, THUMB_H,
                max(8, int(box.get("fontsize", 42) * THUMB_W / 1080)),
                box.get("fontcolor", "white"),
                bool(box.get("bold")),
                float(box.get("box_opacity", 0.5)),
                box.get("x_frac"), box.get("y_frac"),
                box.get("position", "center"),
                italic=bool(box.get("italic")),
                bgcolor=box.get("bgcolor", "#000000"),
                w_frac=box.get("w_frac"),
                h_frac=box.get("h_frac"),
                fontfamily=box.get("fontfamily"),
            )
            out = Image.alpha_composite(out, layer)
        except Exception as exc:
            print(f"[text-preset] thumbnail render failed for one box: {exc}")

    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="JPEG", quality=78)
    preset_id = save_text_preset(name, _json.dumps({"boxes": boxes}),
                                  buf.getvalue())
    return jsonify({"ok": True, "id": preset_id})


@clip_builder_bp.route("/api/text-presets/<int:preset_id>", methods=["GET"])
def api_text_presets_get(preset_id):
    import json as _json
    from db import get_text_preset
    p = get_text_preset(preset_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    payload = _json.loads(p["data_json"])
    # Normalize legacy single-box payloads into {boxes: [...]} so the client
    # can rely on a single shape.
    if isinstance(payload, dict) and "boxes" in payload:
        boxes = payload.get("boxes") or []
    elif isinstance(payload, list):
        boxes = payload
    else:
        boxes = [payload] if payload else []
    return jsonify({
        "id": p["id"], "name": p["name"], "created_at": p["created_at"],
        "boxes": boxes,
    })


@clip_builder_bp.route("/api/text-presets/<int:preset_id>", methods=["DELETE"])
def api_text_presets_delete(preset_id):
    from db import delete_text_preset
    delete_text_preset(preset_id)
    return jsonify({"ok": True})


@clip_builder_bp.route("/api/text-presets/<int:preset_id>/thumb")
def api_text_presets_thumb(preset_id):
    import io
    from db import get_text_preset_thumbnail
    blob = get_text_preset_thumbnail(preset_id)
    if not blob:
        return "", 404
    return send_file(io.BytesIO(blob), mimetype="image/jpeg")


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
            tl = g["timeline"]
            if isinstance(tl, dict):
                count = len([i for i in tl.get("video_track", [])
                             if i.get("type") == "clip"])
            else:
                count = len([i for i in tl if i.get("type") == "clip"])
            return jsonify({
                "video": filename,
                "timeline": tl,
                "count": count,
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


@clip_builder_bp.route("/api/serve-video")
def api_serve_video():
    path = request.args.get("path", "")
    if path and os.path.exists(path) and str(OUTPUT_DIR) in os.path.abspath(path):
        return send_file(os.path.abspath(path), mimetype="video/mp4")
    return "", 404


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

    # New multi-track format
    if "video_track" in data:
        return _generate_multitrack(data)

    # Legacy flat timeline format
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


def _build_layered_segments(clips):
    """Slice the timeline at every clip boundary so each segment has a
    constant active set of clips. Used by the layered (z-order) compositor.

    Returns list of {"start": float, "end": float, "clips": [...]}.
    Within a segment, every listed clip spans the full interval.
    """
    if not clips:
        return []
    boundaries = set()
    for c in clips:
        boundaries.add(round(c["start_time"], 3))
        boundaries.add(round(c["start_time"] + c["duration"], 3))
    boundaries = sorted(boundaries)

    segments = []
    for i in range(len(boundaries) - 1):
        seg_start = boundaries[i]
        seg_end = boundaries[i + 1]
        if seg_end - seg_start < 0.05:
            continue
        active = []
        for c in clips:
            cs = c["start_time"]
            ce = cs + c["duration"]
            if cs <= seg_start + 1e-3 and ce >= seg_end - 1e-3:
                active.append(c)
        if active:
            segments.append({"start": seg_start, "end": seg_end, "clips": active})
    return segments


def _build_time_segments(clips):
    """Group clips into non-overlapping time segments.

    Overlapping wide clips are grouped together so they can be stacked.
    Non-wide clips are always in their own segment (they never overlap).
    Returns list of {"start": float, "end": float, "clips": [...]}.
    """
    if not clips:
        return []

    # Separate wide and non-wide clips
    wides = [c for c in clips if c["wide"]]
    non_wides = [c for c in clips if not c["wide"]]

    # Group overlapping wides using BFS
    wide_groups = []
    visited = set()
    for i, wc in enumerate(wides):
        if i in visited:
            continue
        group = [wc]
        visited.add(i)
        queue = [i]
        while queue:
            cur = queue.pop(0)
            cs = wides[cur]["start_time"]
            ce = cs + wides[cur]["duration"]
            for j, wj in enumerate(wides):
                if j in visited:
                    continue
                js = wj["start_time"]
                je = js + wj["duration"]
                if js < ce and je > cs:
                    visited.add(j)
                    group.append(wj)
                    queue.append(j)
        wide_groups.append(group)

    segments = []

    # Each wide group becomes a segment
    for grp in wide_groups:
        segments.append({
            "start": min(c["start_time"] for c in grp),
            "end": max(c["start_time"] + c["duration"] for c in grp),
            "clips": grp,
        })

    # Each non-wide clip is its own segment
    for nc in non_wides:
        segments.append({
            "start": nc["start_time"],
            "end": nc["start_time"] + nc["duration"],
            "clips": [nc],
        })

    # Sort segments by start time
    segments.sort(key=lambda s: s["start"])
    return segments


def _generate_multitrack(data):
    """Handle new multi-track timeline format with position-based clips."""
    video_track = data.get("video_track", [])
    sound_track = data.get("sound_track", [])
    text_overlays = data.get("text_overlays", [])

    # Parse video track: each clip has start_time, wide flag, stack_order
    clips = []
    for item in video_track:
        if item.get("type") != "clip":
            continue
        clip_data = _resolve_clip(item)
        if clip_data:
            clip_data["start_time"] = item.get("start_time", 0)
            clip_data["track"] = item.get("track", 0)
            clip_data["wide"] = item.get("wide", False)
            clip_data["stack_order"] = item.get("stack_order", 0)
            clip_data["trans_in"] = item.get("trans_in")
            clip_data["trans_out"] = item.get("trans_out")
            clip_data["muted"] = bool(item.get("muted", False))
            clip_data["position"] = item.get("position")  # 'top'/'center'/'bottom'/None
            clip_data["volume"] = item.get("volume", 5)
            # Captions override per-clip: 'inherit' / 'none' / 'top' /
            # 'middle' / 'bottom'. Old saves may use bool/None.
            cap_raw = item.get("captions", "inherit")
            if cap_raw is True:           cap_raw = "bottom"
            elif cap_raw is False:        cap_raw = "none"
            elif cap_raw is None:         cap_raw = "inherit"
            if cap_raw not in ("inherit", "none", "top", "middle", "bottom"):
                cap_raw = "inherit"
            clip_data["captions_override"] = cap_raw
            # Wide-scene crop override (None = inherit layer default).
            # Float 0..1 = horizontal position of the 9:16 crop window
            # (0 = left-aligned, 1 = right-aligned, 0.5 = centered).
            cx = item.get("crop_x_frac")
            clip_data["crop_x_frac"] = (
                float(cx) if isinstance(cx, (int, float)) else None
            )
            # Free-mode crops — list of {src,dst,z} rectangle pairs. When
            # present, this overrides crop_x_frac and produces a multi-
            # window composite at render time (see video.py).
            fc = item.get("free_crops")
            if isinstance(fc, list) and fc:
                clip_data["free_crops"] = fc
            clips.append(clip_data)

    # Track-level settings (mute + default wide-clip position).
    # Defaults match the UI: layer 0=top, 1=center, 2=bottom.
    track_settings_raw = data.get("track_settings") or []
    default_positions = ["top", "center", "bottom"]
    track_settings = []
    for i in range(3):
        s = track_settings_raw[i] if i < len(track_settings_raw) else {}
        # Layer captions: 'none' / 'top' / 'middle' / 'bottom'. Old saves
        # may send a boolean — migrate.
        cap = s.get("captions", "none")
        if cap is True:  cap = "bottom"
        elif cap is False or cap is None: cap = "none"
        if cap not in ("none", "top", "middle", "bottom"):
            cap = "none"
        dcx = s.get("default_crop_x_frac")
        track_settings.append({
            "muted": bool(s.get("muted", False)),
            "default_position": s.get("default_position") or default_positions[i],
            "captions": cap,
            "default_crop_x_frac": (
                float(dcx) if isinstance(dcx, (int, float)) else None
            ),
        })

    # Resolve effective position for each wide clip (per-clip override beats
    # layer default). Saved on the clip so Phase 2 compositing can read it.
    for c in clips:
        if c.get("wide"):
            c["effective_position"] = (
                c.get("position")
                or track_settings[c.get("track", 0)]["default_position"]
            )
            # Crop: per-clip override > layer default > None (no crop).
            crop = c.get("crop_x_frac")
            if crop is None:
                crop = track_settings[c.get("track", 0)].get(
                    "default_crop_x_frac"
                )
            c["effective_crop_x_frac"] = crop
        # Layer mute overrides per-clip mute.
        if track_settings[c.get("track", 0)]["muted"]:
            c["muted"] = True
        # Captions: per-clip override wins, else inherit from the layer.
        # Resolved value is one of 'none' / 'top' / 'middle' / 'bottom'.
        layer_cap = track_settings[c.get("track", 0)]["captions"]
        ovr = c.get("captions_override", "inherit")
        c["captions_pos"] = layer_cap if ovr == "inherit" else ovr
        c["captions_on"]  = c["captions_pos"] != "none"

    if not clips:
        return jsonify({"error": "No valid clips in video track"}), 400

    # Sort by start_time
    clips.sort(key=lambda c: (c.get("track", 0), c["start_time"], c["stack_order"]))

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

    total_dur = max((c["start_time"] + c["duration"]) for c in clips)
    out_file = date_dir / f"hl-{int(total_dur)}-{counter}.mp4"

    include_intro = data.get("include_intro", True)
    include_outro = data.get("include_outro", True)

    with tempfile.TemporaryDirectory() as tmp:
        # Slice timeline at every clip boundary so each segment has a
        # constant active set; layered compositor handles z-order/slot logic.
        segments = _build_layered_segments(clips)

        # Insert black placeholders for gaps between segments
        full_segments = []
        cursor = 0.0
        for seg in segments:
            if seg["start"] > cursor + 0.05:
                full_segments.append({
                    "start": cursor, "end": seg["start"], "clips": [],
                })
            full_segments.append(seg)
            cursor = seg["end"]

        clip_paths = []
        all_transitions = []
        intro_count = 0

        if include_intro:
            intro = find_asset("intro")
            if intro:
                intro_norm = os.path.join(tmp, "intro_norm.mp4")
                if normalize_clip(str(intro), intro_norm):
                    clip_paths.append(intro_norm)
                    all_transitions.append("fade")
                    intro_count = 1

        for si, seg in enumerate(full_segments):
            seg_clips = seg["clips"]
            seg_start = seg["start"]
            seg_dur = seg["end"] - seg_start

            # Empty segment = gap → black placeholder
            if not seg_clips:
                gap_path = os.path.join(tmp, f"gap{si:03d}.mp4")
                if generate_placeholder(gap_path, seg_dur, "black"):
                    clip_paths.append(gap_path)
                    if len(clip_paths) > 1:
                        all_transitions.append(None)
                continue

            # Build placement records for layered compositing.
            placements = []
            for c in seg_clips:
                clip_offset = seg_start - c["start_time"]
                placements.append({
                    "source_path": c["video_file"],
                    "source_start": c["start"] + clip_offset,
                    "source_dur": seg_dur,
                    "is_wide": bool(c.get("wide")),
                    "layer": int(c.get("track", 0)),
                    "position": c.get("effective_position", "top"),
                    "muted": bool(c.get("muted")),
                    "stack_order": int(c.get("stack_order", 0)),
                    "crop_x_frac": c.get("effective_crop_x_frac"),
                    # Free-mode multi-rectangle composite. Overrides
                    # crop_x_frac when present (handled in renderer).
                    "free_crops": c.get("free_crops") or None,
                })

            seg_out = os.path.join(tmp, f"seg{si:03d}_layered.mp4")
            if composite_layered_segment(placements, seg_dur, seg_out):
                # Captions: burn AFTER compositing so they overlay the final
                # 1080x1920 frame at top/middle/bottom of the actual screen,
                # independent of how the compositor cropped or split-screened
                # any wide source. Each captioned clip contributes its own
                # transcript segments tagged with that clip's position; the
                # PNG renderer respects the per-segment override.
                cap_segs = []
                for c in seg_clips:
                    if not (c.get("captions_on") and c.get("video_id")):
                        continue
                    from db import get_transcript_for_clip
                    clip_offset = seg_start - c["start_time"]
                    src_start   = c["start"] + clip_offset
                    src_end     = src_start + seg_dur
                    tx = get_transcript_for_clip(
                        c["video_id"], src_start, src_end,
                    )
                    cap_pos = c.get("captions_pos") or "bottom"
                    for s in tx:
                        cap_segs.append({
                            "start":    s["start"],
                            "end":      s["end"],
                            "text":     s["text"],
                            "position": cap_pos,
                        })
                if cap_segs:
                    from captions import burn_captions
                    seg_capped = os.path.join(tmp, f"seg{si:03d}_capped.mp4")
                    if burn_captions(seg_out, seg_capped, cap_segs,
                                     style={"color": "#FFFFFF",
                                            "bg":    "#000000",
                                            "bg_alpha": 30}):
                        seg_out = seg_capped

                clip_paths.append(seg_out)
                if len(clip_paths) > 1:
                    # Use trans_in from the lowest-layer / first-listed clip.
                    trans_in = seg_clips[0].get("trans_in")
                    all_transitions.append(trans_in)

        outro_added = False
        if include_outro:
            outro = find_asset("outro")
            if outro:
                outro_norm = os.path.join(tmp, "outro_norm.mp4")
                if normalize_clip(str(outro), outro_norm):
                    clip_paths.append(outro_norm)
                    outro_added = True
                    if len(clip_paths) > 1:
                        all_transitions.append("fade")

        if len(clip_paths) < 1:
            return jsonify({"error": "No clips could be extracted"}), 500

        # Pad transitions list
        while len(all_transitions) < len(clip_paths) - 1:
            all_transitions.append(None)
        all_transitions = all_transitions[:len(clip_paths) - 1]

        if len(clip_paths) == 1:
            assembled = clip_paths[0]
        else:
            assembled = os.path.join(tmp, "assembled.mp4")
            if not concatenate_clips(clip_paths, assembled, all_transitions):
                return jsonify({"error": "Video assembly failed"}), 500

        video_dur = get_video_duration(assembled)

        # Music overlay from sound_track
        if sound_track:
            segments = []
            for s in sound_track:
                mpath = music_lookup.get(s.get("name"))
                if mpath:
                    segments.append({
                        "start": s.get("start_time", 0),
                        "duration": s.get("duration", 10),
                        "music": mpath,
                        "volume": s.get("volume", 3),
                    })
            if segments:
                # Fill gaps with silence
                segments.sort(key=lambda x: x["start"])
                full_segments = []
                cursor = 0.0
                for seg in segments:
                    if seg["start"] > cursor + 0.05:
                        full_segments.append({
                            "start": cursor, "duration": seg["start"] - cursor,
                            "music": None, "volume": 0,
                        })
                    full_segments.append(seg)
                    cursor = seg["start"] + seg["duration"]
                if cursor < video_dur:
                    full_segments.append({
                        "start": cursor, "duration": video_dur - cursor,
                        "music": None, "volume": 0,
                    })

                music_track_path = os.path.join(tmp, "music_track.m4a")
                if build_music_track(full_segments, video_dur, music_track_path):
                    with_music = os.path.join(tmp, "with_music.mp4")
                    if overlay_music_track(assembled, music_track_path,
                                           with_music, full_segments):
                        assembled = with_music

        # Text overlays — offset by intro duration so overlays
        # only appear on user clips, not on intro/outro
        intro_offset = 0.0
        if intro_count:
            intro_offset = get_video_duration(clip_paths[0])
        outro_dur = 0.0
        if outro_added:
            outro_dur = get_video_duration(clip_paths[-1])

        if text_overlays:
            overlays = []
            for t in text_overlays:
                if t.get("text", "").strip():
                    raw_start = t.get("start_time", 0)
                    raw_end = t.get("end_time", 3)
                    ov = {
                        "text": t["text"],
                        "start_time": raw_start + intro_offset,
                        "end_time": min(raw_end + intro_offset,
                                        video_dur - outro_dur),
                        "position": t.get("position", "bottom"),
                        "fontsize": t.get("fontsize", 42),
                        "fontcolor": t.get("fontcolor", "white"),
                        "box_opacity": t.get("box_opacity", 0.5),
                    }
                    if "x_frac" in t and "y_frac" in t:
                        ov["x_frac"] = t["x_frac"]
                        ov["y_frac"] = t["y_frac"]
                    if "w_frac" in t and "h_frac" in t:
                        ov["w_frac"] = t["w_frac"]
                        ov["h_frac"] = t["h_frac"]
                    if t.get("fontfamily"):
                        ov["fontfamily"] = t["fontfamily"]
                    if t.get("bold"):
                        ov["bold"] = True
                    if t.get("italic"):
                        ov["italic"] = True
                    if t.get("bgcolor"):
                        ov["bgcolor"] = t["bgcolor"]
                    if t.get("trans_in"):
                        ov["trans_in"] = t["trans_in"]
                    if t.get("trans_out"):
                        ov["trans_out"] = t["trans_out"]
                    overlays.append(ov)
            if overlays:
                with_text = os.path.join(tmp, "with_text.mp4")
                if add_multiple_text_overlays(assembled, overlays, with_text):
                    assembled = with_text

        # Copy final to output
        shutil.copy2(assembled, str(out_file))

    final_dur = get_video_duration(str(out_file))

    # Save the COMPLETE editable state, not just the rendered tracks. Loading
    # the video back into the builder must reproduce every per-clip flag,
    # layer setting, and editor toggle so the user can keep editing.
    save_timeline = {
        "video_track": video_track,
        "sound_track": sound_track,
        "text_overlays": text_overlays,
        "track_settings": data.get("track_settings"),
        "track_count": data.get("track_count"),
        "track_sequential": data.get("track_sequential"),
        "include_intro": data.get("include_intro", True),
        "include_outro": data.get("include_outro", True),
    }
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
<title>ClipBuilder - Builder</title>
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
/* -- Secondary toolbar -- */
.sub-toolbar{
  display:flex;align-items:center;gap:12px;
  padding:8px 24px;flex-shrink:0;
  background:#111;border-bottom:1px solid #2a2a2a;
}
select,button{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 12px;font-size:13px;cursor:pointer;
}
select:hover,button:hover{border-color:#666}
select:focus,button:focus{outline:none;border-color:#e53935}
#clip-count{font-size:13px;color:#888}

/* -- File filter (multi-select dropdown next to Tag filter) -- */
.file-filter{position:relative;display:inline-block}
.file-filter > button{
  display:inline-flex;align-items:center;gap:6px;
  background:#222;color:#e0e0e0;border:1px solid #444;
  border-radius:6px;padding:6px 10px;font-size:13px;cursor:pointer;
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
.ff-item .ff-name{
  flex:1;white-space:normal;word-break:break-word;
  line-height:1.35;
}
.ff-item .ff-count{color:#666;font-size:10px;flex-shrink:0}
nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
  border:1px solid #444;border-radius:6px}
nav a:hover{color:#fff;border-color:#888}
nav a.active{color:#e53935;border-color:#e53935}

/* -- Main grid -- */
main{flex:1;overflow-y:auto;padding:12px;min-height:0}
#clip-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;
}

/* -- Mac-style 3-column scene browser -- */
.bb-search-row{
  display:flex;align-items:center;gap:12px;
  padding:8px 24px;flex-shrink:0;
  background:#111;border-bottom:1px solid #2a2a2a;
}
.bb-search-row input{
  flex:1;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;
  border-radius:6px;padding:7px 12px;font-size:13px;outline:none;
}
.bb-search-row input:focus{border-color:#1976d2}
.bb-search-status{font-size:11px;color:#1976d2;font-weight:600;white-space:nowrap}
main.bb-cols{
  flex:1;display:grid;grid-template-columns:200px 240px 220px 1fr;
  min-height:0;padding:0;overflow:hidden;
  background:#0a0a0a;border-bottom:1px solid #1a1a1a;
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
.bb-row-name{
  flex:1;min-width:0;word-break:break-word;
}
.bb-row-count{
  color:#666;font-size:10px;flex-shrink:0;
  font-family:'SF Mono',Menlo,monospace;
}
.bb-row.selected .bb-row-count{color:#9ec0e8}
.bb-scenes-wrap{flex:1;overflow-y:auto;padding:10px;min-height:0}
.bb-empty{
  color:#666;font-size:13px;text-align:center;padding:40px 16px;
}

/* Folders column extras */
.bb-col-head .bb-col-head-action{
  background:transparent;border:1px solid #2e2e3e;color:#aaa;
  border-radius:4px;padding:0;width:18px;height:18px;line-height:14px;
  text-align:center;font-size:13px;cursor:pointer;
  text-transform:none;letter-spacing:0;font-weight:400;flex-shrink:0;
}
.bb-col-head .bb-col-head-action:hover{border-color:#1976d2;color:#fff}
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

/* -- Heart icon (file rows + clip cards) -- */
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
.clip-card .pg-heart{
  position:absolute;top:6px;right:6px;z-index:3;
  background:rgba(0,0,0,.55);border-radius:50%;
  width:26px;height:26px;
}
.clip-card .pg-heart svg{width:14px;height:14px}
.clip-card .pg-heart.on{background:rgba(229,57,53,.85);color:#fff}
.clip-card{
  background:#1a1a1a;border-radius:8px;overflow:hidden;
  cursor:grab;transition:transform .15s,opacity .2s;position:relative;
}
.clip-card:hover{transform:translateY(-2px)}
.clip-card:active{cursor:grabbing}
.clip-card.in-tl{opacity:.3;pointer-events:none}
.clip-card.sortable-ghost{opacity:.4}
#drag-preview{
  position:absolute;top:-9999px;left:-9999px;
  width:70px;height:50px;border-radius:4px;overflow:hidden;
  pointer-events:none;
}
#drag-preview img{width:100%;height:100%;object-fit:cover;display:block}
.clip-card .thumb{
  width:100%;aspect-ratio:9/16;object-fit:cover;display:block;background:#111;
}
.clip-card .dur{
  position:absolute;top:6px;right:6px;
  background:rgba(0,0,0,.75);color:#fff;font-size:11px;font-weight:600;
  padding:2px 6px;border-radius:4px;
}
.clip-card .play-overlay{
  position:absolute;top:0;left:0;right:0;bottom:36px;
  display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.35);opacity:0;transition:opacity .2s;
  cursor:pointer;z-index:2;
}
.clip-card:hover .play-overlay{opacity:1}
.clip-card.in-tl .play-overlay{display:none}
.clip-card.ignored .play-overlay{display:none}
.clip-card .play-overlay .play-circle{
  width:36px;height:36px;background:rgba(229,57,53,.9);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  transition:transform .15s,background .15s;
}
.clip-card .play-overlay .play-circle:hover{transform:scale(1.15);background:#e53935}
.clip-card .play-overlay .play-circle svg{width:16px;height:16px;fill:#fff;margin-left:2px}
.clip-card .scene-video{
  position:absolute;top:0;left:0;width:100%;height:calc(100% - 36px);
  object-fit:cover;z-index:3;background:#000;border-radius:8px 8px 0 0;
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
  padding:10px 16px 31px;flex-shrink:0;overflow-y:auto;
}
.tl-hdr{
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:4px;font-size:13px;
}
.tl-hdr .lbl{color:#e53935;font-weight:600}
.tl-hdr .tot{color:#888}

/* -- Multi-track timeline -- */
#multi-timeline{
  background:#0a0a0a;border:1px solid #2a2a2a;border-radius:8px;
  overflow-x:auto;overflow-y:hidden;position:relative;
}
#tl-grid{
  display:grid;
  grid-template-columns:90px 1fr;
  grid-auto-rows:auto;
  min-width:100%;
}
/* Time ruler */
#time-ruler{
  grid-column:2;height:20px;position:sticky;top:0;
  background:#111;border-bottom:1px solid #2a2a2a;z-index:5;
  display:flex;align-items:flex-end;
}
.ruler-tick{
  position:absolute;bottom:0;width:1px;height:8px;background:#333;
}
.ruler-tick.major{height:14px;background:#555}
.ruler-label{
  position:absolute;bottom:4px;font-size:8px;color:#666;
  transform:translateX(-50%);font-weight:600;
}
/* Track labels */
.track-label{
  font-size:9px;color:#666;font-weight:700;text-transform:uppercase;
  letter-spacing:1px;background:#111;border-right:1px solid #2a2a2a;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  padding:2px 4px;user-select:none;
}
/* Layer header — name + mute + hamburger position picker + free/seq mode */
.track-header{
  background:#111;border-right:1px solid #2a2a2a;
  display:flex;flex-direction:column;align-items:stretch;justify-content:center;
  gap:4px;padding:4px 6px;user-select:none;
}
.track-header .th-name{
  font-size:9px;color:#888;font-weight:800;letter-spacing:1.2px;
  text-align:center;text-transform:uppercase;
}
.track-header.muted .th-name{color:#5a3030;text-decoration:line-through}
.track-header .th-row{display:flex;gap:3px;align-items:center;justify-content:center}
.track-header .th-btn{
  width:18px;height:18px;border-radius:3px;border:1px solid #2a2a2a;
  background:#181818;color:#888;cursor:pointer;font-size:10px;line-height:1;
  display:inline-flex;align-items:center;justify-content:center;padding:0;
}
.track-header .th-btn:hover{border-color:#444;color:#ddd}
.track-header .th-btn.active{background:#3a1818;border-color:#e53935;color:#ff6b6b}
.track-header .th-btn svg{display:block}

/* Captions cycle button: blue tint when in any non-'none' state.
   Each position uses a distinct accent so the user can tell at a glance
   whether captions are on, off, or which band they sit in. */
.track-header .th-btn.cap-btn.cap-bottom,
.track-header .th-btn.cap-btn.cap-middle,
.track-header .th-btn.cap-btn.cap-top{
  background:#0d2540;border-color:#1976d2;color:#90caf9;
}
.track-header .th-btn.cap-btn.cap-none{color:#555}

/* Wide-position cycle button: dim until non-default; the icon itself
   indicates which slot (top/center/bottom) is active. */
.track-header .th-btn.pos-btn{color:#bbb}
.track-header .th-btn.pos-btn:hover{color:#fff;border-color:#555}

/* Compact Free/Seq toggle inside the track header */
.th-mode{
  display:flex;align-items:center;justify-content:center;gap:4px;
  font-size:8px;color:#888;letter-spacing:.5px;
}
.th-mode .th-mode-lbl{font-weight:700}
.th-mode .th-mode-lbl.on{color:#ddd}
.th-mode .th-tog{
  position:relative;width:22px;height:11px;border-radius:6px;
  background:#2a2a2a;cursor:pointer;flex-shrink:0;
}
.th-mode .th-tog.active{background:#e53935}
.th-mode .th-tog .th-knob{
  position:absolute;top:1px;left:1px;width:9px;height:9px;border-radius:50%;
  background:#fff;transition:left .15s;
}
.th-mode .th-tog.active .th-knob{left:12px}
/* Tracks */
.track{
  position:relative;border-bottom:1px solid #1a1a1a;
  background:#0d0d0d;min-height:40px;
}
.video-track{min-height:70px;overflow:hidden}
.video-track:last-child{border-bottom:none}
.video-track > .clip-card{display:none !important}
.video-track-0{background:#0d0d0d}
.video-track-1{background:#111114}
.video-track-2{background:#14140f}
.video-track-row{display:none}
.video-track-row.active{display:contents}
/* Highlighted while a clip is dragged over it from another layer */
.video-track.drop-target{
  box-shadow:inset 0 0 0 2px #e53935;
  background:#1a0a0a;
}
#sound-track{background:#0c0e0c;z-index:2}
#text-track{background:#0c0c0e;z-index:2}
.vt-group{display:flex;gap:2px;margin-left:4px}
.vt-group-btn{
  font-size:9px;font-weight:700;color:#555;background:#1a1a1a;border:1px solid #333;
  border-radius:4px;padding:2px 6px;cursor:pointer;line-height:1.2;
}
.vt-group-btn:hover{color:#aaa;border-color:#555}
.vt-group-btn.active{color:#e53935;border-color:#e53935;background:#1a1012}
.track-empty{
  position:absolute;left:60px;top:50%;transform:translateY(-50%);
  color:#333;font-size:10px;pointer-events:none;white-space:nowrap;
}

/* -- Track blocks -- */
.track-block{
  position:absolute;top:2px;height:calc(100% - 4px);
  border-radius:4px;cursor:grab;overflow:hidden;
  display:flex;align-items:center;padding:0 6px;
  font-size:10px;font-weight:600;white-space:nowrap;
  transition:box-shadow .15s;user-select:none;
  min-width:20px;
}
.track-block:hover{box-shadow:0 0 8px rgba(255,255,255,.08)}
.track-block:active{cursor:grabbing}
.track-block .blk-label{flex:1;overflow:hidden;text-overflow:ellipsis}
.track-block .blk-rm{
  position:absolute;top:1px;right:2px;
  background:none;border:none;color:inherit;opacity:.4;
  font-size:24px;cursor:pointer;padding:0;line-height:1;
}
.track-block .blk-rm:hover{opacity:1}
.track-block .resize-handle{
  position:absolute;right:0;top:0;width:6px;height:100%;
  cursor:ew-resize;background:transparent;
}
.track-block .resize-handle:hover{background:rgba(255,255,255,.15)}

/* Video blocks */
.vblock{background:#1e1e1e;border:1px solid #333;color:#ccc;z-index:1}
.vblock-is-wide{border-color:#5a5a2a}
/* Insertion indicator */
.vt-insert-bar{
  position:absolute;top:2px;width:3px;height:calc(100% - 4px);
  background:#fff;border-radius:2px;z-index:10000;pointer-events:none;
  display:none;box-shadow:0 0 8px rgba(255,255,255,.7);
}
.vblock .vblock-thumb{
  position:absolute;left:0;top:0;width:100%;height:100%;
  object-fit:cover;opacity:.5;border-radius:4px;
}
.vblock .blk-label{
  position:absolute;top:4px;right:4px;z-index:1;
  background:rgba(0,0,0,.7);padding:1px 6px;border-radius:3px;
  font-size:10px;text-shadow:none;color:#fff;line-height:18px;
}
.vblock .blk-rm{
  position:absolute;top:4px;right:4px;z-index:2;
  color:#fff;opacity:.7;font-size:20px;line-height:18px;
  margin-left:4px;
}
.vblock .blk-rm:hover{opacity:1}
/* X delete is no longer inline — reset duration label to corner. */
.vblock .blk-label{right:4px}
.vblock .vblk-vol{
  position:absolute;bottom:4px;left:4px;z-index:1;
  display:flex;gap:1px;align-items:flex-end;height:14px;
}
.vblock .vblk-vol .vv{
  width:4px;border-radius:1px;cursor:pointer;opacity:.3;background:#fff;
}
.vblock .vblk-vol .vv.active{opacity:.9}
/* Video transition pills */
.vtrans{
  position:absolute;bottom:2px;
  background:rgba(60,60,60,.85);color:#aaa;font-size:8px;font-weight:600;
  padding:1px 4px;border-radius:3px;cursor:pointer;z-index:2;
  max-width:50px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.vtrans.vt-in{left:4px}
.vtrans.vt-out{right:20px}
.vtrans:hover{background:#555;color:#fff}

.vblock-wide{
  position:absolute;top:4px;left:4px;z-index:1;
  width:16px;height:11px;border:1.5px solid rgba(255,255,255,.8);border-radius:2px;
}
.vblock-wide::after{
  content:'';position:absolute;top:1.5px;left:3px;
  width:8px;height:5px;background:rgba(255,255,255,.8);border-radius:1px;
}

/* Block status icons (read-only — full editor opens via click).
 * Sits at the bottom-left of the block; each icon gets the same black
 * pill background as the duration label. */
.vblk-icons{
  position:absolute;bottom:4px;left:4px;z-index:2;
  display:flex;gap:3px;align-items:center;pointer-events:none;
}
.vblk-icons .ic{
  display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;border-radius:3px;
  background:rgba(0,0,0,.7);color:#fff;
}
.vblk-icons .ic svg{width:13px;height:13px;display:block}
.vblk-icons .ic.ic-mute{color:#ff8888}
.vblk-icons .ic-pos{
  flex-direction:column;gap:1.5px;padding:3px;box-sizing:border-box;
}
.vblk-icons .ic-pos .ic-pos-stripe{
  flex:1;width:100%;background:#888;border-radius:1px;min-height:1.5px;
}
.vblk-icons .ic-pos .ic-pos-stripe.sel{background:#e53935}

/* Scene editor popover */
.scene-editor{
  position:fixed;z-index:1000;background:#1c1c20;border:1px solid #333;
  border-radius:10px;padding:14px 16px;min-width:240px;max-width:280px;
  box-shadow:0 10px 30px rgba(0,0,0,.6);
  display:none;color:#ddd;font-size:13px;
}
.scene-editor.active{display:block;animation:seFadeIn .12s ease-out}
@keyframes seFadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.scene-editor .se-row{
  display:flex;align-items:center;justify-content:space-between;
  gap:10px;margin:8px 0;
}
.scene-editor .se-label{
  font-size:10px;font-weight:700;color:#888;letter-spacing:1px;
  text-transform:uppercase;flex-shrink:0;
}
.scene-editor .se-title{
  font-size:11px;font-weight:700;color:#fff;letter-spacing:1px;
  text-transform:uppercase;margin:0 0 8px 0;display:flex;
  justify-content:space-between;align-items:center;
}
.scene-editor .se-close{
  background:none;border:none;color:#888;font-size:18px;
  line-height:1;cursor:pointer;padding:0 4px;
}
.scene-editor .se-close:hover{color:#fff}
.scene-editor .se-pos-group{display:flex;gap:4px}
.scene-editor .se-pos-btn{
  background:#26262c;border:1px solid #3a3a44;color:#bbb;
  padding:6px 10px;border-radius:5px;font-size:11px;font-weight:600;
  cursor:pointer;letter-spacing:.5px;
}
.scene-editor .se-pos-btn:hover{border-color:#555;color:#fff}
.scene-editor .se-pos-btn.sel{background:#e53935;border-color:#e53935;color:#fff}
.scene-editor .se-pos-btn.sel-default{border-color:#5a3a3a;color:#ffb0b0}
.scene-editor .se-trans{
  background:#26262c;border:1px solid #3a3a44;color:#ddd;
  padding:6px 12px;border-radius:5px;cursor:pointer;font-size:12px;
  font-family:inherit;min-width:90px;text-align:left;
}
.scene-editor .se-trans:hover{border-color:#555}
.scene-editor .se-trans .se-trans-clear{
  float:right;color:#888;font-size:11px;margin-left:6px;
}
.scene-editor .se-mute-btn{
  background:#26262c;border:1px solid #3a3a44;color:#bbb;
  padding:6px 14px;border-radius:5px;font-size:11px;font-weight:600;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
}
.scene-editor .se-mute-btn.muted{background:#3a1818;border-color:#e53935;color:#ff8888}
.scene-editor .se-mute-btn:hover{border-color:#555}
.scene-editor .se-mute-btn svg{width:12px;height:12px}
.scene-editor .se-cap-cycle{
  background:#26262c;border:1px solid #3a3a44;color:#bbb;
  padding:6px 12px;border-radius:5px;font-size:11px;font-weight:600;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
}
.scene-editor .se-cap-cycle:hover{border-color:#555}
.scene-editor .se-cap-cycle.cap-bottom,
.scene-editor .se-cap-cycle.cap-middle,
.scene-editor .se-cap-cycle.cap-top{
  background:#0d2540;border-color:#1976d2;color:#90caf9;
}
.scene-editor .se-cap-cycle.cap-none{color:#666}
.scene-editor .se-cap-cycle svg{width:14px;height:14px;flex-shrink:0}
.scene-editor .se-delete{
  width:100%;margin-top:10px;padding:8px;background:#2a1414;
  border:1px solid #5a2020;border-radius:5px;color:#ff8888;
  font-weight:600;font-size:12px;cursor:pointer;letter-spacing:.5px;
  text-transform:uppercase;
}
.scene-editor .se-delete:hover{background:#3a1818;border-color:#e53935;color:#fff}
.scene-editor .se-crop-btn{
  background:#26262c;border:1px solid #3a3a44;color:#bbb;
  padding:6px 12px;border-radius:5px;font-size:11px;font-weight:600;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
}
.scene-editor .se-crop-btn:hover{border-color:#555}
.scene-editor .se-crop-btn.has-crop{background:#0d2540;border-color:#1976d2;color:#90caf9}
.scene-editor .se-crop-btn.layer-default{color:#ffb0b0;border-color:#5a3a3a}

/* Crop modal */
.crop-stage{
  display:flex;justify-content:center;align-items:center;
  background:#0a0a0a;border-radius:8px;padding:14px;
}
.crop-thumb-wrap{
  position:relative;display:inline-block;user-select:none;
  max-width:100%;
}
.crop-thumb-wrap img{
  display:block;max-width:480px;width:100%;height:auto;border-radius:4px;
  pointer-events:none;
}
.crop-mask{
  position:absolute;top:0;bottom:0;background:rgba(0,0,0,.6);
  pointer-events:none;
}
.crop-frame{
  position:absolute;top:0;bottom:0;
  border:2px solid #2196f3;box-sizing:border-box;
  box-shadow:0 0 0 1px rgba(0,0,0,.4);
  cursor:grab;
}
.crop-frame:active{cursor:grabbing}
.crop-frame::after{
  content:'9:16';position:absolute;top:6px;left:6px;
  background:rgba(33,150,243,.85);color:#fff;font-size:10px;font-weight:700;
  padding:2px 6px;border-radius:3px;letter-spacing:.5px;
}
/* Track-header crop button */
.th-btn.crop-btn.active{color:#90caf9}

/* -- Crop modal: mode tabs + free-mode panes -- */
.crop-mode-tabs{
  display:flex;gap:4px;margin:0 0 12px 0;
  border-bottom:1px solid #2a2a2a;
}
.crop-mode-tabs button{
  background:transparent;border:none;color:#888;font-size:12px;font-weight:600;
  padding:8px 16px;letter-spacing:.5px;cursor:pointer;
  border-bottom:2px solid transparent;margin-bottom:-1px;text-transform:uppercase;
}
.crop-mode-tabs button:hover{color:#fff}
.crop-mode-tabs button.active{color:#fff;border-bottom-color:#2196f3}

.cf-toolbar{
  display:flex;align-items:center;gap:6px;
  padding:6px 8px;background:#111;border-radius:6px;margin-bottom:10px;
}
.cf-toolbar .cf-btn{
  background:#1a1a1a;border:1px solid #333;color:#ddd;border-radius:4px;
  padding:4px 10px;font-size:13px;line-height:1;cursor:pointer;
}
.cf-toolbar .cf-btn:hover{border-color:#2196f3;color:#fff}
.cf-toolbar .cf-sep{width:1px;height:18px;background:#2a2a2a;margin:0 4px}
.cf-toolbar .cf-status{font-size:11px;color:#888;margin-left:8px;flex:1;text-align:right}

/* Flex (not grid) so each pane's width is derived from aspect-ratio *
 * height, not forced to share an equal column width. Without this, a   *
 * 9:16 pane and a 16:9 pane both forced to identical column widths     *
 * end up with the same on-screen shape, breaking the aspect lock that  *
 * the rectangle math is built on. */
.cf-stage{display:flex;gap:14px;justify-content:center;align-items:flex-start}
.cf-pane{display:flex;flex-direction:column;gap:6px;min-width:0}
.cf-pane-label{
  font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.6px;
  font-weight:700;
}
.cf-src-wrap,.cf-dst-wrap{
  position:relative;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:6px;
  overflow:hidden;user-select:none;
  /* Height is the load-bearing constraint; width derives from aspect.  *
   * Cap at 60vh so the modal stays usable on short windows.            */
  height:min(60vh,460px);width:auto;
}
.cf-src-wrap{aspect-ratio:16/9}     /* default; overridden once we know src aspect */
.cf-dst-wrap{aspect-ratio:9/16}
.cf-src-wrap img{
  width:100%;height:100%;object-fit:cover;display:block;pointer-events:none;
}
.cf-rects{position:absolute;inset:0;pointer-events:none}
.cf-rect{
  position:absolute;border:3px solid #e53935;box-sizing:border-box;
  pointer-events:auto;cursor:grab;
  background:rgba(255,255,255,0.03);
}
.cf-rect.selected{box-shadow:0 0 0 2px #fff;outline:2px solid #fff}
.cf-rect:active{cursor:grabbing}
.cf-rect .cf-handle{
  position:absolute;width:12px;height:12px;background:#fff;border:2px solid #111;
  border-radius:50%;
}
.cf-rect .cf-handle.h-nw{left:-6px;top:-6px;cursor:nwse-resize}
.cf-rect .cf-handle.h-ne{right:-6px;top:-6px;cursor:nesw-resize}
.cf-rect .cf-handle.h-sw{left:-6px;bottom:-6px;cursor:nesw-resize}
.cf-rect .cf-handle.h-se{right:-6px;bottom:-6px;cursor:nwse-resize}
.cf-rect .cf-label{
  position:absolute;top:-22px;left:-3px;
  font-size:10px;color:#fff;background:#222;padding:1px 6px;border-radius:3px;
  font-family:'SF Mono',Menlo,monospace;letter-spacing:.5px;
}
.cf-dst-wrap .cf-rect{background-size:cover;background-position:center;background-repeat:no-repeat}
.cf-dst-wrap .cf-rect .cf-handle.h-nw,
.cf-dst-wrap .cf-rect .cf-handle.h-ne,
.cf-dst-wrap .cf-rect .cf-handle.h-sw{display:none}   /* only SE handle on dst (uniform scale) */

/* Preset picker rows */
.cf-preset-row{
  display:flex;align-items:center;gap:10px;padding:10px 12px;
  border:1px solid #2a2a2a;border-radius:6px;margin:6px 4px;
  cursor:pointer;background:#10131a;
}
.cf-preset-row:hover{border-color:#3a4860;background:#161b27}
.cf-preset-row .cf-preset-name{flex:1;color:#e0e0e0;font-size:13px;font-weight:600}
.cf-preset-row .cf-preset-meta{font-size:11px;color:#666}
.cf-preset-row .cf-preset-del{
  background:transparent;border:none;color:#666;cursor:pointer;font-size:18px;
  padding:0 6px;line-height:1;
}
.cf-preset-row .cf-preset-del:hover{color:#ef5350}

/* Sound blocks */
.sblock{border:1px solid rgba(255,255,255,.15);color:#fff}
.sblock .blk-vol{
  display:flex;gap:2px;align-items:flex-end;height:18px;
  margin-left:auto;margin-right:24px;flex-shrink:0;
}
.sblock .blk-vol .vb{
  width:6px;border-radius:1px;cursor:pointer;opacity:.35;
}
.sblock .blk-vol .vb.active{opacity:1}

/* Text overlay blocks */
.tblock{background:#1a1a2e;border:1px solid #4040a0;color:#aac}
.tblock-trans{
  position:absolute;bottom:2px;
  background:rgba(60,60,140,.8);color:#99a;font-size:8px;font-weight:600;
  padding:1px 4px;border-radius:3px;cursor:pointer;z-index:1;
}
.tblock-trans.tt-in{left:4px}
.tblock-trans.tt-out{right:20px}
.tblock-trans:hover{background:#4040a0;color:#fff}

/* Transition markers */
.trans-marker{
  position:absolute;top:50%;transform:translate(-50%,-50%);
  background:#333;border:1px solid #555;border-radius:6px;
  padding:4px 8px;font-size:11px;color:#aaa;z-index:2;
  cursor:pointer;white-space:nowrap;font-weight:600;
}
.trans-marker:hover{background:#444;color:#fff}

/* (source rows removed) */

/* -- Context menu -- */
.ctx-menu{
  position:fixed;z-index:200;background:#222;border:1px solid #444;
  border-radius:6px;padding:4px 0;min-width:120px;box-shadow:0 4px 16px rgba(0,0,0,.6);
}
.ctx-menu div{
  padding:6px 14px;font-size:13px;cursor:pointer;color:#e0e0e0;
}
.ctx-menu div:hover{background:#333}

/* -- Hide button -- */
.clip-card .hide-btn{
  position:absolute;bottom:38px;right:4px;z-index:4;
  width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.6);border:1px solid rgba(255,255,255,.15);
  color:#888;font-size:14px;line-height:20px;text-align:center;
  cursor:pointer;display:none;transition:all .15s;padding:0;
}
.clip-card:hover .hide-btn{display:block}
.clip-card.ignored .hide-btn{display:block;color:#4caf50;border-color:rgba(76,175,80,.4)}
.clip-card.ignored .hide-btn:hover{background:rgba(76,175,80,.85);color:#fff;border-color:rgba(76,175,80,.85)}
.clip-card.in-tl .hide-btn{display:none !important}
.clip-card .hide-btn:hover{background:rgba(229,57,53,.85);color:#fff;border-color:rgba(229,57,53,.85)}

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

.controls{display:flex;align-items:center;gap:12px;margin-top:6px}
.incl-label{
  font-size:11px;color:#666;font-weight:700;letter-spacing:1px;
  text-transform:uppercase;
}
.tl-check{font-size:12px;color:#aaa;cursor:pointer;display:flex;align-items:center;gap:5px;user-select:none}
.tl-check input{accent-color:#e53935;cursor:pointer}

/* Shared action buttons (Load / Clear / Generate). Same height = 34px. */
.ctl-actions{display:flex;align-items:center;gap:8px;margin-left:auto}
.action-btn{
  height:34px;padding:0 14px;display:inline-flex;align-items:center;gap:6px;
  background:#1a1a24;border:1px solid #2e2e3e;color:#ccc;border-radius:6px;
  font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;
  transition:background .15s,border-color .15s,color .15s;
}
.action-btn svg{width:14px;height:14px;display:block}
.action-btn:hover{background:#22222e;border-color:#444;color:#fff}
.action-btn.primary{
  background:#e53935;border-color:#e53935;color:#fff;padding:0 18px;
}
.action-btn.primary:hover{background:#c62828;border-color:#c62828}
.action-btn:disabled{background:#333;border-color:#333;color:#888;cursor:not-allowed}
.action-btn.primary:disabled{background:#555;border-color:#555}

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

/* -- Transition Picker -- */
#trans-picker-grid{
  display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:12px;
}
#trans-picker-grid .tp-item{
  padding:5px 10px;border-radius:6px;font-size:11px;font-weight:600;
  cursor:pointer;background:#222;color:#aaa;border:1px solid #333;
  transition:all .15s;
}
#trans-picker-grid .tp-item:hover{color:#fff;border-color:#666;background:#333}
#trans-picker-grid .tp-item.current{background:#e53935;color:#fff;border-color:#e53935}

/* -- Music Picker -- */
#music-picker-list{
  display:flex;flex-direction:column;gap:6px;margin-top:12px;max-height:300px;overflow-y:auto;
}
#music-picker-list .mp-item{
  padding:8px 14px;border-radius:6px;font-size:13px;font-weight:600;
  cursor:pointer;display:flex;align-items:center;gap:8px;
  transition:all .15s;
}
#music-picker-list .mp-item:hover{filter:brightness(1.3)}
#music-picker-list .mp-item .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}

/* -- Text Editor Modal -- */
#text-editor-modal .te-container{
  display:flex;flex-direction:column;
  background:#1a1a1a;border-radius:12px;overflow:hidden;
  width:auto;max-width:95vw;
}
.te-toolbar{
  display:flex;align-items:center;gap:8px;
  padding:10px 16px;background:#222;border-bottom:1px solid #333;
  flex-wrap:wrap;
}
.te-font-select{
  background:#111;color:#fff;border:1px solid #444;border-radius:6px;
  padding:4px 8px;font-size:12px;cursor:pointer;max-width:140px;
}
.te-font-select:focus{outline:none;border-color:#e53935}
.te-toolbar input[type="text"]{
  flex:1;min-width:180px;background:#111;color:#fff;border:1px solid #444;
  border-radius:6px;padding:6px 10px;font-size:14px;
}
.te-toolbar input[type="text"]:focus{outline:none;border-color:#e53935}
.te-toolbar input[type="color"]{
  width:32px;height:28px;border:1px solid #444;border-radius:4px;
  background:none;cursor:pointer;padding:0;
}
.te-label{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px}
.te-sep{width:1px;height:20px;background:#333;flex-shrink:0}
.te-btn{
  width:32px;height:28px;border-radius:4px;font-size:14px;
  display:flex;align-items:center;justify-content:center;
}
.te-btn.active{background:#e53935;color:#fff;border-color:#e53935}
.te-add-btn{font-size:18px;font-weight:700;width:32px}
.te-canvas-wrap{
  display:flex;align-items:center;justify-content:center;
  padding:20px;background:#0a0a0a;
}
#te-canvas{
  width:360px;height:640px;
  background:#000;border:1px solid #333;border-radius:4px;
  position:relative;overflow:hidden;cursor:default;
  background-size:cover;background-position:center center;
  background-repeat:no-repeat;
}

/* -- Text overlay preview modal -- */
#text-preview-modal .tp-container{
  background:#0a0a0a;border-radius:12px;overflow:hidden;
  display:flex;flex-direction:column;max-width:95vw;
}
#text-preview-modal .tp-stage{
  position:relative;width:405px;height:720px;background:#000;
  max-width:90vw;max-height:80vh;
  aspect-ratio:9/16;
}
#text-preview-modal .tp-stage video{
  width:100%;height:100%;object-fit:cover;display:block;background:#000;
}
#text-preview-modal .tp-overlays{
  position:absolute;inset:0;pointer-events:none;
}
/* Cloned text boxes drop the editor's outline/cursor decorations. */
#text-preview-modal .tp-overlays .te-box{
  cursor:default;outline:none !important;
}
#text-preview-modal .tp-foot{
  display:flex;align-items:center;gap:10px;padding:10px 14px;
  background:#161616;border-top:1px solid #222;
}
#text-preview-modal .tp-foot-spacer{flex:1}
#text-preview-modal .tp-info{font-size:11px;color:#888}

.te-preview{
  display:inline-flex;align-items:center;gap:6px;
  padding:6px 12px;font-size:12px;font-weight:600;
  background:linear-gradient(135deg,#1976d2,#1565c0);
  color:#fff;border:1px solid #1976d2;border-radius:6px;cursor:pointer;
}
.te-preview:hover{filter:brightness(1.1)}
.te-preview svg{width:13px;height:13px;fill:currentColor}
#te-canvas.te-has-bg{
  /* Subtle dimming so white text reads on bright frames. */
  box-shadow:inset 0 0 0 9999px rgba(0,0,0,.0);
}
#te-canvas.te-has-bg::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background:rgba(0,0,0,.15);
}
.te-box{
  position:absolute;
  color:#fff;font-size:14px;font-weight:400;
  text-align:center;cursor:grab;user-select:none;
  padding:0;border-radius:4px;
  text-shadow:0 2px 6px rgba(0,0,0,.8);
  pointer-events:auto;width:200px;height:60px;
  display:flex;align-items:center;justify-content:center;
}
.te-box:hover{outline:1px dashed rgba(255,255,255,.3)}
.te-box.te-selected{outline:2px solid #e53935}
.te-box:active{cursor:grabbing}
.te-box .te-bg{
  position:absolute;inset:-5px;border-radius:4px;
  background:rgba(0,0,0,0.5);z-index:-1;
}
.te-box .te-box-rm{
  position:absolute;top:-8px;right:-8px;width:16px;height:16px;
  background:#e53935;border:none;border-radius:50%;color:#fff;
  font-size:10px;line-height:16px;text-align:center;cursor:pointer;
  display:none;padding:0;z-index:2;
}
.te-box:hover .te-box-rm{display:block}
.te-box .te-resize{
  position:absolute;bottom:-3px;right:-3px;width:12px;height:12px;
  cursor:nwse-resize;z-index:3;display:none;
}
.te-box .te-resize::before{
  content:'';position:absolute;bottom:2px;right:2px;
  width:8px;height:8px;
  border-right:2px solid rgba(255,255,255,.6);
  border-bottom:2px solid rgba(255,255,255,.6);
}
.te-box:hover .te-resize,.te-box.te-selected .te-resize{display:block}
.te-text-span{display:block;outline:none;word-break:break-word;line-height:1.15;white-space:pre-wrap;padding:2px}
.te-footer{
  display:flex;justify-content:flex-end;gap:10px;
  padding:12px 16px;background:#222;border-top:1px solid #333;
}
.te-cancel{background:#333;color:#ccc;border:1px solid #555;border-radius:6px;padding:8px 20px;font-size:13px;cursor:pointer}
.te-cancel:hover{background:#444}
.te-apply{background:#e53935;color:#fff;border:none;border-radius:6px;padding:8px 24px;font-size:13px;font-weight:600;cursor:pointer}
.te-apply:hover{background:#c62828}
.te-save{
  background:#1a3a1a;color:#9be09b;border:1px solid #2e6b2e;
  border-radius:6px;padding:8px 18px;font-size:13px;font-weight:600;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
}
.te-save:hover{background:#225522;border-color:#3e8a3e;color:#fff}
.te-save svg{width:14px;height:14px}
.te-gallery{
  background:#1a1a24;color:#ccc;border:1px solid #2e2e3e;
  border-radius:6px;padding:8px 14px;font-size:13px;font-weight:600;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
}
.te-gallery:hover{background:#22222e;border-color:#444;color:#fff}
.te-gallery svg{width:14px;height:14px}
.te-footer-spacer{flex:1}

/* Gallery grid */
.tg-grid{
  display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  max-height:60vh;overflow-y:auto;
}
.tg-item{
  position:relative;border:1px solid #2a2a32;border-radius:8px;
  overflow:hidden;cursor:pointer;background:#0a0a0e;
  transition:border-color .15s,transform .15s;
}
.tg-item:hover{border-color:#e53935;transform:translateY(-2px)}
.tg-item img{
  width:100%;height:auto;display:block;aspect-ratio:9/16;object-fit:cover;
}
.tg-item .tg-name{
  position:absolute;left:0;right:0;bottom:0;
  padding:6px 8px;font-size:11px;color:#fff;
  background:linear-gradient(transparent,rgba(0,0,0,.85));
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.tg-item .tg-del{
  position:absolute;top:6px;right:6px;
  width:24px;height:24px;border-radius:4px;
  background:rgba(0,0,0,.7);color:#fff;border:none;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;padding:0;
  opacity:0;transition:opacity .15s,background .15s;
}
.tg-item:hover .tg-del{opacity:1}
.tg-item .tg-del:hover{background:#e53935}
.tg-item .tg-del svg{width:14px;height:14px}
.tg-empty{padding:30px;text-align:center;color:#666;font-size:13px}
</style>
</head>
<body>

<!-- pg-chrome -->

<!-- Hidden legacy controls — kept so existing JS that reads
     tag-filter.value / writes options to it doesn't need to be ripped
     out. The new column-based browser drives all filtering. -->
<select id="tag-filter" style="display:none"><option value="">All Tags</option></select>

<div class="bb-search-row">
  <input type="search" id="bb-search"
         placeholder="Search scene transcripts… (e.g. a phrase the speaker said)"
         oninput="onBbSearchInput()" autocomplete="off">
  <span class="bb-search-status" id="bb-search-status"></span>
  <span id="clip-count"></span>
</div>

<main class="bb-cols">
  <div class="bb-col bb-folders">
    <div class="bb-col-head">
      Folders
      <button type="button" class="bb-col-head-action" title="New folder"
              onclick="bbCreateFolder()">+</button>
    </div>
    <div class="bb-list" id="bb-folders-list"></div>
  </div>
  <div class="bb-col bb-files">
    <div class="bb-col-head">Files</div>
    <div class="bb-list" id="bb-files-list"></div>
  </div>
  <div class="bb-col bb-tags">
    <div class="bb-col-head">Tags</div>
    <div class="bb-list" id="bb-tags-list"></div>
  </div>
  <div class="bb-col bb-scenes">
    <div class="bb-col-head" id="bb-scenes-head">Scenes</div>
    <div class="bb-scenes-wrap">
      <div id="clip-grid"></div>
    </div>
  </div>
</main>

<footer>
  <div class="tl-hdr">
    <span class="lbl">TIMELINE</span>
    <input type="file" id="load-input" accept=".json,.mp4,.mov,.avi,.mkv,.webm,.m4v" style="display:none" onchange="handleLoadFile(event)">
    <span class="tot" id="tl-total">0 clips &mdash; 0.0s</span>
  </div>
  <div id="multi-timeline">
    <div id="tl-grid">
      <div id="time-ruler"></div>
      <div class="track-label">SND</div>
      <div id="sound-track" class="track"><span class="track-empty">click to add music</span></div>
      <div class="track-label">TXT</div>
      <div id="text-track" class="track"><span class="track-empty">click to add text overlay</span></div>
      <div class="track-header" id="track-header-0" data-track="0"></div>
      <div id="video-track-0" class="track video-track video-track-0"><span class="track-empty">drag any clip here</span><div class="vt-insert-bar"></div></div>
      <div class="video-track-row" id="vt-row-1"><div class="track-header" id="track-header-1" data-track="1"></div><div id="video-track-1" class="track video-track video-track-1"><span class="track-empty">drag any clip here</span><div class="vt-insert-bar"></div></div></div>
      <div class="video-track-row" id="vt-row-2"><div class="track-header" id="track-header-2" data-track="2"></div><div id="video-track-2" class="track video-track video-track-2"><span class="track-empty">drag any clip here</span><div class="vt-insert-bar"></div></div></div>
    </div>
  </div>
  <div class="controls">
    <span class="incl-label">Include</span>
    <label class="tl-check"><input type="checkbox" id="include-intro" checked/> Intro Video</label>
    <label class="tl-check"><input type="checkbox" id="include-outro" checked/> Outro Video</label>
    <span style="font-size:12px;color:#888;margin-left:6px">Video Timelines</span>
    <div class="vt-group"><button class="vt-group-btn active" onclick="setTrackCount(1)">I</button><button class="vt-group-btn" onclick="setTrackCount(2)">II</button><button class="vt-group-btn" onclick="setTrackCount(3)">III</button></div>
    <div class="ctl-actions">
      <button class="action-btn" onclick="triggerLoad()" title="Load timeline">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>
        <span>Load</span>
      </button>
      <button class="action-btn" id="clear-btn" onclick="clearTimeline()" title="Clear timeline">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
        <span>Clear</span>
      </button>
      <button class="action-btn primary" id="gen-btn" onclick="generateVideo()" title="Generate video">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        <span>Generate Video</span>
      </button>
    </div>
  </div>
</footer>

<div class="overlay" id="loading">
  <div class="spinner"></div>
  <div>Generating video&hellip;</div>
</div>

<div class="overlay" id="result-modal">
  <div class="modal" style="max-width:400px">
    <video id="result-video" controls autoplay playsinline style="width:100%;max-height:70vh;border-radius:8px;background:#000;margin-bottom:12px"></video>
    <p id="result-info" style="font-size:12px"></p>
    <button onclick="openResult()">Open in Finder</button>
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

<!-- Per-scene editor popover (anchored next to the clicked block) -->
<div class="scene-editor" id="scene-editor"></div>

<div class="overlay" id="crop-modal">
  <div class="modal" id="crop-modal-inner" style="max-width:560px">
    <h2 style="color:#2196f3">Crop Scene</h2>
    <div class="crop-mode-tabs">
      <button type="button" data-mode="strip" class="active" onclick="setCropMode('strip')">Standard</button>
      <button type="button" data-mode="free" onclick="setCropMode('free')">Free</button>
    </div>

    <!-- Standard (legacy single-strip) mode pane -->
    <div id="crop-mode-strip" class="crop-mode-pane">
      <p id="crop-info" style="font-size:12px;color:#aaa;margin-bottom:12px">
        Drag the 9:16 frame to pick the cropped region. The selection fills the full output frame.
      </p>
      <div id="crop-stage" class="crop-stage">
        <div id="crop-thumb-wrap" class="crop-thumb-wrap">
          <img id="crop-thumb" alt="">
          <div id="crop-mask-l" class="crop-mask"></div>
          <div id="crop-mask-r" class="crop-mask"></div>
          <div id="crop-frame" class="crop-frame"></div>
        </div>
      </div>
    </div>

    <!-- Free mode pane: source view on the left, 9:16 preview on the
         right, with a toolbar above them. -->
    <div id="crop-mode-free" class="crop-mode-pane" style="display:none">
      <div class="cf-toolbar">
        <button type="button" class="cf-btn" onclick="cfAddRect()" title="Add rectangle">+</button>
        <button type="button" class="cf-btn" onclick="cfDeleteSelected()" title="Delete selected (Del)">×</button>
        <span class="cf-sep"></span>
        <button type="button" class="cf-btn" onclick="cfBumpZ(1)" title="Bring forward">↑</button>
        <button type="button" class="cf-btn" onclick="cfBumpZ(-1)" title="Send back">↓</button>
        <span class="cf-sep"></span>
        <button type="button" class="cf-btn" onclick="cfOpenPresetLoad()">Load…</button>
        <button type="button" class="cf-btn" onclick="cfSavePreset()">Save preset…</button>
        <span class="cf-status" id="cf-status"></span>
      </div>
      <div class="cf-stage">
        <div class="cf-pane">
          <div class="cf-pane-label">Source</div>
          <div class="cf-src-wrap" id="cf-src-wrap">
            <img id="cf-src-img" alt="">
            <div class="cf-rects" id="cf-src-rects"></div>
          </div>
        </div>
        <div class="cf-pane">
          <div class="cf-pane-label">Output (9:16)</div>
          <div class="cf-dst-wrap" id="cf-dst-wrap">
            <div class="cf-rects" id="cf-dst-rects"></div>
          </div>
        </div>
      </div>
    </div>

    <div style="margin-top:14px;display:flex;gap:8px;justify-content:center;align-items:center">
      <button onclick="clearCrop()">Remove Crop</button>
      <button onclick="closeCropModal()">Cancel</button>
      <button onclick="saveCrop()" class="primary" style="background:#2196f3;border-color:#2196f3;color:#fff">Save</button>
    </div>
  </div>
</div>

<!-- Crop-preset picker (Load…) -->
<div class="overlay" id="cf-preset-modal">
  <div class="modal" style="max-width:560px;max-height:80vh;display:flex;flex-direction:column">
    <h2 style="color:#2196f3;margin-top:0">Load Crop Preset</h2>
    <p style="color:#888;font-size:11px;margin:0 0 10px 0">Double-click a preset to apply it to this clip.</p>
    <div id="cf-preset-list" style="flex:1;overflow-y:auto;margin:0 -4px"></div>
    <div style="margin-top:12px;text-align:right">
      <button onclick="cfClosePresetLoad()">Close</button>
    </div>
  </div>
</div>

<div class="overlay" id="trans-picker-modal">
  <div class="modal" style="max-width:480px">
    <h2 style="color:#e53935">Change Transition</h2>
    <div id="trans-picker-grid"></div>
    <div style="margin-top:16px">
      <button onclick="removeTransition()">No Transition</button>
      <button onclick="closeTransPicker()">Cancel</button>
    </div>
  </div>
</div>

<div class="overlay" id="music-picker-modal">
  <div class="modal" style="max-width:400px">
    <h2 style="color:#4caf50">Add Music</h2>
    <div id="music-picker-list"></div>
    <div style="margin-top:16px">
      <button onclick="closeMusicPicker()">Cancel</button>
    </div>
  </div>
</div>

<div class="overlay" id="text-editor-modal">
  <div class="te-container">
    <div class="te-toolbar">
      <button class="te-btn te-add-btn" onclick="teAddBox()" title="Add text box">+</button>
      <div class="te-sep"></div>
      <select id="te-font" class="te-font-select" onchange="teFontChanged()"></select>
      <div class="te-sep"></div>
      <label class="te-label">Text</label>
      <input type="color" id="te-fontcolor" value="#ffffff"/>
      <div class="te-sep"></div>
      <button id="te-bold-btn" class="te-btn" onclick="teToggleBold()"><b>B</b></button>
      <button id="te-italic-btn" class="te-btn" onclick="teToggleItalic()"><i>I</i></button>
      <div class="te-sep"></div>
      <label class="te-label">BG</label>
      <input type="color" id="te-bgcolor" value="#000000"/>
      <button id="te-bg-none-btn" class="te-btn" onclick="teToggleBg()" title="Toggle background">&#8416;</button>
      <div class="te-sep"></div>
      <label class="te-label tl-check" style="display:flex;align-items:center;gap:6px;cursor:pointer"
             title="Show the frame from the video at this overlay's start time as the canvas background, so you can see where the text will sit on screen">
        <input type="checkbox" id="te-show-bg" onchange="teToggleVideoBg()">
        <span>Display Video Background</span>
      </label>
    </div>
    <div class="te-canvas-wrap">
      <div id="te-canvas"></div>
    </div>
    <div class="te-footer">
      <button class="te-gallery" onclick="openTextGallery()" title="Open saved overlays gallery">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M22 16V4c0-1.1-.9-2-2-2H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0-2-.9 2-2zM11 12l2.03 2.71L16 11l4 5H8l3-4zM2 6v14c0 1.1.9 2 2 2h14v-2H4V6H2z"/></svg>
        <span>Gallery</span>
      </button>
      <div class="te-footer-spacer"></div>
      <button class="te-preview" onclick="openTextPreview()" title="Play the underlying clip with this overlay on top">
        <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="8,5 19,12 8,19"/></svg>
        <span>Preview</span>
      </button>
      <button class="te-cancel" onclick="closeTextEditor()">Cancel</button>
      <button class="te-apply" onclick="applyTextEditor()">Apply</button>
      <button class="te-save" onclick="saveTextOverlay()" title="Save to gallery">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></svg>
        <span>Save</span>
      </button>
    </div>
  </div>
</div>

<!-- Text overlay preview modal — plays the underlying clip with the
     in-editor text boxes overlaid in place so the user can see how the
     overlay sits on top of the actual scene. -->
<div class="overlay" id="text-preview-modal">
  <div class="tp-container">
    <div class="tp-stage" id="tp-stage">
      <video id="tp-video" playsinline muted></video>
      <div id="tp-overlays" class="tp-overlays"></div>
    </div>
    <div class="tp-foot">
      <span class="tp-info" id="tp-info"></span>
      <div class="tp-foot-spacer"></div>
      <button class="te-cancel" onclick="closeTextPreview()">Close</button>
    </div>
  </div>
</div>

<!-- Text overlay gallery modal -->
<div class="overlay" id="text-gallery-modal">
  <div class="modal" style="max-width:760px;width:90vw">
    <h2 style="color:#e53935;margin-top:0">Text Overlay Gallery</h2>
    <p style="color:#888;font-size:12px;margin:0 0 14px 0">
      Click a preset to load its style into the editor. Use the trash icon to remove one.
    </p>
    <div id="tg-grid" class="tg-grid"></div>
    <div id="tg-empty" class="tg-empty" style="display:none">
      No saved overlays yet. Configure one in the editor and click Save.
    </div>
    <div style="margin-top:14px;text-align:right">
      <button class="te-cancel" onclick="closeTextGallery()">Close</button>
    </div>
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

var CELL_W = 60; /* px per second */
var allClips = [];
var musicList = [];
var musicColorMap = {};
var gridSort = null;
var resultPath = '';

/* Track data */
var videoItems = [];    /* [{clip, transition (optional before)}] */
var soundItems = [];    /* [{id, name, volume, startTime, duration, el}] */
var textItems = [];     /* [{id, text, startTime, endTime, position, fontsize, el}] */
var selectedTransition = 'fade';
var nextBlkId = 0;
var tlTrackCount = 1;
var tlSequential = [true, true, true]; /* per-track: true = sequential, false = free-form */

/* Layer settings — persisted in timeline JSON.
 * default_position drives where wide clips on this layer render in the
 * 1080x1920 frame: top slot, center slot, bottom slot. Layer 1 → top,
 * 2 → center, 3 → bottom. muted = mute the entire layer's audio.
 */
var trackSettings = [
  {muted:false, default_position:'top',    captions:'none', default_crop_x_frac:null},
  {muted:false, default_position:'center', captions:'none', default_crop_x_frac:null},
  {muted:false, default_position:'bottom', captions:'none', default_crop_x_frac:null},
];

// Captions cycle order. Click the layer icon to advance:
//   none → bottom → middle → top → none → ...
// Same cycle is reused per-clip with an extra 'inherit' state at the start.
var CAPTIONS_LAYER_CYCLE = ['none','bottom','middle','top'];
var CAPTIONS_CLIP_CYCLE  = ['inherit','bottom','middle','top','none'];

function toggleTrackMute(t) {
  trackSettings[t].muted = !trackSettings[t].muted;
  renderTrackHeaders();
  saveBuilderState();
}
function cycleTrackCaptions(t) {
  var cur = trackSettings[t].captions || 'none';
  var i = CAPTIONS_LAYER_CYCLE.indexOf(cur);
  trackSettings[t].captions = CAPTIONS_LAYER_CYCLE[(i + 1) % CAPTIONS_LAYER_CYCLE.length];
  renderTrackHeaders();
  renderVideoTrack(); /* per-clip "captions" badge depends on layer flag */
  saveBuilderState();
}
function setTrackPosition(t, pos) {
  trackSettings[t].default_position = pos;
  renderTrackHeaders();
  renderVideoTrack(); /* clip-level "uses default" badges may need redraw */
  saveBuilderState();
}

// Cycle wide-clip vertical position with one click instead of forcing
// the user to hit a tiny stripe. Cycles top → center → bottom → top.
var POS_CYCLE = ['top','center','bottom'];
function cycleTrackPosition(t) {
  var cur = trackSettings[t].default_position || 'top';
  var i = POS_CYCLE.indexOf(cur);
  setTrackPosition(t, POS_CYCLE[(i + 1) % POS_CYCLE.length]);
}

// ── Icon helpers (4-state captions, 3-state wide position) ───────────────
// Captions: a horizontal bar at top/middle/bottom shows the active state;
// the 'none' state is an outline-only icon with a slash.
function capIconHtml(state, size) {
  size = size || 12;
  var fill = 'currentColor';
  var bar = function(y){return '<rect x="3" y="'+y+'" width="14" height="3" rx="1" fill="'+fill+'"/>'};
  var dimBox = '<rect x="2" y="2" width="16" height="16" rx="2" stroke="'+fill+'" fill="none" stroke-width="1.4"/>';
  var box    = '<rect x="2" y="2" width="16" height="16" rx="2" stroke="'+fill+'" fill="none" stroke-width="1.4"/>';
  if (state === 'top')    return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 20 20">'+box+bar(5)+'</svg>';
  if (state === 'middle') return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 20 20">'+box+bar(8.5)+'</svg>';
  if (state === 'bottom') return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 20 20">'+box+bar(12)+'</svg>';
  // 'none' — outline-only with a strike-through to make off-state obvious.
  return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 20 20">'+dimBox
       + '<line x1="3" y1="17" x2="17" y2="3" stroke="'+fill+'" stroke-width="1.6"/></svg>';
}

// Wide-position icon: stack of three thin bars with the active one filled.
function posIconHtml(pos, size) {
  size = size || 12;
  var fill = 'currentColor';
  function bar(y, sel) {
    var op = sel ? '1' : '0.25';
    return '<rect x="3" y="'+y+'" width="14" height="3" rx="1" fill="'+fill+'" opacity="'+op+'"/>';
  }
  return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 20 20">'
    + bar(3,  pos === 'top')
    + bar(8.5, pos === 'center')
    + bar(14, pos === 'bottom')
    + '</svg>';
}
function renderTrackHeaders() {
  for (var t = 0; t < 3; t++) {
    (function(t){
    var hdr = document.getElementById('track-header-' + t);
    if (!hdr) return;
    var s = trackSettings[t];
    hdr.classList.toggle('muted', !!s.muted);
    var label = ['Video I','Video II','Video III'][t];
    var muteIcon = s.muted
      ? '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.796 8.796 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z"/></svg>'
      : '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>';
    var stripes = ['top','center','bottom'].map(function(p) {
      return '<div class="th-stripe' + (s.default_position === p ? ' sel' : '') + '" data-pos="' + p + '"></div>';
    }).join('');
    var seqOn = !!tlSequential[t];
    var capState = s.captions || 'none';
    var capIcon = capIconHtml(capState, 12);
    var capTitle = 'Captions: ' + capState
      + ' — click to cycle (none → bottom → middle → top)';
    var posIcon = posIconHtml(s.default_position, 12);
    var posTitle = 'Wide-clip position: ' + s.default_position
      + ' — click to cycle (top → center → bottom)';
    var hasCrop = (typeof s.default_crop_x_frac === 'number');
    var cropIcon = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" '
      + 'stroke="currentColor" stroke-width="2">'
      + '<path d="M6 2v14a2 2 0 0 0 2 2h14"/>'
      + '<path d="M2 6h14a2 2 0 0 1 2 2v14"/></svg>';
    var cropTitle = hasCrop
      ? ('Layer crop: ' + Math.round(s.default_crop_x_frac * 100) + '% — click to edit')
      : 'Layer crop: off — click to set default crop for wide clips';
    hdr.innerHTML =
      '<div class="th-name">' + label + '</div>'
      + '<div class="th-row">'
      +   '<button class="th-btn' + (s.muted ? ' active' : '') + '" '
      +           'title="Mute layer" data-act="mute">' + muteIcon + '</button>'
      +   '<button class="th-btn cap-btn cap-' + capState + '" '
      +           'title="' + capTitle + '" data-act="cap">' + capIcon + '</button>'
      +   '<button class="th-btn pos-btn" '
      +           'title="' + posTitle + '" data-act="pos">' + posIcon + '</button>'
      +   '<button class="th-btn crop-btn' + (hasCrop ? ' active' : '') + '" '
      +           'title="' + cropTitle + '" data-act="crop">' + cropIcon + '</button>'
      + '</div>'
      + '<div class="th-mode" title="Free: clips can overlap. Seq: clips snap end-to-end.">'
      +   '<span class="th-mode-lbl' + (!seqOn ? ' on' : '') + '">FREE</span>'
      +   '<div class="th-tog' + (seqOn ? ' active' : '') + '" id="tl-mode-toggle-' + t + '" data-act="mode">'
      +     '<div class="th-knob"></div>'
      +   '</div>'
      +   '<span class="th-mode-lbl' + (seqOn ? ' on' : '') + '">SEQ</span>'
      + '</div>';
    hdr.querySelector('[data-act="mute"]').onclick = function() { toggleTrackMute(t); };
    hdr.querySelector('[data-act="cap"]').onclick = function() { cycleTrackCaptions(t); };
    hdr.querySelector('[data-act="pos"]').onclick = function() { cycleTrackPosition(t); };
    hdr.querySelector('[data-act="crop"]').onclick = function() { openCropModal({trackIdx: t}); };
    hdr.querySelector('[data-act="mode"]').onclick = function(e) {
      e.stopPropagation();
      toggleTimelineMode(t);
      renderTrackHeaders(); /* refresh on/off label colors */
    };
    })(t);
  }
}

async function init() {
  var tags = await fetch('/api/tags').then(function(r){return r.json()});
  var sel = document.getElementById('tag-filter');
  for (var tag in tags) {
    var o = document.createElement('option');
    o.value = tag; o.textContent = tag + ' (' + tags[tag] + ')';
    sel.appendChild(o);
  }

  allClips = await fetch('/api/clips').then(function(r){return r.json()});
  bbInit();
  renderGrid();

  /* Load fonts for text editor */
  var fontData = await fetch('/api/fonts').then(function(r){return r.json()});
  var fontSel = document.getElementById('te-font');
  var defOpt = document.createElement('option');
  defOpt.value = ''; defOpt.textContent = 'Default';
  fontSel.appendChild(defOpt);
  for (var fi = 0; fi < fontData.length; fi++) {
    var fo = document.createElement('option');
    fo.value = fontData[fi].name; fo.textContent = fontData[fi].name;
    if (fontData[fi].file) fo.style.fontFamily = '"' + fontData[fi].name + '"';
    fontSel.appendChild(fo);
    /* Register custom fonts via CSS @font-face */
    if (fontData[fi].file) {
      var style = document.createElement('style');
      style.textContent = '@font-face { font-family: "' + fontData[fi].name + '"; src: url("/api/font/' + fontData[fi].file + '"); }';
      document.head.appendChild(style);
    }
  }

  musicList = await fetch('/api/music').then(function(r){return r.json()});
  musicColorMap['No Music'] = NO_MUSIC_COLOR;
  for (var i = 0; i < musicList.length; i++) {
    musicColorMap[musicList[i].name] = MUSIC_COLORS[i % MUSIC_COLORS.length];
  }
  setupTracks();
  renderTrackHeaders();

  /* Auto-load timeline from ?load=filename query param */
  var params = new URLSearchParams(window.location.search);
  var loadFile = params.get('load');
  if (loadFile) {
    /* Loading a generated video supersedes the autosaved state. */
    clearBuilderState();
    fetch('/api/load-video', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename: loadFile}),
    }).then(function(r){return r.json()}).then(function(data) {
      if (data.error) return;
      if (data.timeline && data.timeline.video_track) loadNewTimeline(data.timeline);
      else if (data.timeline) loadOldTimeline(data.timeline);
    });
    /* Clean up the URL */
    window.history.replaceState({}, '', '/builder');
  } else {
    /* No explicit load — bring back whatever the user had last time. */
    if (restoreBuilderState()) {
      renderTrackHeaders(); /* reflect restored layer mute/captions/crop */
    }
  }
  /* Restore is done — autosave on subsequent mutations is now safe. */
  _builderSaveEnabled = true;

  /* Append a scene to the end of Layer I via ?add_scene=<id> — handoff from
   * the Analyze page's transcript modal. Runs after restore so the new clip
   * lands at the end of the preserved timeline. */
  var addId = parseInt(params.get('add_scene'), 10);
  if (!isNaN(addId)) {
    var c = allClips.find(function(x){ return x.id === addId; });
    if (c) {
      addVideoItem(c, undefined, 0);
    }
    window.history.replaceState({}, '', '/builder');
  }

  /* Drain the pending-append queue written by the Analyze page's
   * "Add to Builder" button. Each id gets appended to the end of Layer I
   * (the current end — so we always extend the *current* timeline, never
   * a stale snapshot). Done after restore so order is: existing timeline
   * + new scenes in the order the user added them. */
  var PENDING_KEY = 'pg-builder-pending-scenes-v1';
  var _drainedAdded = 0, _drainedDup = 0, _drainedMissing = 0, _lastDrainedId = null;
  try {
    var praw = localStorage.getItem(PENDING_KEY);
    if (praw) {
      var pq = JSON.parse(praw) || [];
      localStorage.removeItem(PENDING_KEY);
      for (var pi = 0; pi < pq.length; pi++) {
        var pid = pq[pi];
        var pc = allClips.find(function(x){ return x.id === pid; });
        if (!pc) { _drainedMissing++; continue; }
        if (addVideoItem(pc, undefined, 0)) {
          _drainedAdded++;
          _lastDrainedId = pid;
        } else {
          _drainedDup++;
        }
      }
    }
  } catch (e) { /* ignore */ }

  /* Live handoff from the Analyze page. While this tab is open, the
   * analyzer broadcasts {type:'add_scene', scene_id} after creating a
   * scene; we refresh the clip cache, append to the current end of Layer
   * I, drop that id from the pending queue, scroll to the new end, and
   * show a toast. */
  try {
    var _bc = new BroadcastChannel('pg-builder');
    _bc.onmessage = async function(ev) {
      var m = ev.data || {};
      if (m.type !== 'add_scene' || typeof m.scene_id !== 'number') return;
      try {
        allClips = await fetch('/api/clips').then(function(r){return r.json()});
      } catch (e) { return; }
      var nc = allClips.find(function(x){ return x.id === m.scene_id; });
      // Always drop this id from the persisted queue — whether we add or
      // not — so a stale queue entry can't haunt the next cold load.
      try {
        var raw2 = localStorage.getItem(PENDING_KEY);
        if (raw2) {
          var q2 = JSON.parse(raw2) || [];
          q2 = q2.filter(function(x){ return x !== m.scene_id; });
          if (q2.length) localStorage.setItem(PENDING_KEY, JSON.stringify(q2));
          else localStorage.removeItem(PENDING_KEY);
        }
      } catch (e) { /* ignore */ }
      if (!nc) {
        _pgBuilderToast('Add to Builder: scene #' + m.scene_id + ' not found');
        return;
      }
      var added = addVideoItem(nc, undefined, 0);
      renderGrid();
      if (added) {
        scrollTimelineToEnd();
        _pgBuilderToast('Added scene #' + m.scene_id + ' to Layer I');
      } else {
        _pgBuilderToast('Scene #' + m.scene_id + ' is already on the timeline');
      }
    };
  } catch (e) { /* BroadcastChannel unsupported */ }

  /* Cold-load feedback: if the queue had pending scenes, surface what
   * happened — added vs duplicate vs missing. Silent drains were leaving
   * users with no idea why their click "didn't work". */
  if (_drainedAdded || _drainedDup || _drainedMissing) {
    var parts = [];
    if (_drainedAdded)   parts.push('added ' + _drainedAdded);
    if (_drainedDup)     parts.push(_drainedDup + ' already on timeline');
    if (_drainedMissing) parts.push(_drainedMissing + ' not found');
    _pgBuilderToast('Add to Builder: ' + parts.join(', '));
  }

  /* Hand-off scroll: when navigated from Analyze "Add to Builder" toast,
   * scroll the horizontal timeline to its end so the user lands on the
   * newly-appended clip. Runs after queue drain + restore. */
  if (params.get('scroll_end') === '1' || _drainedAdded) {
    scrollTimelineToEnd();
    if (params.get('scroll_end') === '1') {
      window.history.replaceState({}, '', '/builder');
    }
  }
}

function scrollTimelineToEnd() {
  var el = document.getElementById('multi-timeline');
  if (!el) return;
  /* Defer one frame so any just-added clip has been laid out. */
  requestAnimationFrame(function() {
    el.scrollLeft = el.scrollWidth;
  });
}

function _pgBuilderToast(msg) {
  var t = document.getElementById('pg-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'pg-toast';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);'
      + 'padding:10px 18px;border-radius:8px;font:600 13px system-ui,sans-serif;'
      + 'color:#fff;background:#2e7d32;z-index:99999;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.4);opacity:0;transition:opacity .2s ease;'
      + 'pointer-events:none;';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(_pgBuilderToast._h);
  _pgBuilderToast._h = setTimeout(function(){ t.style.opacity = '0'; }, 2800);
}

function renderMusicLabels() {
  var container = document.getElementById('music-labels');
  container.innerHTML = '';
  for (var i = 0; i < musicList.length; i++) {
    var m = musicList[i];
    var col = musicColorMap[m.name] || NO_MUSIC_COLOR;
    var pill = document.createElement('div');
    pill.className = 'music-pill';
    pill.style.background = col.bg;
    pill.style.color = col.fg;
    pill.innerHTML = '<span class="dot" style="background:' + col.dot + '"></span>' + m.name;
    pill.addEventListener('click', function(name) {
      return function() { addSoundBlock(name, 3, getVideoDuration(), Math.max(5, getVideoDuration())); };
    }(m.name));
    container.appendChild(pill);
  }
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
    pill.className = 'trans-pill' + (name === selectedTransition ? ' active' : '');
    pill.textContent = name;
    pill.addEventListener('click', function(n, el) {
      return function() {
        selectedTransition = n;
        container.querySelectorAll('.trans-pill').forEach(function(p){p.classList.remove('active')});
        el.classList.add('active');
      };
    }(name, pill));
    container.appendChild(pill);
  }
}

/* ─── Multi-track timeline ─── */

function getVideoDuration() {
  var maxEnd = 0;
  for (var i = 0; i < videoItems.length; i++) {
    var end = videoItems[i].startTime + videoItems[i].duration;
    if (end > maxEnd) maxEnd = end;
  }
  return maxEnd;
}

function renderRuler() {
  var ruler = document.getElementById('time-ruler');
  var dur = Math.max(getVideoDuration(), 10);
  var w = Math.ceil(dur) * CELL_W;
  ruler.innerHTML = '';
  ruler.style.width = w + 'px';
  for (var s = 0; s <= Math.ceil(dur); s++) {
    var tick = document.createElement('div');
    tick.className = 'ruler-tick' + (s % 5 === 0 ? ' major' : '');
    tick.style.left = (s * CELL_W) + 'px';
    ruler.appendChild(tick);
    if (s % 5 === 0) {
      var lbl = document.createElement('span');
      lbl.className = 'ruler-label';
      lbl.style.left = (s * CELL_W) + 'px';
      lbl.textContent = s + 's';
      ruler.appendChild(lbl);
    }
  }
}

function computeVideoRows() {
  /* Assign visual rows using greedy interval packing.
     Sort by stackOrder first so overlapping wides respect user's vertical order. */
  var sorted = videoItems.slice().sort(function(a, b) {
    return a.startTime - b.startTime || a.stackOrder - b.stackOrder;
  });
  var rowEnds = []; /* tracks end-time of last item in each row */
  for (var i = 0; i < sorted.length; i++) {
    var vi = sorted[i];
    var s = vi.startTime, e = s + vi.duration;
    var placed = false;
    for (var r = 0; r < rowEnds.length; r++) {
      if (rowEnds[r] <= s) {
        rowEnds[r] = e;
        vi._row = r;
        placed = true;
        break;
      }
    }
    if (!placed) {
      vi._row = rowEnds.length;
      rowEnds.push(e);
    }
  }
  return Math.max(1, rowEnds.length);
}

function renderVideoTrack() {
  var dur = Math.max(getVideoDuration(), 10);

  for (var t = 0; t < 3; t++) {
    var track = document.getElementById('video-track-' + t);
    track.querySelectorAll('.vblock,.trans-marker').forEach(function(e){e.remove()});
    track.style.width = (dur * CELL_W) + 'px';

    var trackItems = [];
    for (var i = 0; i < videoItems.length; i++) {
      if ((videoItems[i].track || 0) === t) trackItems.push(i);
    }

    /* Compute rows for this track */
    var rowEnds = [];
    trackItems.sort(function(a, b) { return videoItems[a].startTime - videoItems[b].startTime; });
    for (var ti = 0; ti < trackItems.length; ti++) {
      var vi = videoItems[trackItems[ti]];
      var s = vi.startTime, e = s + vi.duration;
      var placed = false;
      for (var r = 0; r < rowEnds.length; r++) {
        if (rowEnds[r] <= s) { rowEnds[r] = e; vi._row = r; placed = true; break; }
      }
      if (!placed) { vi._row = rowEnds.length; rowEnds.push(e); }
    }
    var numRows = Math.max(1, rowEnds.length);
    var baseH = 70;
    track.style.minHeight = (numRows > 1 ? numRows * 50 : baseH) + 'px';

    for (var ti = 0; ti < trackItems.length; ti++) {
      var idx = trackItems[ti];
      var vi = videoItems[idx];
      var blk = document.createElement('div');
      blk.className = 'track-block vblock' + (vi.clip.wide ? ' vblock-is-wide' : '');
      blk.style.left = (vi.startTime * CELL_W) + 'px';
      blk.style.width = (vi.duration * CELL_W) + 'px';
      if (numRows > 1) {
        var rowH = 100 / numRows;
        blk.style.top = (vi._row * rowH) + '%';
        blk.style.height = rowH + '%';
      }
      var thumbUrl = vi.clip.id !== undefined ? '/api/thumbnail/' + vi.clip.id : '';
      var muted = !!vi.muted;
      var effPos = vi.position || trackSettings[t].default_position;

      /* Status icons (read-only — click anywhere on the block to edit) */
      var icons = '';
      // Mute indicator
      if (muted) {
        icons += '<span class="ic ic-mute" title="Muted">'
          + '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z"/></svg>'
          + '</span>';
      }
      // Slot indicator for wides
      if (vi.clip.wide) {
        var posStripes = ['top','center','bottom'].map(function(p) {
          return '<div class="ic-pos-stripe' + (effPos === p ? ' sel' : '') + '"></div>';
        }).join('');
        icons += '<span class="ic ic-pos" title="Slot: ' + effPos
          + (vi.position ? '' : ' (layer default)') + '">' + posStripes + '</span>';
      }
      // Transition pills (read-only icons; click on block opens editor)
      var transIn = vi.trans_in || '';
      var transOut = vi.trans_out || '';
      var transBadges = '';
      if (transIn) {
        transBadges += '<span class="ic ic-trans" title="In: ' + transIn + '">'
          + '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 12l8-8v5h8v6h-8v5z"/></svg>'
          + '</span>';
      }
      if (transOut) {
        transBadges += '<span class="ic ic-trans" title="Out: ' + transOut + '">'
          + '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20 12l-8-8v5H4v6h8v5z"/></svg>'
          + '</span>';
      }

      blk.innerHTML = (thumbUrl ? '<img class="vblock-thumb" src="' + thumbUrl + '"/>' : '')
        + '<span class="blk-label">' + vi.duration.toFixed(1) + 's</span>'
        + (vi.clip.wide ? '<span class="vblock-wide"></span>' : '')
        + '<div class="vblk-icons">' + icons + transBadges + '</div>';
      blk.dataset.idx = idx;
      vi._el = blk;
      makeVideoDraggable(blk, vi, idx);
      track.appendChild(blk);
    }

    /* update empty label */
    var emp = track.querySelector('.track-empty');
    if (trackItems.length && emp) emp.style.display = 'none';
    else if (!trackItems.length && emp) emp.style.display = '';
  }
}

function renderSoundTrack() {
  var track = document.getElementById('sound-track');
  track.querySelectorAll('.sblock').forEach(function(e){e.remove()});
  var dur = Math.max(getVideoDuration(), 10);
  track.style.width = (dur * CELL_W) + 'px';
  /* stack overlapping blocks */
  soundItems.sort(function(a,b){return a.startTime - b.startTime});
  var rows = [];
  for (var i = 0; i < soundItems.length; i++) {
    var si = soundItems[i];
    var placed = false;
    for (var r = 0; r < rows.length; r++) {
      if (rows[r] <= si.startTime) { rows[r] = si.startTime + si.duration; si._row = r; placed = true; break; }
    }
    if (!placed) { si._row = rows.length; rows.push(si.startTime + si.duration); }
    var col = musicColorMap[si.name] || NO_MUSIC_COLOR;
    var blk = document.createElement('div');
    blk.className = 'track-block sblock';
    blk.style.left = (si.startTime * CELL_W) + 'px';
    blk.style.width = (si.duration * CELL_W) + 'px';
    blk.style.background = col.bg;
    blk.style.color = col.fg;
    blk.style.borderColor = col.dot;
    var rowH = rows.length > 1 ? (100 / rows.length) : 100;
    blk.style.top = (si._row * rowH) + '%';
    blk.style.height = rowH + '%';
    var volBars = '';
    var heights = [5,8,11,14,18];
    for (var v = 0; v < 5; v++) {
      volBars += '<div class="vb' + (v < si.volume ? ' active' : '') + '" data-lv="' + (v+1) + '"'
        + ' style="height:' + heights[v] + 'px;background:' + col.dot + '"'
        + ' onclick="event.stopPropagation();setSoundVol(this)"></div>';
    }
    blk.innerHTML = '<span class="blk-label">' + si.name + '</span>'
      + '<div class="blk-vol">' + volBars + '</div>'
      + '<button class="blk-rm" onclick="event.stopPropagation();removeSoundItem(' + si.id + ')">&times;</button>'
      + '<div class="resize-handle"></div>';
    blk.dataset.blkId = si.id;
    si.el = blk;
    makeDraggable(blk, si, 'sound');
    track.appendChild(blk);
  }
  var emp = track.querySelector('.track-empty');
  if (soundItems.length && emp) emp.style.display = 'none';
  else if (!soundItems.length && emp) emp.style.display = '';
}

function renderTextTrack() {
  var track = document.getElementById('text-track');
  track.querySelectorAll('.tblock').forEach(function(e){e.remove()});
  var dur = Math.max(getVideoDuration(), 10);
  track.style.width = (dur * CELL_W) + 'px';
  textItems.sort(function(a,b){return a.startTime - b.startTime});
  var rows = [];
  for (var i = 0; i < textItems.length; i++) {
    var ti = textItems[i];
    var placed = false;
    for (var r = 0; r < rows.length; r++) {
      if (rows[r] <= ti.startTime) { rows[r] = ti.endTime; ti._row = r; placed = true; break; }
    }
    if (!placed) { ti._row = rows.length; rows.push(ti.endTime); }
    var blk = document.createElement('div');
    blk.className = 'track-block tblock';
    blk.style.left = (ti.startTime * CELL_W) + 'px';
    blk.style.width = ((ti.endTime - ti.startTime) * CELL_W) + 'px';
    var rowH = rows.length > 1 ? (100 / rows.length) : 100;
    blk.style.top = (ti._row * rowH) + '%';
    blk.style.height = rowH + '%';
    var transIn = ti.trans_in || 'fade';
    var transOut = ti.trans_out || 'fade';
    blk.innerHTML = '<span class="blk-label">' + (ti.label || ti.text || '') + '</span>'
      + '<span class="tblock-trans tt-in" data-tid="' + ti.id + '" data-dir="in">' + transIn + '</span>'
      + '<span class="tblock-trans tt-out" data-tid="' + ti.id + '" data-dir="out">' + transOut + '</span>'
      + '<button class="blk-rm" onclick="event.stopPropagation();removeTextItem(' + ti.id + ')">&times;</button>'
      + '<div class="resize-handle"></div>';
    blk.dataset.blkId = ti.id;
    /* Single-click to edit (track drag vs click via movement threshold) */
    (function(item, block) {
      var downX, downY, wasDrag = false;
      block.addEventListener('mousedown', function(e) {
        downX = e.clientX; downY = e.clientY; wasDrag = false;
      });
      block.addEventListener('mousemove', function(e) {
        if (downX !== undefined && (Math.abs(e.clientX - downX) > 4 || Math.abs(e.clientY - downY) > 4)) wasDrag = true;
      });
      block.addEventListener('mouseup', function(e) {
        if (!wasDrag && !e.target.closest('.blk-rm') && !e.target.closest('.resize-handle') && !e.target.closest('.tblock-trans')) {
          e.stopPropagation();
          openTextEditor(item, item.startTime, item.endTime);
        }
        downX = undefined;
      });
    })(ti, blk);
    /* Transition pill clicks */
    blk.querySelectorAll('.tblock-trans').forEach(function(pill) {
      pill.addEventListener('click', function(e) {
        e.stopPropagation();
        openTextTransPicker(parseInt(this.dataset.tid), this.dataset.dir);
      });
    });
    ti.el = blk;
    makeDraggable(blk, ti, 'text');
    track.appendChild(blk);
  }
  var emp = track.querySelector('.track-empty');
  if (textItems.length && emp) emp.style.display = 'none';
  else if (!textItems.length && emp) emp.style.display = '';
}

/* ─── Video drag and overlap resolution ─── */
function _trackUnderPoint(x, y) {
  var els = document.elementsFromPoint(x, y) || [];
  for (var i = 0; i < els.length; i++) {
    var vt = els[i].closest && els[i].closest('.video-track');
    if (vt && vt.id && vt.id.indexOf('video-track-') === 0) {
      return parseInt(vt.id.slice('video-track-'.length), 10);
    }
  }
  return null;
}

function _clearDropHighlight() {
  document.querySelectorAll('.video-track.drop-target').forEach(function(vt) {
    vt.classList.remove('drop-target');
  });
}

function makeVideoDraggable(el, data, idx) {
  var startX, startLeft, startY, startTopPx, moved;
  el.addEventListener('mousedown', function(e) {
    if (e.target.classList.contains('blk-rm') || e.target.classList.contains('vv')) return;
    e.preventDefault();
    startX = e.clientX;
    startY = e.clientY;
    startLeft = parseFloat(el.style.left);
    startTopPx = el.offsetTop;
    moved = false;
    function onMove(e2) {
      if (Math.abs(e2.clientX - startX) > 3 || Math.abs(e2.clientY - startY) > 3) moved = true;
      if (!moved) return;
      var dx = e2.clientX - startX;
      el.style.left = Math.max(0, startLeft + dx) + 'px';
      /* Highlight the layer the cursor is currently over (if different) */
      var hover = _trackUnderPoint(e2.clientX, e2.clientY);
      _clearDropHighlight();
      if (hover !== null && hover < tlTrackCount && hover !== (data.track || 0)) {
        var tgt = document.getElementById('video-track-' + hover);
        if (tgt) tgt.classList.add('drop-target');
      }
      /* Vertical drag for wide videos to reorder stack */
      if (data.clip.wide) {
        el.style.top = (startTopPx + (e2.clientY - startY)) + 'px';
      }
    }
    function onUp(e2) {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      _clearDropHighlight();
      if (!moved) {
        // Treat as click → open scene editor anchored to this block
        openSceneEditor(data, el);
        return;
      }
      var newStart = Math.max(0, parseFloat(el.style.left) / CELL_W);
      /* Snap to 0.5s grid */
      newStart = Math.round(newStart * 2) / 2;

      /* Detect target layer from drop position */
      var oldTrack = data.track || 0;
      var newTrack = _trackUnderPoint(e2.clientX, e2.clientY);
      if (newTrack === null || newTrack >= tlTrackCount) newTrack = oldTrack;
      var movedTrack = (newTrack !== oldTrack);
      if (movedTrack) data.track = newTrack;

      var vidIdx = videoItems.indexOf(data);
      var t = data.track || 0;
      data.startTime = newStart;

      if (tlSequential[t]) {
        packTimeline(t);
      } else {
        if (data.clip.wide && vidIdx >= 0 && !movedTrack) {
          reorderWideStack(vidIdx, e2.clientY - startY);
        }
        if (vidIdx >= 0) resolveVideoOverlaps(vidIdx);
      }
      /* If we moved out of the old track, clean it up too */
      if (movedTrack && tlSequential[oldTrack]) packTimeline(oldTrack);

      syncTl();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function reorderWideStack(movedIdx, dy) {
  /* Find the overlap group this wide video belongs to */
  var moved = videoItems[movedIdx];
  var mStart = moved.startTime, mEnd = mStart + moved.duration;
  var group = [movedIdx];
  for (var i = 0; i < videoItems.length; i++) {
    if (i === movedIdx || !videoItems[i].clip.wide) continue;
    var s = videoItems[i].startTime, e = s + videoItems[i].duration;
    if (s < mEnd && e > mStart) group.push(i);
  }
  if (group.length < 2) return;
  /* Sort by stackOrder */
  group.sort(function(a, b) { return videoItems[a].stackOrder - videoItems[b].stackOrder; });
  var oldPos = group.indexOf(movedIdx);
  /* Determine new position from dy: each row ~ 50px */
  var shift = Math.round(dy / 40);
  var newPos = Math.max(0, Math.min(group.length - 1, oldPos + shift));
  if (newPos === oldPos) return;
  /* Remove from old position and insert at new */
  group.splice(oldPos, 1);
  group.splice(newPos, 0, movedIdx);
  /* Reassign stackOrder */
  for (var k = 0; k < group.length; k++) {
    videoItems[group[k]].stackOrder = k;
  }
}

function resolveVideoOverlaps(droppedIdx) {
  var dropped = videoItems[droppedIdx];
  var t = dropped.track || 0;
  var sameTrack = videoItems.filter(function(v) { return (v.track || 0) === t; });

  var dropStart = dropped.startTime;
  var dropEnd = dropStart + dropped.duration;

  /* Find the leftmost video on this track that overlaps with the dropped */
  var overlapped = null;
  for (var i = 0; i < sameTrack.length; i++) {
    var vi = sameTrack[i];
    if (vi === dropped) continue;
    var viEnd = vi.startTime + vi.duration;
    if (vi.startTime < dropEnd && viEnd > dropStart) {
      if (!overlapped || vi.startTime < overlapped.startTime) {
        overlapped = vi;
      }
    }
  }

  if (overlapped) {
    var toPush = [];
    for (var i = 0; i < sameTrack.length; i++) {
      if (sameTrack[i] === dropped) continue;
      if (sameTrack[i].startTime <= overlapped.startTime) {
        toPush.push(sameTrack[i]);
      }
    }
    toPush.sort(function(a, b) { return a.startTime - b.startTime; });
    var cursor = dropEnd;
    for (var j = 0; j < toPush.length; j++) {
      toPush[j].startTime = cursor;
      cursor += toPush[j].duration;
    }
  }

  /* Sweep overlaps within this track */
  sweepTrackOverlaps(t);
}

function sweepTrackOverlaps(trackIdx) {
  var items = videoItems.filter(function(v) { return (v.track || 0) === trackIdx; });
  items.sort(function(a, b) { return a.startTime - b.startTime; });

  for (var i = 0; i < items.length; i++) {
    var vi = items[i];
    var viEnd = vi.startTime + vi.duration;

    for (var j = 0; j < items.length; j++) {
      if (items[j] === vi) continue;
      var vs = items[j].startTime;
      var ve = vs + items[j].duration;
      if (vs < viEnd && ve > vi.startTime) {
        vi.startTime = Math.max(vi.startTime, ve);
        viEnd = vi.startTime + vi.duration;
      }
    }
  }
}

function setTrackCount(n) {
  tlTrackCount = n;
  document.querySelectorAll('.vt-group-btn').forEach(function(b, i) {
    b.classList.toggle('active', i < n);
  });
  for (var t = 1; t <= 2; t++) {
    var row = document.getElementById('vt-row-' + t);
    if (t < n) {
      row.classList.add('active');
    } else {
      row.classList.remove('active');
      /* Move items from hidden tracks to track 0 */
      videoItems.forEach(function(vi) { if (vi.track === t) vi.track = 0; });
    }
  }
  if (tlSequential[0]) packTimeline(0);
  syncTl();
}

function toggleTimelineMode(trackIdx) {
  tlSequential[trackIdx] = !tlSequential[trackIdx];
  var tog = document.getElementById('tl-mode-toggle-' + trackIdx);
  tog.classList.toggle('active', tlSequential[trackIdx]);
  if (tlSequential[trackIdx]) packTimeline(trackIdx);
  syncTl();
}

function packTimeline(trackIdx) {
  /* Pack clips on a given track sequentially with no gaps, preserving order */
  var items = videoItems.filter(function(v) { return (v.track || 0) === trackIdx; });
  items.sort(function(a, b) { return a.startTime - b.startTime; });
  var cursor = 0;
  for (var i = 0; i < items.length; i++) {
    items[i].startTime = cursor;
    cursor += items[i].duration;
  }
}

/* Drag and resize for sound/text blocks */
function makeDraggable(el, data, trackType) {
  var startX, startLeft, startW, resizing = false;
  var handle = el.querySelector('.resize-handle');
  handle.addEventListener('mousedown', function(e) {
    e.stopPropagation();
    resizing = true;
    startX = e.clientX;
    startW = parseFloat(el.style.width);
    function onMove(e2) {
      var dx = e2.clientX - startX;
      var newW = Math.max(CELL_W, Math.round((startW + dx) / CELL_W) * CELL_W);
      el.style.width = newW + 'px';
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      resizing = false;
      var newDur = Math.max(1, Math.round(parseFloat(el.style.width) / CELL_W));
      if (trackType === 'sound') { data.duration = newDur; }
      else { data.endTime = data.startTime + newDur; }
      syncTl();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
  el.addEventListener('mousedown', function(e) {
    if (resizing || e.target.classList.contains('blk-rm') || e.target.classList.contains('vb') || e.target.classList.contains('tblock-trans')) return;
    startX = e.clientX;
    startLeft = parseFloat(el.style.left);
    function onMove(e2) {
      var dx = e2.clientX - startX;
      var newLeft = Math.max(0, Math.round((startLeft + dx) / CELL_W) * CELL_W);
      el.style.left = newLeft + 'px';
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      var newStart = Math.max(0, Math.round(parseFloat(el.style.left) / CELL_W));
      if (trackType === 'sound') { data.startTime = newStart; }
      else {
        var dur = data.endTime - data.startTime;
        data.startTime = newStart;
        data.endTime = newStart + dur;
      }
      syncTl();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function setSoundVol(barEl) {
  var lv = parseInt(barEl.dataset.lv);
  var blk = barEl.closest('.sblock');
  var id = parseInt(blk.dataset.blkId);
  var item = soundItems.find(function(s){return s.id===id});
  if (item) item.volume = lv;
  blk.querySelectorAll('.vb').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.lv) <= lv);
  });
}

function setVideoVol(barEl) {
  var lv = parseInt(barEl.dataset.lv);
  var idx = parseInt(barEl.dataset.vidx);
  if (idx >= 0 && idx < videoItems.length) videoItems[idx].volume = lv;
  barEl.closest('.vblk-vol').querySelectorAll('.vv').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.lv) <= lv);
  });
}

function toggleClipMute(idx) {
  if (idx < 0 || idx >= videoItems.length) return;
  videoItems[idx].muted = !videoItems[idx].muted;
  renderVideoTrack();
}

function setClipCaptions(idx, val) {
  // Per-clip captions: 'inherit' / 'none' / 'bottom' / 'middle' / 'top'.
  // 'inherit' means the render pipeline reads the layer's setting.
  if (idx < 0 || idx >= videoItems.length) return;
  if (CAPTIONS_CLIP_CYCLE.indexOf(val) < 0) val = 'inherit';
  videoItems[idx].captions = val;
  renderVideoTrack();
}

function cycleClipCaptions(idx) {
  if (idx < 0 || idx >= videoItems.length) return;
  var cur = videoItems[idx].captions || 'inherit';
  // Tolerate the previous tri-state representation in saved data.
  if (cur === true)  cur = 'bottom';
  if (cur === false) cur = 'none';
  if (cur === null)  cur = 'inherit';
  var i = CAPTIONS_CLIP_CYCLE.indexOf(cur);
  setClipCaptions(idx, CAPTIONS_CLIP_CYCLE[(i + 1) % CAPTIONS_CLIP_CYCLE.length]);
}

function setClipPosition(idx, pos) {
  if (idx < 0 || idx >= videoItems.length) return;
  var vi = videoItems[idx];
  if (!vi.clip.wide) return;
  var trackDefault = trackSettings[vi.track || 0].default_position;
  // Toggle off the override when user picks the layer's default — keeps the
  // model "uses default" rather than "explicitly set to default".
  if (pos === trackDefault) {
    vi.position = null;
  } else {
    vi.position = pos;
  }
  renderVideoTrack();
}

/* ── Scene editor popover ────────────────────────────────────────────── */

var _seData = null;     /* the videoItem currently being edited */
var _seAnchor = null;   /* the block element it's anchored to */

function openSceneEditor(data, anchorEl) {
  _seData = data;
  _seAnchor = anchorEl;
  var ed = document.getElementById('scene-editor');
  ed.innerHTML = _seBuildHTML(data);
  ed.classList.add('active');
  _sePosition(ed, anchorEl);
  _seWireEvents(ed);
}

function closeSceneEditor() {
  _seData = null;
  _seAnchor = null;
  document.getElementById('scene-editor').classList.remove('active');
}

function _seBuildHTML(vi) {
  var t = vi.track || 0;
  var trackDefault = trackSettings[t].default_position;
  var effPos = vi.position || trackDefault;
  var muted = !!vi.muted;
  var transIn = vi.trans_in || '';
  var transOut = vi.trans_out || '';
  var name = (vi.clip && vi.clip.filename) ? vi.clip.filename : 'Scene';
  // Truncate long names
  if (name.length > 28) name = name.slice(0, 25) + '...';

  var html = ''
    + '<div class="se-title">'
    +   '<span>' + name + '</span>'
    +   '<button class="se-close" data-act="close" title="Close">&times;</button>'
    + '</div>';

  if (vi.clip && vi.clip.wide) {
    var posBtns = ['top','center','bottom'].map(function(p){
      var sel = (effPos === p);
      var isOverride = (vi.position && p === effPos);
      var cls = 'se-pos-btn' + (sel ? ' sel' : '')
              + (sel && !isOverride ? ' sel-default' : '');
      return '<button class="' + cls + '" data-act="pos" data-pos="' + p + '">'
           + p.charAt(0).toUpperCase() + p.slice(1) + '</button>';
    }).join('');
    html += ''
      + '<div class="se-row">'
      +   '<span class="se-label">Position</span>'
      +   '<div class="se-pos-group">' + posBtns + '</div>'
      + '</div>';

    // Crop row — per-clip override > layer default > none.
    var clipCrop = (typeof vi.crop_x_frac === 'number') ? vi.crop_x_frac : null;
    var layerCrop = trackSettings[t].default_crop_x_frac;
    var cropLabel, cropCls = 'se-crop-btn';
    if (clipCrop !== null) {
      cropLabel = 'Custom (' + Math.round(clipCrop * 100) + '%)';
      cropCls += ' has-crop';
    } else if (layerCrop !== null) {
      cropLabel = 'Layer (' + Math.round(layerCrop * 100) + '%)';
      cropCls += ' has-crop layer-default';
    } else {
      cropLabel = 'None';
    }
    html += ''
      + '<div class="se-row">'
      +   '<span class="se-label">Crop</span>'
      +   '<button class="' + cropCls + '" data-act="crop">'
      +     '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
      +       '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M2 6h14a2 2 0 0 1 2 2v14"/>'
      +     '</svg>'
      +     '<span>' + cropLabel + '</span>'
      +   '</button>'
      + '</div>';
  }

  var muteIcon = muted
    ? '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>';
  html += ''
    + '<div class="se-row">'
    +   '<span class="se-label">Sound</span>'
    +   '<button class="se-mute-btn' + (muted ? ' muted' : '') + '" data-act="mute">'
    +     muteIcon + '<span>' + (muted ? 'Muted' : 'On') + '</span>'
    +   '</button>'
    + '</div>'
    // Captions: per-clip 5-state cycle (inherit / none / bottom / middle / top).
    // Same single-click-cycles UX as the layer header button.
    + (function(){
        var raw = vi.captions;
        // Migrate legacy boolean/null shape.
        if (raw === true)  raw = 'bottom';
        else if (raw === false) raw = 'none';
        else if (raw === null || raw === undefined) raw = 'inherit';
        var capState = raw;
        var layerCap = trackSettings[t].captions || 'none';
        var label = (capState === 'inherit')
          ? 'Inherit (' + layerCap + ')'
          : capState.charAt(0).toUpperCase() + capState.slice(1);
        var iconState = (capState === 'inherit') ? layerCap : capState;
        var icon = capIconHtml(iconState, 14);
        return '<div class="se-row">'
          + '<span class="se-label">Captions</span>'
          + '<button class="se-cap-cycle cap-' + iconState + '" data-act="cap-cycle" '
          +   'title="Click to cycle: inherit → bottom → middle → top → none">'
          +   icon + '<span>' + label + '</span>'
          + '</button>'
          + '</div>';
      })()
    + '<div class="se-row">'
    +   '<span class="se-label">Trans In</span>'
    +   '<button class="se-trans" data-act="trans-in">'
    +     (transIn || '<span style="color:#666">None</span>')
    +     (transIn ? '<span class="se-trans-clear" data-act="clear-in">×</span>' : '')
    +   '</button>'
    + '</div>'
    + '<div class="se-row">'
    +   '<span class="se-label">Trans Out</span>'
    +   '<button class="se-trans" data-act="trans-out">'
    +     (transOut || '<span style="color:#666">None</span>')
    +     (transOut ? '<span class="se-trans-clear" data-act="clear-out">×</span>' : '')
    +   '</button>'
    + '</div>'
    + '<button class="se-delete" data-act="delete">Delete Scene</button>';
  return html;
}

function _sePosition(ed, anchorEl) {
  /* Place to the right of the block when there's room, else to the left. */
  var ar = anchorEl.getBoundingClientRect();
  ed.style.visibility = 'hidden';
  ed.style.left = '0px';
  ed.style.top = '0px';
  // Force layout to read width/height
  var er = ed.getBoundingClientRect();
  var pad = 8;
  var leftSpace = ar.left;
  var rightSpace = window.innerWidth - ar.right;
  var x;
  if (rightSpace >= er.width + pad + 10) {
    x = ar.right + pad;
  } else if (leftSpace >= er.width + pad + 10) {
    x = ar.left - er.width - pad;
  } else {
    x = Math.max(8, Math.min(window.innerWidth - er.width - 8, ar.left));
  }
  var y = ar.top;
  // Clamp to viewport
  y = Math.max(8, Math.min(window.innerHeight - er.height - 8, y));
  ed.style.left = x + 'px';
  ed.style.top = y + 'px';
  ed.style.visibility = 'visible';
}

function _seWireEvents(ed) {
  ed.querySelectorAll('[data-act]').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.stopPropagation();
      if (!_seData) return;
      var act = this.dataset.act;
      var idx = videoItems.indexOf(_seData);
      if (idx < 0 && act !== 'close') return;
      if (act === 'close') { closeSceneEditor(); }
      else if (act === 'pos') { setClipPosition(idx, this.dataset.pos); _seRefresh(); }
      else if (act === 'crop') { closeSceneEditor(); openCropModal({clipIdx: idx}); }
      else if (act === 'cap-cycle') { cycleClipCaptions(idx); _seRefresh(); }
      else if (act === 'mute') { toggleClipMute(idx); _seRefresh(); }
      else if (act === 'trans-in') { closeSceneEditor(); openVideoTransPicker(idx, 'in'); }
      else if (act === 'trans-out') { closeSceneEditor(); openVideoTransPicker(idx, 'out'); }
      else if (act === 'clear-in') { _seData.trans_in = null; renderVideoTrack(); _seRefresh(); }
      else if (act === 'clear-out') { _seData.trans_out = null; renderVideoTrack(); _seRefresh(); }
      else if (act === 'delete') {
        closeSceneEditor();
        removeVideoItem(idx);
      }
    });
  });
}

function _seRefresh() {
  if (!_seData || !_seAnchor) return;
  // Re-render the timeline so the icon row updates, then locate the new
  // block element for this clip and re-anchor.
  var ed = document.getElementById('scene-editor');
  ed.innerHTML = _seBuildHTML(_seData);
  // The render replaces blocks — find the fresh element by clip identity.
  var fresh = _seData._el;
  if (fresh && document.body.contains(fresh)) {
    _seAnchor = fresh;
  }
  _sePosition(ed, _seAnchor);
  _seWireEvents(ed);
}

/* Dismiss popover on outside click / escape */
document.addEventListener('mousedown', function(e) {
  var ed = document.getElementById('scene-editor');
  if (!ed || !ed.classList.contains('active')) return;
  if (ed.contains(e.target)) return;
  // Don't close if the click landed on a vblock (it'll re-open via mouseup).
  if (e.target.closest('.vblock')) return;
  closeSceneEditor();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeSceneEditor();
});

/* ── Crop modal ──────────────────────────────────────────────────────────
 * Lets the user pick a horizontal 9:16 crop window over a wide source. Used
 * both per-clip (target = videoItems[clipIdx]) and per-layer
 * (target = trackSettings[trackIdx].default_crop_x_frac).
 *
 * State:
 *   _cropTarget   — {clipIdx} | {trackIdx}
 *   _cropFrac     — float 0..1 (left edge of crop window as fraction of
 *                   available horizontal range). 0.5 = centered.
 *   _cropDrag     — drag bookkeeping
 *
 * The frame width is computed from the actual thumbnail aspect ratio so it
 * matches the source — height fills the thumbnail, width = thumbH * 9/16
 * scaled into thumbnail-display pixels.
 */
var _cropTarget = null;
var _cropFrac = 0.5;
var _cropDrag = null;

function openCropModal(target) {
  _cropTarget = target;
  var existing;
  var thumbUrl = null;
  var info = '';
  // Per-clip free-mode state. Reset on every open; rehydrate if the
  // target clip already has saved free_crops.
  _cfRects = [];
  _cfSelected = null;
  _cfThumbUrl = null;
  if (target.clipIdx !== undefined) {
    var vi = videoItems[target.clipIdx];
    if (!vi || !vi.clip) return;
    existing = (typeof vi.crop_x_frac === 'number') ? vi.crop_x_frac : null;
    if (existing === null) {
      // Seed from layer default so the user starts from where the layer is.
      var ld = trackSettings[vi.track || 0].default_crop_x_frac;
      _cropFrac = (typeof ld === 'number') ? ld : 0.5;
    } else {
      _cropFrac = existing;
    }
    if (vi.clip.id !== undefined) {
      thumbUrl = '/api/thumbnail/' + vi.clip.id;
      _cfThumbUrl = thumbUrl;
    }
    // Pre-load any existing free-mode crops on this clip.
    if (Array.isArray(vi.free_crops) && vi.free_crops.length) {
      _cfRects = vi.free_crops.map(function(r, i){
        return {
          id: ++_cfNextId,
          sx: r.src && r.src.x_frac || 0, sy: r.src && r.src.y_frac || 0,
          sw: r.src && r.src.w_frac || 0.3, sh: r.src && r.src.h_frac || 0.3,
          dx: r.dst && r.dst.x_frac || 0,  dy: r.dst && r.dst.y_frac || 0,
          dw: r.dst && r.dst.w_frac || 0.3, dh: r.dst && r.dst.h_frac || 0.3,
          z:  (typeof r.z === 'number') ? r.z : i,
          color: r.color || CF_COLORS[i % CF_COLORS.length],
        };
      });
    }
    info = (vi.clip.filename || 'Scene')
      + ' — drag the 9:16 frame to pick the cropped region.';
  } else if (target.trackIdx !== undefined) {
    var s = trackSettings[target.trackIdx];
    existing = (typeof s.default_crop_x_frac === 'number') ? s.default_crop_x_frac : null;
    _cropFrac = (existing !== null) ? existing : 0.5;
    info = 'Video ' + ['I','II','III'][target.trackIdx] + ' layer default'
         + ' — applies to all wide clips on this layer (unless overridden per-scene).';
    // Pick any wide clip in this layer for the preview thumb, if available.
    for (var i = 0; i < videoItems.length; i++) {
      var v = videoItems[i];
      if ((v.track||0) === target.trackIdx && v.clip && v.clip.wide && v.clip.id !== undefined) {
        thumbUrl = '/api/thumbnail/' + v.clip.id;
        break;
      }
    }
  } else { return; }

  document.getElementById('crop-info').textContent = info;
  var img = document.getElementById('crop-thumb');
  img.onload = function() { _cropLayout(); };
  img.onerror = function() {
    // Fallback: synthesize a 16:9 placeholder so the user can still set crop.
    img.removeAttribute('src');
    img.style.width = '480px';
    img.style.height = '270px';
    img.style.background = 'linear-gradient(90deg,#222,#444,#222)';
    _cropLayout();
  };
  img.style.background = '';
  img.style.height = '';
  if (thumbUrl) img.src = thumbUrl;
  else img.onerror();

  document.getElementById('crop-modal').classList.add('active');
  _cropWireDrag();
  // Layout once even if image is cached.
  setTimeout(_cropLayout, 0);
  // Initialize free-mode pane (image + any existing rects). Default
  // mode is Standard; switch to Free if this clip already has free crops.
  _cfWire();
  _cfLoadImage(thumbUrl);
  setCropMode((_cfRects.length && target.clipIdx !== undefined) ? 'free' : 'strip');
}

function _cropLayout() {
  var img = document.getElementById('crop-thumb');
  var wrap = document.getElementById('crop-thumb-wrap');
  var frame = document.getElementById('crop-frame');
  var ml = document.getElementById('crop-mask-l');
  var mr = document.getElementById('crop-mask-r');
  if (!img || !frame) return;
  var W = img.clientWidth || img.naturalWidth || 480;
  var H = img.clientHeight || img.naturalHeight || 270;
  wrap.style.width = W + 'px';
  // Frame width = H * 9/16, height = H. Constrained to image width.
  var fw = Math.min(W, H * 9 / 16);
  var maxX = Math.max(0, W - fw);
  var x = maxX * Math.max(0, Math.min(1, _cropFrac));
  frame.style.width = fw + 'px';
  frame.style.left = x + 'px';
  ml.style.left = '0px';
  ml.style.width = x + 'px';
  mr.style.left = (x + fw) + 'px';
  mr.style.width = (W - x - fw) + 'px';
}

function _cropWireDrag() {
  var frame = document.getElementById('crop-frame');
  var wrap = document.getElementById('crop-thumb-wrap');
  if (!frame || frame._wired) return;
  frame._wired = true;
  function onDown(e) {
    e.preventDefault();
    var rect = wrap.getBoundingClientRect();
    var img = document.getElementById('crop-thumb');
    var W = img.clientWidth || rect.width;
    var H = img.clientHeight || rect.height;
    var fw = Math.min(W, H * 9 / 16);
    var maxX = Math.max(0, W - fw);
    var clientX = (e.touches ? e.touches[0].clientX : e.clientX);
    var startFrameX = parseFloat(frame.style.left) || 0;
    var startMouseX = clientX - rect.left;
    _cropDrag = {startFrameX:startFrameX, startMouseX:startMouseX,
                 maxX:maxX, fw:fw, W:W, rect:rect};
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchmove', onMove, {passive:false});
    document.addEventListener('touchend', onUp);
  }
  function onMove(e) {
    if (!_cropDrag) return;
    e.preventDefault();
    var clientX = (e.touches ? e.touches[0].clientX : e.clientX);
    var rect = _cropDrag.rect;
    var mouseX = clientX - rect.left;
    var newX = _cropDrag.startFrameX + (mouseX - _cropDrag.startMouseX);
    newX = Math.max(0, Math.min(_cropDrag.maxX, newX));
    _cropFrac = _cropDrag.maxX > 0 ? (newX / _cropDrag.maxX) : 0.5;
    _cropLayout();
  }
  function onUp() {
    _cropDrag = null;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onUp);
  }
  frame.addEventListener('mousedown', onDown);
  frame.addEventListener('touchstart', onDown, {passive:false});
  // Click anywhere on the image to recenter the frame on that x.
  wrap.addEventListener('click', function(e) {
    if (e.target === frame) return;
    var img = document.getElementById('crop-thumb');
    var rect = wrap.getBoundingClientRect();
    var W = img.clientWidth || rect.width;
    var H = img.clientHeight || rect.height;
    var fw = Math.min(W, H * 9 / 16);
    var maxX = Math.max(0, W - fw);
    var mouseX = e.clientX - rect.left;
    var newX = Math.max(0, Math.min(maxX, mouseX - fw/2));
    _cropFrac = maxX > 0 ? (newX / maxX) : 0.5;
    _cropLayout();
  });
}

function saveCrop() {
  if (!_cropTarget) { closeCropModal(); return; }
  if (_cropMode === 'free' && _cropTarget.clipIdx !== undefined) {
    // Free-mode: write the multi-rectangle composite to this clip.
    var vi = videoItems[_cropTarget.clipIdx];
    if (vi) {
      if (_cfRects.length) {
        vi.free_crops = _cfRects.map(function(r){
          return {
            src: {x_frac:r.sx, y_frac:r.sy, w_frac:r.sw, h_frac:r.sh},
            dst: {x_frac:r.dx, y_frac:r.dy, w_frac:r.dw, h_frac:r.dh},
            z: r.z, color: r.color,
          };
        });
        // Free crops override the legacy single-strip crop.
        vi.crop_x_frac = null;
      } else {
        delete vi.free_crops;
      }
    }
    closeCropModal();
    renderVideoTrack();
    saveBuilderState();
    return;
  }
  var f = Math.max(0, Math.min(1, _cropFrac));
  if (_cropTarget.clipIdx !== undefined) {
    var vi = videoItems[_cropTarget.clipIdx];
    if (vi) { vi.crop_x_frac = f; delete vi.free_crops; }
  } else if (_cropTarget.trackIdx !== undefined) {
    trackSettings[_cropTarget.trackIdx].default_crop_x_frac = f;
    renderTrackHeaders();
  }
  closeCropModal();
  renderVideoTrack();
  saveBuilderState();
}

function clearCrop() {
  if (!_cropTarget) { closeCropModal(); return; }
  if (_cropTarget.clipIdx !== undefined) {
    var vi = videoItems[_cropTarget.clipIdx];
    if (vi) { vi.crop_x_frac = null; delete vi.free_crops; }
  } else if (_cropTarget.trackIdx !== undefined) {
    trackSettings[_cropTarget.trackIdx].default_crop_x_frac = null;
    renderTrackHeaders();
  }
  closeCropModal();
  renderVideoTrack();
  saveBuilderState();
}

function closeCropModal() {
  _cropTarget = null;
  _cropDrag = null;
  _cfDrag = null;
  document.getElementById('crop-modal').classList.remove('active');
}

window.addEventListener('resize', function() {
  var m = document.getElementById('crop-modal');
  if (m && m.classList.contains('active')) {
    _cropLayout();
    _cfLayout();
  }
});

/* ── Free-mode crop ───────────────────────────────────────────────────
 *
 * Two synchronized panes — Source (left, source aspect) and Output
 * (right, locked 9:16). The user adds N rectangles on the source; each
 * one mirrors to the right at locked aspect ratio. On render, every
 * rectangle becomes a crop+overlay step in the ffmpeg filter graph.
 */
var CF_COLORS = ['#e53935','#1976d2','#43a047','#fb8c00','#8e24aa','#00acc1'];
var _cropMode = 'strip';
var _cfRects = [];
var _cfSelected = null;
var _cfNextId = 0;
var _cfDrag = null;
var _cfSrcAspect = 16/9;
var _cfThumbUrl = null;

function setCropMode(mode) {
  _cropMode = mode;
  var stripBtn = document.querySelector('.crop-mode-tabs button[data-mode="strip"]');
  var freeBtn  = document.querySelector('.crop-mode-tabs button[data-mode="free"]');
  if (stripBtn) stripBtn.classList.toggle('active', mode === 'strip');
  if (freeBtn)  freeBtn.classList.toggle('active',  mode === 'free');
  document.getElementById('crop-mode-strip').style.display = mode === 'strip' ? '' : 'none';
  document.getElementById('crop-mode-free').style.display  = mode === 'free'  ? '' : 'none';
  // Widen the modal in free mode so two panes fit.
  var inner = document.getElementById('crop-modal-inner');
  if (inner) inner.style.maxWidth = (mode === 'free') ? '960px' : '560px';
  if (mode === 'free') setTimeout(function(){ _cfLayout(); _cfRender(); }, 0);
}

function _cfLoadImage(url) {
  var img = document.getElementById('cf-src-img');
  if (!img) return;
  img.onload = function() {
    if (img.naturalWidth && img.naturalHeight) {
      _cfSrcAspect = img.naturalWidth / img.naturalHeight;
      var wrap = document.getElementById('cf-src-wrap');
      if (wrap) wrap.style.aspectRatio = (img.naturalWidth + '/' + img.naturalHeight);
    }
    _cfLayout(); _cfRender();
  };
  img.onerror = function(){ _cfLayout(); _cfRender(); };
  if (url) img.src = url;
  else img.removeAttribute('src');
}

function _cfLayout() {
  // Heights + widths are driven entirely by CSS (height: min(60vh, 460px)
  // + aspect-ratio set on the wrap). Nothing to do here — kept as a hook
  // for the resize listener so future tweaks have a home.
}

function cfAddRect() {
  var idx = _cfRects.length;
  var color = CF_COLORS[idx % CF_COLORS.length];
  // Default: 40% wide, centered. Source aspect drives dst height.
  var sw = 0.4, sh = 0.4, sx = 0.3, sy = 0.3;
  // Lock the destination rectangle to the source rectangle's PIXEL
  // aspect ratio inside the 9:16 output canvas.
  //   srcRectAspect = (srcW*sw) / (srcH*sh) = _cfSrcAspect * (sw/sh)
  //   dstRectAspect = (W*dw)   / (H*dh)     = (dw/dh) * (9/16)
  //   match → dh = dw * (sh/sw) * (9/16) / _cfSrcAspect
  var dw = 0.5;
  var dh = dw * (sh / sw) * (9 / 16) / _cfSrcAspect;
  if (!isFinite(dh) || dh <= 0) dh = 0.5;
  dh = Math.min(0.9, dh);
  var r = {
    id: ++_cfNextId,
    sx: sx, sy: sy, sw: sw, sh: sh,
    dx: (1 - dw) / 2, dy: (1 - dh) / 2, dw: dw, dh: dh,
    z: _cfRects.length, color: color,
  };
  _cfRects.push(r);
  _cfSelected = r.id;
  _cfRender();
}

function cfDeleteSelected() {
  if (!_cfSelected) return;
  _cfRects = _cfRects.filter(function(r){ return r.id !== _cfSelected; });
  _cfSelected = null;
  _cfRender();
}

function cfBumpZ(dir) {
  if (!_cfSelected) return;
  var r = _cfRects.find(function(x){ return x.id === _cfSelected; });
  if (!r) return;
  r.z += dir;
  _cfRender();
}

function _cfRecomputeDstAspect(r) {
  // Lock dst rect's pixel aspect to the source rect's pixel aspect:
  //   dh = dw * (sh/sw) * (9/16) / _cfSrcAspect
  // (See cfAddRect for the derivation.)
  var dh = r.dw * (r.sh / r.sw) * (9 / 16) / _cfSrcAspect;
  if (!isFinite(dh) || dh <= 0) dh = r.dw;
  r.dh = Math.min(1.0, Math.max(0.02, dh));
  if (r.dy + r.dh > 1) r.dy = Math.max(0, 1 - r.dh);
}

function _cfRender() {
  var srcCt = document.getElementById('cf-src-rects');
  var dstCt = document.getElementById('cf-dst-rects');
  if (!srcCt || !dstCt) return;
  srcCt.innerHTML = '';
  dstCt.innerHTML = '';
  // Sort by z ascending so the last one rendered is on top in the DOM.
  var sorted = _cfRects.slice().sort(function(a,b){ return a.z - b.z; });
  for (var i = 0; i < sorted.length; i++) {
    var r = sorted[i];
    var sel = (r.id === _cfSelected) ? ' selected' : '';
    var idx = _cfRects.indexOf(r);

    // SOURCE rectangle (resizable + draggable). 4 handles.
    var sEl = document.createElement('div');
    sEl.className = 'cf-rect' + sel;
    sEl.style.cssText = 'border-color:' + r.color + ';'
      + 'left:' + (r.sx*100) + '%;top:' + (r.sy*100) + '%;'
      + 'width:' + (r.sw*100) + '%;height:' + (r.sh*100) + '%;';
    sEl.dataset.rid = r.id; sEl.dataset.pane = 'src';
    sEl.innerHTML =
      '<span class="cf-label">' + (idx+1) + '  z=' + r.z + '</span>' +
      '<div class="cf-handle h-nw" data-h="nw"></div>' +
      '<div class="cf-handle h-ne" data-h="ne"></div>' +
      '<div class="cf-handle h-sw" data-h="sw"></div>' +
      '<div class="cf-handle h-se" data-h="se"></div>';
    srcCt.appendChild(sEl);

    // DST rectangle (uniform-scale on SE corner + drag to move).
    var dEl = document.createElement('div');
    dEl.className = 'cf-rect' + sel;
    var bg = _cfThumbUrl
      ? ("background-image:url('" + _cfThumbUrl + "');"
         + "background-size:" + (100/r.sw) + "% " + (100/r.sh) + "%;"
         + "background-position:" + (-r.sx/(1-r.sw)*100) + "% " + (-r.sy/(1-r.sh)*100) + "%;")
      : '';
    dEl.style.cssText = 'border-color:' + r.color + ';'
      + 'left:' + (r.dx*100) + '%;top:' + (r.dy*100) + '%;'
      + 'width:' + (r.dw*100) + '%;height:' + (r.dh*100) + '%;' + bg;
    dEl.dataset.rid = r.id; dEl.dataset.pane = 'dst';
    dEl.innerHTML =
      '<span class="cf-label">' + (idx+1) + '</span>' +
      '<div class="cf-handle h-se" data-h="se"></div>';
    dstCt.appendChild(dEl);
  }
  // Status line: count of rects.
  var st = document.getElementById('cf-status');
  if (st) st.textContent = _cfRects.length
    ? (_cfRects.length + ' rectangle' + (_cfRects.length !== 1 ? 's' : '')
       + (_cfSelected ? ' (selected #' + (_cfRects.findIndex(function(x){return x.id===_cfSelected})+1) + ')' : ''))
    : 'No rectangles yet. Click + to add one.';
}

function _cfWire() {
  ['cf-src-wrap','cf-dst-wrap'].forEach(function(id){
    var wrap = document.getElementById(id);
    if (!wrap || wrap._cfWired) return;
    wrap._cfWired = true;
    wrap.addEventListener('mousedown', _cfOnMouseDown);
  });
  if (!window._cfGlobalWired) {
    window._cfGlobalWired = true;
    document.addEventListener('mousemove', _cfOnMouseMove);
    document.addEventListener('mouseup',   _cfOnMouseUp);
    document.addEventListener('keydown', function(e){
      var modal = document.getElementById('crop-modal');
      if (!modal || !modal.classList.contains('active') || _cropMode !== 'free') return;
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (_cfSelected) { cfDeleteSelected(); e.preventDefault(); }
      }
    });
  }
}

function _cfOnMouseDown(e) {
  var rectEl = e.target.closest('.cf-rect');
  var wrap = e.currentTarget;
  var pane = (wrap.id === 'cf-src-wrap') ? 'src' : 'dst';
  if (!rectEl) {
    // Click on empty area = deselect.
    _cfSelected = null;
    _cfRender();
    return;
  }
  e.preventDefault();
  var rid = parseInt(rectEl.dataset.rid, 10);
  _cfSelected = rid;
  var rc = _cfRects.find(function(x){ return x.id === rid; });
  if (!rc) return;
  var handle = e.target.closest('.cf-handle');
  var b = wrap.getBoundingClientRect();
  _cfDrag = {
    pane: pane, rid: rid,
    paneW: b.width, paneH: b.height,
    paneL: b.left, paneT: b.top,
    startX: e.clientX, startY: e.clientY,
    init: {sx:rc.sx, sy:rc.sy, sw:rc.sw, sh:rc.sh,
           dx:rc.dx, dy:rc.dy, dw:rc.dw, dh:rc.dh},
    handle: handle ? handle.dataset.h : null,
  };
  _cfRender();
}

function _cfOnMouseMove(e) {
  if (!_cfDrag) return;
  var rc = _cfRects.find(function(x){ return x.id === _cfDrag.rid; });
  if (!rc) return;
  var dxFrac = (e.clientX - _cfDrag.startX) / _cfDrag.paneW;
  var dyFrac = (e.clientY - _cfDrag.startY) / _cfDrag.paneH;
  var I = _cfDrag.init;
  if (_cfDrag.pane === 'src') {
    if (!_cfDrag.handle) {
      // Move
      rc.sx = Math.max(0, Math.min(1 - I.sw, I.sx + dxFrac));
      rc.sy = Math.max(0, Math.min(1 - I.sh, I.sy + dyFrac));
    } else {
      // Resize handles — free aspect on the source side.
      if (_cfDrag.handle.indexOf('e') >= 0) rc.sw = Math.max(0.02, Math.min(1 - I.sx, I.sw + dxFrac));
      if (_cfDrag.handle.indexOf('s') >= 0) rc.sh = Math.max(0.02, Math.min(1 - I.sy, I.sh + dyFrac));
      if (_cfDrag.handle.indexOf('w') >= 0) {
        var nsx = Math.max(0, Math.min(I.sx + I.sw - 0.02, I.sx + dxFrac));
        rc.sw = (I.sx + I.sw) - nsx; rc.sx = nsx;
      }
      if (_cfDrag.handle.indexOf('n') >= 0) {
        var nsy = Math.max(0, Math.min(I.sy + I.sh - 0.02, I.sy + dyFrac));
        rc.sh = (I.sy + I.sh) - nsy; rc.sy = nsy;
      }
      // Recompute dst aspect to stay locked to source.
      _cfRecomputeDstAspect(rc);
    }
  } else {
    // dst pane
    if (!_cfDrag.handle) {
      rc.dx = Math.max(0, Math.min(1 - I.dw, I.dx + dxFrac));
      rc.dy = Math.max(0, Math.min(1 - I.dh, I.dy + dyFrac));
    } else if (_cfDrag.handle === 'se') {
      // Uniform scale by SE corner — drive from dw, then re-lock dh.
      var newDw = Math.max(0.05, Math.min(1 - I.dx, I.dw + dxFrac));
      rc.dw = newDw;
      _cfRecomputeDstAspect(rc);
    }
  }
  _cfRender();
}

function _cfOnMouseUp() { _cfDrag = null; }

/* ── Crop presets (save/load) ────────────────────────────────────── */

async function cfSavePreset() {
  if (!_cfRects.length) { alert('Add at least one rectangle to save.'); return; }
  var name = (window.prompt('Preset name:') || '').trim();
  if (!name) return;
  var body = {
    name: name,
    rects: _cfRects.map(function(r){
      return {
        src: {x_frac:r.sx, y_frac:r.sy, w_frac:r.sw, h_frac:r.sh},
        dst: {x_frac:r.dx, y_frac:r.dy, w_frac:r.dw, h_frac:r.dh},
        z: r.z, color: r.color,
      };
    }),
  };
  var r = await fetch('/api/crop-presets', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { alert('Save failed.'); return; }
  document.getElementById('cf-status').textContent = 'Saved as "' + name + '"';
}

async function cfOpenPresetLoad() {
  var modal = document.getElementById('cf-preset-modal');
  var list = document.getElementById('cf-preset-list');
  list.innerHTML = '<div style="color:#777;padding:14px;font-size:12px">Loading…</div>';
  modal.classList.add('active');
  try {
    var items = await fetch('/api/crop-presets').then(function(r){return r.json()});
    if (!items.length) {
      list.innerHTML = '<div style="color:#777;padding:14px;font-size:12px">'
        + 'No presets saved yet. Build a layout and click "Save preset…".</div>';
      return;
    }
    list.innerHTML = '';
    for (var i = 0; i < items.length; i++) {
      var p = items[i];
      var row = document.createElement('div');
      row.className = 'cf-preset-row';
      row.dataset.pid = p.id;
      row.innerHTML = '<span class="cf-preset-name">' + (p.name || 'Untitled')
        + '</span><span class="cf-preset-meta">'
        + (p.rects ? p.rects.length : 0) + ' rect'
        + ((p.rects && p.rects.length !== 1) ? 's' : '')
        + '</span><button class="cf-preset-del" title="Delete"'
        + ' data-pid="' + p.id + '">&times;</button>';
      row.ondblclick = (function(pp){ return function(){ cfApplyPreset(pp); }; })(p);
      list.appendChild(row);
    }
    list.querySelectorAll('.cf-preset-del').forEach(function(b){
      b.addEventListener('click', async function(e){
        e.stopPropagation();
        if (!confirm('Delete this preset?')) return;
        await fetch('/api/crop-presets/' + encodeURIComponent(b.dataset.pid),
                   {method:'DELETE'});
        cfOpenPresetLoad();
      });
    });
  } catch (e) {
    list.innerHTML = '<div style="color:#ef5350;padding:14px;font-size:12px">'
      + 'Failed to load presets.</div>';
  }
}

function cfClosePresetLoad() {
  document.getElementById('cf-preset-modal').classList.remove('active');
}

function cfApplyPreset(p) {
  _cfRects = (p.rects || []).map(function(r, i){
    return {
      id: ++_cfNextId,
      sx: r.src && r.src.x_frac || 0, sy: r.src && r.src.y_frac || 0,
      sw: r.src && r.src.w_frac || 0.3, sh: r.src && r.src.h_frac || 0.3,
      dx: r.dst && r.dst.x_frac || 0,  dy: r.dst && r.dst.y_frac || 0,
      dw: r.dst && r.dst.w_frac || 0.3, dh: r.dst && r.dst.h_frac || 0.3,
      z:  (typeof r.z === 'number') ? r.z : i,
      color: r.color || CF_COLORS[i % CF_COLORS.length],
    };
  });
  _cfSelected = null;
  cfClosePresetLoad();
  setCropMode('free');
  _cfRender();
}

/* Last-chance save so transient state changes (toggles, drags, drops that
 * happen between syncTl calls) always reach localStorage. */
window.addEventListener('beforeunload', function() { saveBuilderState(); });

function addVideoItem(clip, startTime, trackIdx) {
  /* Prevent adding the same scene twice. Returns false so callers can tell
   * the difference between "appended" and "skipped (duplicate)". */
  if (clip.id !== undefined && getTlClipIds().indexOf(clip.id) >= 0) return false;
  var t = trackIdx !== undefined ? trackIdx : 0;
  var dur = clip.duration;
  var trackItems = videoItems.filter(function(v) { return (v.track || 0) === t; });
  var trackEnd = 0;
  trackItems.forEach(function(v) { trackEnd = Math.max(trackEnd, v.startTime + v.duration); });
  var st = startTime !== undefined ? startTime : trackEnd;
  videoItems.push({
    clip: {id: clip.id, video_file: clip.video_file, start: clip.start, end: clip.end, filename: clip.filename, wide: clip.wide},
    duration: dur,
    startTime: st,
    track: t,
    trans_in: selectedTransition,
    trans_out: null,
    volume: 5,
    stackOrder: 0,
    crop_x_frac: null,
  });
  if (tlSequential[t]) {
    packTimeline(t);
  } else {
    resolveVideoOverlaps(videoItems.length - 1);
  }
  syncTl();
  return true;
}

function removeVideoItem(idx) {
  var removed = videoItems[idx];
  var t = removed.track || 0;
  videoItems.splice(idx, 1);
  if (tlSequential[t]) {
    packTimeline(t);
  } else {
    var gap = removed.duration;
    var threshold = removed.startTime;
    for (var i = 0; i < videoItems.length; i++) {
      if ((videoItems[i].track || 0) === t && videoItems[i].startTime >= threshold + gap) {
        videoItems[i].startTime -= gap;
      }
    }
  }
  syncTl();
}

function addSoundBlock(name, volume, startTime, duration) {
  soundItems.push({
    id: ++nextBlkId, name: name, volume: volume,
    startTime: startTime || 0, duration: duration || 10, el: null,
  });
  syncTl();
}

function removeSoundItem(id) {
  soundItems = soundItems.filter(function(s){return s.id !== id});
  syncTl();
}

var TEXT_TRANSITIONS = ['none','fade','slide_up','slide_down','slide_left','slide_right'];

function addTextGroup(boxes, startTime, endTime) {
  var label = boxes.map(function(b){return b.text}).join(' / ');
  textItems.push({
    id: ++nextBlkId,
    label: label,
    boxes: boxes,
    trans_in: 'fade', trans_out: 'fade',
    startTime: startTime, endTime: endTime,
    el: null,
  });
  syncTl();
}

function updateTextGroup(id, boxes, startTime, endTime) {
  var item = textItems.find(function(t){return t.id===id});
  if (!item) return;
  item.boxes = boxes;
  item.label = boxes.map(function(b){return b.text}).join(' / ');
  item.startTime = startTime;
  item.endTime = endTime;
  syncTl();
}

function removeTextItem(id) {
  textItems = textItems.filter(function(t){return t.id !== id});
  syncTl();
}

var lastInsertX = 0;

function setupTracks() {
  /* Video tracks: accept clips from grid via SortableJS */
  for (var t = 0; t < 3; t++) {
    (function(trackIdx) {
      var vt = document.getElementById('video-track-' + trackIdx);
      new Sortable(vt, {
        group: {name:'timeline', pull:false, put:function(to, from, el) {
          if (!el.classList.contains('clip-card') || el.classList.contains('in-tl') || el.classList.contains('ignored')) return false;
          // Any clip can land on any layer.
          return true;
        }},
        sort: false,
        draggable: '.no-sort-dummy',
        animation: 0,
        onAdd: function(evt) {
          var el = evt.item;
          el.style.display = 'none';
          var id = parseInt(el.dataset.id);
          var clip = allClips.find(function(c){return c.id===id});
          el.remove();
          if (clip) {
            var startTime = Math.max(0, Math.round(lastInsertX / CELL_W * 2) / 2);
            addVideoItem(clip, startTime, trackIdx);
          }
        },
      });

      /* Insertion bar */
      var insertBar = vt.querySelector('.vt-insert-bar');
      vt.addEventListener('dragover', function(e) {
        var rect = vt.getBoundingClientRect();
        var mouseX = e.clientX - rect.left + vt.parentElement.parentElement.scrollLeft;
        var snapped = Math.max(0, Math.round(mouseX / (CELL_W / 2)) * (CELL_W / 2));
        lastInsertX = snapped;
        insertBar.style.left = snapped + 'px';
        insertBar.style.display = 'block';
      });
      vt.addEventListener('dragleave', function(e) {
        if (!vt.contains(e.relatedTarget)) insertBar.style.display = 'none';
      });
      vt.addEventListener('drop', function() { insertBar.style.display = 'none'; });
    })(t);
  }
  document.addEventListener('dragend', function() {
    document.querySelectorAll('.vt-insert-bar').forEach(function(b) { b.style.display = 'none'; });
  });

  /* Sound track: click to add music */
  document.getElementById('sound-track').addEventListener('click', function(e) {
    if (e.target.closest('.sblock')) return;
    var rect = this.getBoundingClientRect();
    var x = e.clientX - rect.left + this.parentElement.parentElement.scrollLeft;
    var startSec = Math.max(0, Math.round((x - 50) / CELL_W));
    openMusicPicker(startSec);
  });

  /* Text track: click to add */
  document.getElementById('text-track').addEventListener('click', function(e) {
    if (e.target.closest('.tblock')) return;
    var rect = this.getBoundingClientRect();
    var x = e.clientX - rect.left + this.parentElement.parentElement.scrollLeft;
    var startSec = Math.max(0, Math.round((x - 50) / CELL_W));
    openTextEditor(null, startSec, startSec + 3);
  });

  syncTl();
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
  var heartCls = c.favorite ? ' on' : '';
  return '<div class="' + cls + '" data-id="' + c.id + '" data-tc="' + (c.thumb_count||1) + '" oncontextmenu="showCtx(event,' + c.id + ')">'
    + '<img class="thumb" src="/api/thumbnail/' + c.id + '" loading="lazy"/>'
    + '<div class="play-overlay" onclick="event.stopPropagation();playScene(this,' + c.id + ')"><div class="play-circle"><svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div></div>'
    + '<button class="pg-heart' + heartCls + '" onclick="event.stopPropagation();bbToggleClipFavorite(' + c.id + ')" title="Toggle favorite">' + PG_HEART_SVG + '</button>'
    + '<span class="dur">' + c.duration + 's</span>'
    + '<button class="hide-btn" title="' + (c.ignored ? 'Unhide' : 'Hide') + '" onclick="event.stopPropagation();toggleIgnore(' + c.id + ',' + !c.ignored + ')">' + (c.ignored ? '&#9711;' : '&#10005;') + '</button>'
    + '<span class="ignore-badge">HIDDEN</span>'
    + '<span class="wide-badge"></span>'
    + dots
    + '<div class="info">'
    + '<div class="fn" title="' + c.filename + '">' + c.filename.substring(0,25) + '</div>'
    + '<div class="tg" title="' + c.tags.join(', ') + '">' + tags + '</div>'
    + '</div></div>';
}

function renderGrid() {
  // Column-based filter chain: Folder → File → Tag → transcript search.
  var clips = allClips.filter(function(c){return !c.ignored});
  if (bbFolderFiles) {
    clips = clips.filter(function(c){return bbFolderFiles.has(c.filename)});
  }
  if (selectedFile) {
    clips = clips.filter(function(c){return c.filename === selectedFile});
  }
  if (selectedTag) {
    clips = clips.filter(function(c){return (c.tags||[]).indexOf(selectedTag) >= 0});
  }
  if (searchHitIds) {
    clips = clips.filter(function(c){return searchHitIds.has(c.id)});
  }
  var grid = document.getElementById('clip-grid');
  if (!clips.length) {
    grid.innerHTML = '<div class="bb-empty">No scenes match the current filters.</div>';
  } else {
    grid.innerHTML = clips.map(cardHTML).join('');
  }
  document.getElementById('clip-count').textContent =
    clips.length + ' scene' + (clips.length === 1 ? '' : 's');

  if (gridSort) gridSort.destroy();
  gridSort = new Sortable(grid, {
    group: {name:'timeline',pull:'clone',put:false},
    sort: false,
    animation: 150,
    filter: '.in-tl,.ignored',
    setData: function(dataTransfer, el) {
      /* Create small drag image near cursor */
      var img = el.querySelector('.thumb');
      if (img) {
        var preview = document.getElementById('drag-preview');
        if (!preview) {
          preview = document.createElement('div');
          preview.id = 'drag-preview';
          preview.innerHTML = '<img/>';
          document.body.appendChild(preview);
        }
        preview.querySelector('img').src = img.src;
        dataTransfer.setDragImage(preview, 35, 25);
      }
    },
  });
}

function getTlClipIds() {
  return videoItems.map(function(v){return v.clip.id}).filter(function(id){return id !== undefined});
}

function clearTimeline() {
  if (!videoItems.length && !soundItems.length && !textItems.length) {
    // Even if in-memory is empty, nuke any stale persisted state so legacy
    // entries from earlier sessions can't resurface on the next load.
    clearBuilderState();
    try { localStorage.removeItem('pg-builder-pending-scenes-v1'); } catch (e) {}
    return;
  }
  if (!window.confirm('Clear the entire timeline?')) return;
  videoItems = []; soundItems = []; textItems = [];
  clearBuilderState();
  try { localStorage.removeItem('pg-builder-pending-scenes-v1'); } catch (e) {}
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
  var indices = Array.from(segmentState.selected).sort(function(a,b){return a-b});
  for (var i = 0; i < indices.length; i++) {
    var idx = indices[i];
    var segStart = Math.round((clip.start + interval * idx) * 100) / 100;
    var segEnd = Math.round(Math.min(clip.start + interval * (idx + 1), clip.end) * 100) / 100;
    var segDur = Math.round((segEnd - segStart) * 10) / 10;
    videoItems.push({
      clip: {video_file: clip.video_file, start: segStart, end: segEnd, filename: clip.filename, wide: clip.wide},
      duration: segDur,
      startTime: getVideoDuration(),
      trans_in: selectedTransition,
      trans_out: null,
      volume: 5,
      stackOrder: 0,
      crop_x_frac: null,
    });
  }
  closeSegmentModal();
  syncTl();
}

function closeSegmentModal() {
  document.getElementById('segment-modal').classList.remove('active');
  segmentState = {clip: null, selected: new Set()};
}

function syncTl() {
  renderRuler();
  renderVideoTrack();
  renderSoundTrack();
  renderTextTrack();
  saveBuilderState();
  var total = getVideoDuration();
  var summary = videoItems.length + ' clip' + (videoItems.length !== 1 ? 's' : '');
  if (soundItems.length) summary += ' + ' + soundItems.length + ' music';
  if (textItems.length) summary += ' + ' + textItems.length + ' text';
  summary += ' \u2014 ' + total.toFixed(1) + 's';
  document.getElementById('tl-total').textContent = summary;

  var clipIds = getTlClipIds();
  document.querySelectorAll('#clip-grid .clip-card').forEach(function(card) {
    var cid = parseInt(card.dataset.id);
    card.classList.toggle('in-tl', clipIds.indexOf(cid) >= 0);
  });
}

function filterByTag() { renderGrid(); }

// ── Mac-Finder-style 3-column scene browser ──────────────────────────────
//
// Column 1 = files (single-select; "All Files" row clears the filter).
// Column 2 = tags present in the currently-selected file (or all clips
//   when no file is picked). "All Tags" clears the tag filter.
// Column 3 = the existing scene grid, filtered by file → tag → transcript
//   search (column 3 has no UI of its own; it just reacts to columns 1/2
//   and the search bar at the top).

var selectedFolder = 'all'; // smart-folder id or user-folder id; default = "All Files"
var selectedFile = null;    // null = "All Files" within folder
var selectedTag  = null;    // null = "All Tags"
var searchHitIds = null;    // null = no search active; Set<sceneId> when active
var _bbSearchTimer = null;
// Folder state (refreshed from /api/folders/list).
var bbFolders = { smart: [], user: [], memberships: {} };
// Set of filenames in the currently-selected folder. null = no folder
// restriction (shouldn't normally happen since default is "all"); empty
// Set = folder exists but contains nothing.
var bbFolderFiles = null;
// Filenames flagged as favorites (from /api/folders/list ?scope=source).
var bbFileFavorites = new Set();

// Shared heart-icon SVG (filled/outline are switched via .pg-heart.on
// in CSS — same icon, just recolored / re-filled).
var PG_HEART_SVG =
  '<svg viewBox="0 0 24 24"><path d="M12 21s-7-4.35-9.5-9.13C.9 8.5 2.5 5 6 5c2 0 3.4 1.1 6 4 2.6-2.9 4-4 6-4 3.5 0 5.1 3.5 3.5 6.87C19 16.65 12 21 12 21z"/></svg>';

// Strip the extension and turn underscores/dashes into spaces so the
// files column reads like prose instead of a slug.
function _ffPretty(name) {
  var base = (name || '').replace(/\.[^.]+$/, '');
  return base.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function _bbEsc(s) {
  return (s || '').replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

// Persist column selections + search query across navigations. The
// keys are page-scoped so /builder doesn't read /rate's state.
var _PG_STATE_KEY = 'pg.builder.state';
function _pgSaveState() {
  try {
    localStorage.setItem(_PG_STATE_KEY, JSON.stringify({
      folder: selectedFolder,
      file:   selectedFile,
      tag:    selectedTag,
      search: (document.getElementById('bb-search') || {}).value || '',
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
        var i = document.getElementById('bb-search');
        if (i) i.value = s.search;
      }
    }
  } catch (e) {}
}

async function bbInit() {
  _pgLoadState();
  await bbReloadFolders();
  // _bbRecomputeFolderFiles (called from bbReloadFolders) snaps an
  // unknown folder id back to "all" — guarantees a saved-but-deleted
  // folder doesn't break the page.
  bbRenderFolderCol();
  bbRenderFilesCol();
  bbRenderTagsCol();
  // Kick off transcript search if the stored value brings one back.
  var s = (document.getElementById('bb-search') || {}).value || '';
  if (s.trim()) _bbDoSearch(s.trim());
  // Single delegated listener per column handles row clicks; cheaper
  // than re-binding every render and dodges the filename-quoting
  // problem an inline onclick would have.
  var foldersList = document.getElementById('bb-folders-list');
  foldersList.addEventListener('click', function(e) {
    // Action buttons (rename / delete) live inside the row; let their
    // own handlers run instead of treating the click as row-selection.
    if (e.target.closest('.bb-row-icon-btn')) return;
    var row = e.target.closest('.bb-row');
    if (!row) return;
    bbSelectFolder(row.getAttribute('data-fid'));
  });
  // Drag-and-drop wiring for the folders column. Drop on a USER folder
  // moves the dragged file there; smart folders refuse drops.
  foldersList.addEventListener('dragover', _bbFolderDragOver);
  foldersList.addEventListener('dragleave', _bbFolderDragLeave);
  foldersList.addEventListener('drop', _bbFolderDrop);

  var filesList = document.getElementById('bb-files-list');
  filesList.addEventListener('click', function(e) {
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
  // Each file row carries its filename via dragstart; the folders column
  // listener reads it from dataTransfer on drop.
  filesList.addEventListener('dragstart', function(e) {
    var row = e.target.closest('.bb-row[draggable="true"]');
    if (!row) return;
    var fn = row.getAttribute('data-fn');
    if (!fn) return;
    e.dataTransfer.setData('text/plain', fn);
    e.dataTransfer.setData('application/x-pg-file', fn);
    e.dataTransfer.effectAllowed = 'move';
  });
  var tagsList = document.getElementById('bb-tags-list');
  tagsList.addEventListener('click', function(e) {
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
    // Fall back to All Files if the selection vanished (e.g. folder deleted).
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
    var icon = '';
    if (isSmart) {
      if (f.id === 'all') icon = '<span class="bb-row-smart-icon">&#9776;</span>';
      else if (f.id === 'today') icon = '<span class="bb-row-smart-icon">&#9728;</span>';
      else icon = '<span class="bb-row-smart-icon">&#9728;</span>';
    } else {
      icon = '<span class="bb-row-smart-icon">&#128193;</span>';
    }
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
  // Wire per-row action buttons (rename / delete). One delegated handler
  // would also work but doing it here keeps the click intent crystal.
  list.querySelectorAll('.bb-row-icon-btn').forEach(function(b){
    b.addEventListener('click', function(e){
      e.stopPropagation();
      var fid = b.getAttribute('data-fid');
      var act = b.getAttribute('data-act');
      if (act === 'rename') bbRenameFolder(fid);
      else if (act === 'delete') bbDeleteFolder(fid);
    });
  });
}

function bbSelectFolder(fid) {
  if (!fid) return;
  selectedFolder = fid;
  _bbRecomputeFolderFiles();
  // Reset downstream selections if they no longer apply to the new pool.
  if (selectedFile && bbFolderFiles && !bbFolderFiles.has(selectedFile)) {
    selectedFile = null;
  }
  bbRenderFolderCol();
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
  _pgSaveState();
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
  // If we just deleted the active folder, snap back to All Files.
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
  // Restrict the pool to files that live in the currently-selected
  // folder. bbFolderFiles is a Set of filenames; null means no folder
  // restriction (treated as "all").
  var folderSet = bbFolderFiles;
  var counts = {};
  var totalAll = 0;
  for (var i = 0; i < allClips.length; i++) {
    var c = allClips[i];
    if (c.ignored) continue;
    var fn = c.filename || '';
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
       + ' data-fn="">'
       + '<span class="bb-row-name">All Files</span>'
       + '<span class="bb-row-count">' + totalAll + '</span>'
       + '</div>';
  for (var i = 0; i < files.length; i++) {
    var f = files[i];
    var pretty = _bbEsc(_ffPretty(f));
    var safe = _bbEsc(f);
    // draggable=true makes the row a drag source for the Folders column.
    var favCls = bbFileFavorites.has(f) ? ' on' : '';
    html += '<div class="bb-row' + (selectedFile===f?' selected':'') + '"'
         + ' draggable="true"'
         + ' data-fn="' + safe + '" title="' + safe + '">'
         + '<button class="pg-heart' + favCls + '" data-fn="' + safe + '"'
         + ' data-act="fav-file" title="Toggle favorite">' + PG_HEART_SVG + '</button>'
         + '<span class="bb-row-name">' + pretty + '</span>'
         + '<span class="bb-row-count">' + counts[f] + '</span>'
         + '</div>';
  }
  document.getElementById('bb-files-list').innerHTML = html;
}

async function bbToggleFileFavorite(filename) {
  var on = !bbFileFavorites.has(filename);
  if (on) bbFileFavorites.add(filename);
  else bbFileFavorites.delete(filename);
  bbRenderFilesCol();
  try {
    await fetch('/api/folders/favorite', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: filename, favorite: on, scope: 'source'}),
    });
  } catch (e) {}
  await bbReloadFolders();
  bbRenderFolderCol();
  bbRenderFilesCol();
  renderGrid();
}

async function bbToggleClipFavorite(sceneId) {
  var c = allClips.find(function(x){ return x.id === sceneId; });
  if (!c) return;
  var on = !c.favorite;
  c.favorite = on;
  document.querySelectorAll('.clip-card[data-id="' + sceneId + '"] .pg-heart')
    .forEach(function(b){ b.classList.toggle('on', on); });
  try {
    await fetch('/rate/api/favorite', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({scene_id: sceneId, favorite: on}),
    });
  } catch (e) {}
}

function bbRenderTagsCol() {
  var pool = allClips.filter(function(c){return !c.ignored});
  if (bbFolderFiles) {
    pool = pool.filter(function(c){return bbFolderFiles.has(c.filename)});
  }
  if (selectedFile) {
    pool = pool.filter(function(c){return c.filename === selectedFile});
  }
  var counts = {};
  for (var i = 0; i < pool.length; i++) {
    var ts = pool[i].tags || [];
    for (var j = 0; j < ts.length; j++) counts[ts[j]] = (counts[ts[j]] || 0) + 1;
  }
  var tags = Object.keys(counts).sort();
  var html = '';
  html += '<div class="bb-row bb-row-all' + (selectedTag===null?' selected':'') + '"'
       + ' data-tag="">'
       + '<span class="bb-row-name">All Tags</span>'
       + '<span class="bb-row-count">' + pool.length + '</span>'
       + '</div>';
  for (var i = 0; i < tags.length; i++) {
    var t = tags[i];
    var safe = _bbEsc(t);
    html += '<div class="bb-row' + (selectedTag===t?' selected':'') + '"'
         + ' data-tag="' + safe + '">'
         + '<span class="bb-row-name">' + safe + '</span>'
         + '<span class="bb-row-count">' + counts[t] + '</span>'
         + '</div>';
  }
  document.getElementById('bb-tags-list').innerHTML = html;
}

function bbSelectFile(fn) {
  selectedFile = fn || null;
  // If the previously-active tag isn't in the new file, fall back to
  // "All Tags" so the scene grid doesn't go silently empty.
  if (selectedTag) {
    var pool = allClips.filter(function(c){
      return !c.ignored
          && (selectedFile === null || c.filename === selectedFile);
    });
    var has = pool.some(function(c){
      return (c.tags || []).indexOf(selectedTag) >= 0;
    });
    if (!has) selectedTag = null;
  }
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
  _pgSaveState();
}

function bbSelectTag(t) {
  selectedTag = t || null;
  bbRenderTagsCol();
  renderGrid();
  _pgSaveState();
}

function onBbSearchInput() {
  var q = (document.getElementById('bb-search').value || '').trim();
  if (_bbSearchTimer) clearTimeout(_bbSearchTimer);
  // Debounce so each keystroke doesn't hit the server.
  _bbSearchTimer = setTimeout(function(){ _bbDoSearch(q); _pgSaveState(); }, 280);
}

async function _bbDoSearch(q) {
  var status = document.getElementById('bb-search-status');
  if (!q) {
    searchHitIds = null;
    if (status) status.textContent = '';
    renderGrid();
    return;
  }
  if (status) status.textContent = 'searching…';
  try {
    // Reuse the Scenes-page transcript search endpoint — same dataset,
    // same indexing, so the result set is identical.
    var r = await fetch('/rate/api/search?q=' + encodeURIComponent(q));
    var data = await r.json();
    searchHitIds = new Set(data.scene_ids || []);
  } catch (e) {
    searchHitIds = new Set();
  }
  if (status) {
    status.textContent = searchHitIds.size
      ? (searchHitIds.size + ' transcript match' + (searchHitIds.size !== 1 ? 'es' : ''))
      : 'no transcript matches';
  }
  renderGrid();
}

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

  // Refresh the file / tag columns so counts (and disappearing tags) stay
  // accurate after a hide / unhide. Render order matters: files first so
  // the tags column sees the up-to-date selectedFile pool.
  bbRenderFilesCol();
  bbRenderTagsCol();
  renderGrid();
}

function buildTimeline() {
  /* Sort by startTime for generation order */
  var sorted = videoItems.slice().sort(function(a,b){return a.startTime - b.startTime});
  var vt = [];
  for (var i = 0; i < sorted.length; i++) {
    var vi = sorted[i];
    var c = vi.clip;
    var entry = {type:'clip', start_time:vi.startTime, track:vi.track||0, wide:!!c.wide, stack_order:vi.stackOrder||0, volume:vi.volume||5,
      muted:!!vi.muted, position:vi.position||null,
      trans_in:vi.trans_in||null, trans_out:vi.trans_out||null,
      crop_x_frac: (typeof vi.crop_x_frac === 'number') ? vi.crop_x_frac : null,
      free_crops: (Array.isArray(vi.free_crops) && vi.free_crops.length) ? vi.free_crops : null,
      captions: (function(){
        var v = vi.captions;
        if (v === true)  return 'bottom';   // legacy in-memory state
        if (v === false) return 'none';
        if (CAPTIONS_CLIP_CYCLE.indexOf(v) >= 0) return v;
        return 'inherit';
      })()};
    if (c.id !== undefined) entry.id = c.id;
    else { entry.video_file = c.video_file; entry.start = c.start; entry.end = c.end; }
    vt.push(entry);
  }
  var st = soundItems.map(function(s) {
    return {name:s.name, volume:s.volume, start_time:s.startTime, duration:s.duration};
  });
  var tt = [];
  for (var ti = 0; ti < textItems.length; ti++) {
    var tg = textItems[ti];
    var boxes = tg.boxes || [{text:tg.text,fontsize:tg.fontsize,fontcolor:tg.fontcolor,
      bold:tg.bold,box_opacity:tg.box_opacity,x_frac:tg.x_frac,y_frac:tg.y_frac}];
    for (var bj = 0; bj < boxes.length; bj++) {
      var bx = boxes[bj];
      var o = {text:bx.text, start_time:tg.startTime, end_time:tg.endTime,
              fontsize:bx.fontsize||42, fontcolor:bx.fontcolor||'white',
              box_opacity:bx.box_opacity !== undefined ? bx.box_opacity : 0.5,
              trans_in:tg.trans_in||'fade', trans_out:tg.trans_out||'fade'};
      if (bx.fontfamily) o.fontfamily = bx.fontfamily;
      if (bx.x_frac !== undefined) { o.x_frac = bx.x_frac; o.y_frac = bx.y_frac; }
      if (bx.w_frac) { o.w_frac = bx.w_frac; o.h_frac = bx.h_frac; }
      if (bx.bold) o.bold = true;
      if (bx.italic) o.italic = true;
      if (bx.bgcolor) o.bgcolor = bx.bgcolor;
      tt.push(o);
    }
  }
  return {video_track:vt, sound_track:st, text_overlays:tt,
    track_settings: trackSettings.map(function(s){
      return {muted: !!s.muted, default_position: s.default_position,
              captions: s.captions || 'none',
              default_crop_x_frac: (typeof s.default_crop_x_frac === 'number') ? s.default_crop_x_frac : null};
    }),
    track_count: tlTrackCount,
    track_sequential: tlSequential.slice(),
    include_intro: document.getElementById('include-intro').checked,
    include_outro: document.getElementById('include-outro').checked};
}

/* Persist builder state across page navigation. Saved on every timeline
 * mutation (via syncTl) and restored in init() before any URL-param actions
 * so handoffs like ?add_scene=<id> append to the saved timeline rather than
 * starting from scratch. */
var BUILDER_STATE_KEY = 'pg-builder-state-v1';
/* Guard: setupTracks() calls syncTl() which calls saveBuilderState() before
 * init's restoreBuilderState() runs. Without this flag, the initial empty
 * snapshot wipes any saved timeline on every page load. Flipped to true
 * once init() finishes restoring (or deciding not to). */
var _builderSaveEnabled = false;

function saveBuilderState() {
  if (!_builderSaveEnabled) return;
  try {
    var snap = buildTimeline();
    localStorage.setItem(BUILDER_STATE_KEY, JSON.stringify(snap));
  } catch (e) { /* quota / serialization — ignore */ }
}

function restoreBuilderState() {
  try {
    var raw = localStorage.getItem(BUILDER_STATE_KEY);
    if (!raw) return false;
    var data = JSON.parse(raw);
    if (!data || !data.video_track) return false;
    // Don't restore an empty timeline — nothing to restore.
    var hasContent = (data.video_track && data.video_track.length)
                  || (data.sound_track && data.sound_track.length)
                  || (data.text_overlays && data.text_overlays.length);
    if (!hasContent) return false;
    loadNewTimeline(data);
    return true;
  } catch (e) { return false; }
}

function clearBuilderState() {
  try { localStorage.removeItem(BUILDER_STATE_KEY); } catch (e) {}
}

async function generateVideo() {
  if (!videoItems.length) { alert('Add clips to the timeline first!'); return; }

  var btn = document.getElementById('gen-btn');
  btn.disabled = true;
  document.getElementById('loading').classList.add('active');

  try {
    var res = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(buildTimeline()),
    });
    var data = await res.json();
    document.getElementById('loading').classList.remove('active');
    btn.disabled = false;

    if (data.error) {
      alert('Error: ' + data.error);
    } else {
      resultPath = data.path;
      var vid = document.getElementById('result-video');
      vid.src = '/api/serve-video?path=' + encodeURIComponent(data.path);
      vid.load();
      vid.play().catch(function(){});
      document.getElementById('result-info').textContent =
        data.duration + 's \u2014 ' + data.path;
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
  var vid = document.getElementById('result-video');
  vid.pause(); vid.src = '';
  document.getElementById('result-modal').classList.remove('active');
}

// -- Load timeline from JSON --
function triggerLoad() {
  document.getElementById('load-input').click();
}

function handleLoadFile(e) {
  var file = e.target.files[0];
  if (!file) return;
  var ext = file.name.split('.').pop().toLowerCase();
  var videoExts = ['mp4','mov','avi','mkv','webm','m4v'];
  if (videoExts.indexOf(ext) >= 0) {
    fetch('/api/load-video', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename: file.name}),
    }).then(function(r){return r.json()}).then(function(data) {
      if (!data.timeline) return;
      if (data.timeline.video_track) loadNewTimeline(data.timeline);
      else loadOldTimeline(data.timeline);
    });
    e.target.value = '';
    return;
  }
  var reader = new FileReader();
  reader.onload = function(ev) {
    try {
      var data = JSON.parse(ev.target.result);
      if (data.video_track) {
        loadNewTimeline(data);
      } else if (data.timeline) {
        loadOldTimeline(data.timeline);
      }
    } catch (err) { alert('Invalid JSON: ' + err.message); }
  };
  reader.readAsText(file);
  e.target.value = '';
}

function loadOldTimeline(timeline) {
  /* Convert old flat format to new multi-track */
  videoItems = []; soundItems = []; textItems = [];
  var pendTrans = null;
  for (var i = 0; i < timeline.length; i++) {
    var item = timeline[i];
    if (item.type === 'transition') { pendTrans = item.name; }
    else if (item.type === 'clip') {
      var fname = (item.video_file || '').split('/').pop();
      var clip = null;
      if (item.id !== undefined) {
        clip = allClips.find(function(c){return c.id===item.id});
      }
      if (!clip && item.video_file) {
        clip = allClips.find(function(c){
          return c.filename === fname && Math.abs(c.start - item.start) < 0.1;
        });
      }
      var dur = clip ? clip.duration : (item.end - item.start);
      videoItems.push({
        clip: clip ? {id:clip.id, video_file:clip.video_file, start:clip.start, end:clip.end, filename:clip.filename, wide:clip.wide}
                    : {video_file:item.video_file, start:item.start, end:item.end, filename:fname},
        duration: dur,
        startTime: getVideoDuration(),
        trans_in: videoItems.length > 0 ? (pendTrans || 'fade') : null,
        trans_out: null,
        volume: 5,
        stackOrder: 0,
        crop_x_frac: null,
      });
      pendTrans = null;
    } else if (item.type === 'music' && item.name) {
      addSoundBlock(item.name, item.volume || 3, 0, Math.max(5, getVideoDuration()));
    }
  }
  syncTl();
}

function loadNewTimeline(data) {
  videoItems = []; soundItems = []; textItems = [];
  var vt = data.video_track || [];
  var pendTrans = null;
  for (var i = 0; i < vt.length; i++) {
    var item = vt[i];
    if (item.type === 'transition') { pendTrans = item.name; continue; }
    if (item.type !== 'clip') continue;
    var clip = null;
    if (item.id !== undefined) clip = allClips.find(function(c){return c.id===item.id});
    var dur = clip ? clip.duration : (item.end - item.start);
    var itemTrack = item.track || 0;
    videoItems.push({
      clip: clip ? {id:clip.id, video_file:clip.video_file, start:clip.start, end:clip.end, filename:clip.filename, wide:clip.wide}
                  : {video_file:item.video_file, start:item.start, end:item.end, filename:(item.video_file||'').split('/').pop()},
      duration: dur,
      startTime: item.start_time !== undefined ? item.start_time : getVideoDuration(),
      track: itemTrack,
      trans_in: item.trans_in || (videoItems.length > 0 ? (pendTrans || null) : null),
      trans_out: item.trans_out || null,
      volume: item.volume || 5,
      muted: !!item.muted,
      position: item.position || null,
      stackOrder: item.stack_order || 0,
      // Captions: accept the new enum or migrate the old tri-state.
      captions: (function(){
        var v = item.captions;
        if (v === true)  return 'bottom';
        if (v === false) return 'none';
        if (CAPTIONS_CLIP_CYCLE.indexOf(v) >= 0) return v;
        return 'inherit';
      })(),
      crop_x_frac: (typeof item.crop_x_frac === 'number') ? item.crop_x_frac : null,
      free_crops: Array.isArray(item.free_crops) ? item.free_crops : null,
    });
    if (itemTrack > 0 && itemTrack >= tlTrackCount) setTrackCount(itemTrack + 1);
    pendTrans = null;
  }
  /* Track-level settings: layer mute + default wide-clip position */
  if (Array.isArray(data.track_settings)) {
    var defaults = ['top','center','bottom'];
    for (var ts = 0; ts < 3; ts++) {
      var src = data.track_settings[ts] || {};
      trackSettings[ts] = {
        muted: !!src.muted,
        default_position: src.default_position || defaults[ts],
        default_crop_x_frac: (typeof src.default_crop_x_frac === 'number') ? src.default_crop_x_frac : null,
        // Old saves had a boolean; new ones have 'none'/'top'/'middle'/'bottom'.
        captions: (function(){
          var v = src.captions;
          if (v === true)  return 'bottom';
          if (v === false || v === undefined || v === null) return 'none';
          if (['none','top','middle','bottom'].indexOf(v) >= 0) return v;
          return 'none';
        })(),
      };
    }
  }
  /* Per-layer Free/Seq mode */
  if (Array.isArray(data.track_sequential)) {
    for (var sq = 0; sq < 3; sq++) {
      tlSequential[sq] = data.track_sequential[sq] !== false;
    }
  }
  /* Visible layer count (also expand if any clip lives on a higher layer) */
  var maxClipTrack = 0;
  for (var ci = 0; ci < videoItems.length; ci++) {
    if ((videoItems[ci].track || 0) > maxClipTrack) maxClipTrack = videoItems[ci].track;
  }
  var desiredCount = Math.max(
    data.track_count || 1,
    maxClipTrack + 1,
  );
  if (desiredCount !== tlTrackCount) setTrackCount(desiredCount);
  /* Intro/outro toggles */
  if (typeof data.include_intro === 'boolean') {
    var ii = document.getElementById('include-intro');
    if (ii) ii.checked = !!data.include_intro;
  }
  if (typeof data.include_outro === 'boolean') {
    var io = document.getElementById('include-outro');
    if (io) io.checked = !!data.include_outro;
  }
  renderTrackHeaders();
  var st = data.sound_track || [];
  for (var j = 0; j < st.length; j++) {
    soundItems.push({id:++nextBlkId, name:st[j].name, volume:st[j].volume||3,
      startTime:st[j].start_time||0, duration:st[j].duration||10, el:null});
  }
  /* Group loaded text overlays by matching start_time+end_time */
  var tt = data.text_overlays || [];
  var ttGroups = {};
  for (var k = 0; k < tt.length; k++) {
    var key = (tt[k].start_time||0) + '_' + (tt[k].end_time||3);
    if (!ttGroups[key]) ttGroups[key] = {startTime:tt[k].start_time||0, endTime:tt[k].end_time||3, trans_in:tt[k].trans_in||'fade', trans_out:tt[k].trans_out||'fade', boxes:[]};
    ttGroups[key].boxes.push({text:tt[k].text, fontsize:tt[k].fontsize||42,
      fontcolor:tt[k].fontcolor||'white', bold:tt[k].bold||false,
      box_opacity:tt[k].box_opacity!==undefined?tt[k].box_opacity:0.5,
      x_frac:tt[k].x_frac, y_frac:tt[k].y_frac, w_frac:tt[k].w_frac, h_frac:tt[k].h_frac});
  }
  for (var gk in ttGroups) {
    var g = ttGroups[gk];
    textItems.push({id:++nextBlkId, label:g.boxes.map(function(b){return b.text}).join(' / '),
      boxes:g.boxes, trans_in:g.trans_in, trans_out:g.trans_out, startTime:g.startTime, endTime:g.endTime, el:null});
  }
  syncTl();
}

function cancelLoad() {
  document.getElementById('load-modal').classList.remove('active');
}

/* ─── Transition Picker ─── */
var transPickerIdx = -1;
var transPickerDir = 'in'; /* 'in' or 'out' */

function openTransPicker(idx) { openVideoTransPicker(idx, 'in'); }

function openVideoTransPicker(idx, dir) {
  transPickerIdx = idx;
  transPickerDir = dir || 'in';
  var vi = videoItems[idx];
  var current = (dir === 'out' ? vi.trans_out : vi.trans_in) || '';
  var grid = document.getElementById('trans-picker-grid');
  grid.innerHTML = '';
  for (var i = 0; i < TRANSITION_LIST.length; i++) {
    var name = TRANSITION_LIST[i];
    var item = document.createElement('div');
    item.className = 'tp-item' + (name === current ? ' current' : '');
    item.textContent = name;
    item.addEventListener('click', function(n) {
      return function() { pickTransition(n); };
    }(name));
    grid.appendChild(item);
  }
  document.getElementById('trans-picker-modal').classList.add('active');
}

function pickTransition(name) {
  if (transPickerIdx >= 0 && transPickerIdx < videoItems.length) {
    if (transPickerDir === 'out') videoItems[transPickerIdx].trans_out = name;
    else videoItems[transPickerIdx].trans_in = name;
  }
  closeTransPicker();
  syncTl();
}

function removeTransition() {
  if (transPickerIdx >= 0 && transPickerIdx < videoItems.length) {
    if (transPickerDir === 'out') videoItems[transPickerIdx].trans_out = null;
    else videoItems[transPickerIdx].trans_in = null;
  }
  closeTransPicker();
  syncTl();
}

function closeTransPicker() {
  document.getElementById('trans-picker-modal').classList.remove('active');
  transPickerIdx = -1;
}

/* ─── Music Picker ─── */
var musicPickerStartSec = 0;

function openMusicPicker(startSec) {
  musicPickerStartSec = startSec;
  var list = document.getElementById('music-picker-list');
  list.innerHTML = '';
  for (var i = 0; i < musicList.length; i++) {
    var m = musicList[i];
    var col = musicColorMap[m.name] || NO_MUSIC_COLOR;
    var item = document.createElement('div');
    item.className = 'mp-item';
    item.style.background = col.bg;
    item.style.color = col.fg;
    item.innerHTML = '<span class="dot" style="background:' + col.dot + '"></span>' + m.name;
    item.addEventListener('click', function(name) {
      return function() { pickMusic(name); };
    }(m.name));
    list.appendChild(item);
  }
  document.getElementById('music-picker-modal').classList.add('active');
}

function pickMusic(name) {
  addSoundBlock(name, 3, musicPickerStartSec, Math.max(5, getVideoDuration() - musicPickerStartSec));
  closeMusicPicker();
}

function closeMusicPicker() {
  document.getElementById('music-picker-modal').classList.remove('active');
}

/* ─── Text Transition Picker ─── */
var textTransPickerId = -1;
var textTransPickerDir = 'in';

function openTextTransPicker(itemId, dir) {
  textTransPickerId = itemId;
  textTransPickerDir = dir || 'in';
  var item = textItems.find(function(t){return t.id===itemId});
  if (!item) return;
  var current = dir === 'out' ? (item.trans_out || 'fade') : (item.trans_in || 'fade');
  var grid = document.getElementById('trans-picker-grid');
  grid.innerHTML = '';
  for (var i = 0; i < TEXT_TRANSITIONS.length; i++) {
    var name = TEXT_TRANSITIONS[i];
    var el = document.createElement('div');
    el.className = 'tp-item' + (name === current ? ' current' : '');
    el.textContent = name;
    el.addEventListener('click', function(n) {
      return function() { pickTextTrans(n); };
    }(name));
    grid.appendChild(el);
  }
  var label = dir === 'out' ? 'Exit Animation' : 'Enter Animation';
  document.querySelector('#trans-picker-modal h2').textContent = label;
  document.querySelector('#trans-picker-modal h2').style.color = '#4040a0';
  document.getElementById('trans-picker-modal').classList.add('active');
}

function pickTextTrans(name) {
  var item = textItems.find(function(t){return t.id===textTransPickerId});
  if (item) {
    if (textTransPickerDir === 'out') item.trans_out = name;
    else item.trans_in = name;
  }
  document.getElementById('trans-picker-modal').classList.remove('active');
  document.querySelector('#trans-picker-modal h2').textContent = 'Change Transition';
  document.querySelector('#trans-picker-modal h2').style.color = '#e53935';
  textTransPickerId = -1;
  syncTl();
}

/* ─── Text Editor Modal ─── */
var teState = {editId: null, startTime: 0, endTime: 3, boxes: [], selectedBox: null, dragging: false};
var teNextBoxId = 0;

function openTextEditor(existingItem, startTime, endTime) {
  teState.editId = existingItem ? existingItem.id : null;
  teState.startTime = startTime;
  teState.endTime = endTime;
  teState.boxes = [];
  teState.selectedBox = null;

  var canvas = document.getElementById('te-canvas');
  canvas.innerHTML = '';

  /* Show modal FIRST so elements have layout dimensions */
  document.getElementById('text-editor-modal').classList.add('active');

  var boxOpts = [];
  if (existingItem && existingItem.boxes && existingItem.boxes.length) {
    for (var bi = 0; bi < existingItem.boxes.length; bi++) {
      var eb = existingItem.boxes[bi];
      boxOpts.push({
        text: eb.text || '',
        fontfamily: eb.fontfamily || null,
        fontsize: eb.fontsize || 42,
        fontcolor: eb.fontcolor && eb.fontcolor !== 'white' ? eb.fontcolor : '#ffffff',
        bold: eb.bold || false,
        italic: eb.italic || false,
        bgcolor: eb.bgcolor || '#000000',
        box_opacity: eb.box_opacity !== undefined ? eb.box_opacity : 0.5,
        x_frac: eb.x_frac !== undefined ? eb.x_frac : 0.5,
        y_frac: eb.y_frac !== undefined ? eb.y_frac : 0.5,
        w_frac: eb.w_frac,
        h_frac: eb.h_frac,
      });
    }
  } else if (existingItem && existingItem.text) {
    boxOpts.push({
      text: existingItem.text,
      fontfamily: existingItem.fontfamily || null,
      fontsize: existingItem.fontsize || 42,
      fontcolor: existingItem.fontcolor && existingItem.fontcolor !== 'white' ? existingItem.fontcolor : '#ffffff',
      bold: existingItem.bold || false,
      italic: existingItem.italic || false,
      bgcolor: existingItem.bgcolor || '#000000',
      box_opacity: existingItem.box_opacity !== undefined ? existingItem.box_opacity : 0.5,
      x_frac: existingItem.x_frac !== undefined ? existingItem.x_frac : 0.5,
      y_frac: existingItem.y_frac !== undefined ? existingItem.y_frac : 0.5,
      w_frac: existingItem.w_frac,
      h_frac: existingItem.h_frac,
    });
  } else {
    boxOpts.push({text: '', fontsize: 42, fontcolor: '#ffffff', bold: false, box_opacity: 0.5, x_frac: 0.5, y_frac: 0.5});
  }

  /* Create boxes now that modal is visible and has layout */
  for (var boi = 0; boi < boxOpts.length; boi++) {
    teCreateBox(boxOpts[boi]);
  }

  teSelectBox(teState.boxes[0]);

  /* Live toolbar bindings */
  document.getElementById('te-fontcolor').oninput = teToolbarChanged;
  document.getElementById('te-bgcolor').oninput = teToolbarChanged;

  /* If the "Display Video Background" toggle is still on from a previous
   * edit, refresh the preview now that we know which clip we're on. */
  teRefreshVideoBg();
}

function teCreateBox(opts) {
  var box = {
    id: ++teNextBoxId,
    text: opts.text || '',
    fontfamily: opts.fontfamily || null,
    fontsize: opts.fontsize || 42,
    fontcolor: opts.fontcolor || '#ffffff',
    bold: opts.bold || false,
    italic: opts.italic || false,
    bgcolor: opts.bgcolor || '#000000',
    box_opacity: opts.box_opacity !== undefined ? opts.box_opacity : 0.5,
    x_frac: opts.x_frac !== undefined ? opts.x_frac : 0.5,
    y_frac: opts.y_frac !== undefined ? opts.y_frac : 0.5,
    el: null,
  };

  var canvas = document.getElementById('te-canvas');
  var cW = canvas.offsetWidth || 360;
  var cH = canvas.offsetHeight || 640;
  var el = document.createElement('div');
  el.className = 'te-box';
  el.style.left = (box.x_frac * 100) + '%';
  el.style.top = (box.y_frac * 100) + '%';
  el.style.transform = 'translate(-50%,-50%)';
  if (opts.w_frac) el.style.width = Math.round(opts.w_frac * cW) + 'px';
  if (opts.h_frac) el.style.height = Math.round(opts.h_frac * cH) + 'px';

  var rmBtn = document.createElement('button');
  rmBtn.className = 'te-box-rm';
  rmBtn.textContent = '\u00d7';
  rmBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    teRemoveBox(box.id);
  });
  el.appendChild(rmBtn);

  var resizeHandle = document.createElement('div');
  resizeHandle.className = 'te-resize';
  el.appendChild(resizeHandle);

  el.addEventListener('mousedown', function(e) {
    if (e.target === rmBtn) return;
    teSelectBox(box);
  });

  el.addEventListener('dblclick', function(e) {
    e.stopPropagation();
    teEditBoxInline(box);
  });

  box.el = el;
  teState.boxes.push(box);
  canvas.appendChild(el);
  teRenderBox(box);
  teSetupBoxDrag(box);
  return box;
}

function teRemoveBox(boxId) {
  var box = teState.boxes.find(function(b){return b.id===boxId});
  if (!box) return;
  if (teState.boxes.length <= 1) return; /* keep at least one */
  box.el.remove();
  teState.boxes = teState.boxes.filter(function(b){return b.id !== boxId});
  if (teState.selectedBox && teState.selectedBox.id === boxId) {
    teSelectBox(teState.boxes[0]);
  }
}

function teSelectBox(box) {
  teState.selectedBox = box;
  /* Update selection visuals */
  teState.boxes.forEach(function(b) {
    b.el.classList.toggle('te-selected', b.id === box.id);
  });
  /* Load box props into toolbar */
  document.getElementById('te-font').value = box.fontfamily || '';
  document.getElementById('te-fontcolor').value = box.fontcolor;
  document.getElementById('te-bold-btn').classList.toggle('active', box.bold);
  document.getElementById('te-italic-btn').classList.toggle('active', box.italic);
  document.getElementById('te-bgcolor').value = box.bgcolor || '#000000';
  document.getElementById('te-bg-none-btn').classList.toggle('active', box.box_opacity > 0);
}

function teToolbarChanged() {
  var box = teState.selectedBox;
  if (!box) return;
  box.fontcolor = document.getElementById('te-fontcolor').value;
  box.bold = document.getElementById('te-bold-btn').classList.contains('active');
  box.italic = document.getElementById('te-italic-btn').classList.contains('active');
  box.bgcolor = document.getElementById('te-bgcolor').value;
  teRenderBox(box);
}

function teFontChanged() {
  var box = teState.selectedBox;
  if (!box) return;
  var val = document.getElementById('te-font').value;
  box.fontfamily = val || null;
  teRenderBox(box);
}

function teToggleBg() {
  var box = teState.selectedBox;
  if (!box) return;
  box.box_opacity = box.box_opacity > 0 ? 0 : 1;
  document.getElementById('te-bg-none-btn').classList.toggle('active', box.box_opacity > 0);
  teRenderBox(box);
}

function teRenderBox(box) {
  var el = box.el;
  var text = box.text || 'Text';

  /* Preserve special child elements */
  var rmBtn = el.querySelector('.te-box-rm');
  var resizeH = el.querySelector('.te-resize');

  /* Update or create text span */
  var span = el.querySelector('.te-text-span');
  if (!span) {
    span = document.createElement('span');
    span.className = 'te-text-span';
    el.insertBefore(span, rmBtn);
  }
  span.textContent = text;

  el.style.color = box.fontcolor;
  el.style.fontFamily = box.fontfamily ? '"' + box.fontfamily + '", sans-serif' : 'sans-serif';
  el.style.fontWeight = box.bold ? '700' : '400';
  el.style.fontStyle = box.italic ? 'italic' : 'normal';

  /* Auto-fit: find largest font that fits within the box */
  teFitText(box);

  /* Background */
  var existing = el.querySelector('.te-bg');
  if (existing) existing.remove();
  if (box.box_opacity > 0 && box.bgcolor) {
    var r = parseInt(box.bgcolor.slice(1,3),16);
    var g = parseInt(box.bgcolor.slice(3,5),16);
    var b = parseInt(box.bgcolor.slice(5,7),16);
    var bg = document.createElement('div');
    bg.className = 'te-bg';
    bg.style.background = 'rgba(' + r + ',' + g + ',' + b + ',' + box.box_opacity + ')';
    el.appendChild(bg);
  }
}

function teFitText(box) {
  var el = box.el;
  var span = el.querySelector('.te-text-span');
  if (!span) return;
  var boxW = el.offsetWidth || 200;
  var boxH = el.offsetHeight || 60;
  /* Use a hidden measurer so flex layout doesn't interfere */
  span.style.position = 'absolute';
  span.style.width = boxW + 'px';
  /* Binary search for largest font that fits */
  var lo = 6, hi = Math.min(boxW, boxH), best = lo;
  while (lo <= hi) {
    var mid = Math.floor((lo + hi) / 2);
    span.style.fontSize = mid + 'px';
    if (span.scrollHeight <= boxH) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  span.style.fontSize = best + 'px';
  span.style.position = '';
  span.style.width = '';
  box.fontsize = Math.round(best * 3); /* scale for 1080px output */
}

function teToggleBold() {
  document.getElementById('te-bold-btn').classList.toggle('active');
  teToolbarChanged();
}

function teToggleItalic() {
  document.getElementById('te-italic-btn').classList.toggle('active');
  teToolbarChanged();
}

function teEditBoxInline(box) {
  var el = box.el;
  var span = el.querySelector('.te-text-span');
  if (!span) return;
  span.contentEditable = 'true';
  span.focus();
  /* Select all text */
  var range = document.createRange();
  range.selectNodeContents(span);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  function finish() {
    span.contentEditable = 'false';
    box.text = span.textContent.trim() || 'Text';
    span.removeEventListener('blur', finish);
    span.removeEventListener('keydown', onKey);
    teRenderBox(box);
  }
  function onKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); finish(); }
    if (e.key === 'Escape') { span.textContent = box.text; finish(); }
  }
  span.addEventListener('blur', finish);
  span.addEventListener('keydown', onKey);
}

function teAddBox() {
  var bgOn = document.getElementById('te-bg-none-btn').classList.contains('active');
  var box = teCreateBox({
    text: '',
    fontsize: 42,
    fontcolor: document.getElementById('te-fontcolor').value,
    bold: document.getElementById('te-bold-btn').classList.contains('active'),
    italic: document.getElementById('te-italic-btn').classList.contains('active'),
    bgcolor: document.getElementById('te-bgcolor').value,
    box_opacity: bgOn ? 1 : 0,
    x_frac: 0.5,
    y_frac: 0.3 + Math.random() * 0.4,
  });
  teSelectBox(box);
  teEditBoxInline(box);
}

function teSetupBoxDrag(box) {
  var canvas = document.getElementById('te-canvas');
  var el = box.el;
  var resizeHandle = el.querySelector('.te-resize');
  var startX, startY, origLeft, origTop, origW, isResizing = false;

  /* Position drag — lock width */
  function onDown(e) {
    if (e.target.classList.contains('te-box-rm') || e.target.classList.contains('te-resize')) return;
    e.preventDefault();
    teState.dragging = true;
    isResizing = false;
    teSelectBox(box);
    var rect = canvas.getBoundingClientRect();
    var touch = e.touches ? e.touches[0] : e;
    startX = touch.clientX;
    startY = touch.clientY;
    origLeft = parseFloat(el.style.left) / 100 * rect.width;
    origTop = parseFloat(el.style.top) / 100 * rect.height;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchmove', onMove, {passive:false});
    document.addEventListener('touchend', onUp);
  }
  function onMove(e) {
    if (!teState.dragging) return;
    e.preventDefault();
    var touch = e.touches ? e.touches[0] : e;
    var rect = canvas.getBoundingClientRect();
    var dx = touch.clientX - startX;
    var dy = touch.clientY - startY;
    var newLeft = Math.max(0, Math.min(rect.width, origLeft + dx));
    var newTop = Math.max(0, Math.min(rect.height, origTop + dy));
    el.style.left = (newLeft / rect.width * 100) + '%';
    el.style.top = (newTop / rect.height * 100) + '%';
  }
  function onUp() {
    teState.dragging = false;
    box.x_frac = parseFloat(el.style.left) / 100;
    box.y_frac = parseFloat(el.style.top) / 100;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onUp);
  }

  el.addEventListener('mousedown', onDown);
  el.addEventListener('touchstart', onDown);

  /* Resize via corner handle — both width and height */
  var origH;
  function onResizeDown(e) {
    e.preventDefault();
    e.stopPropagation();
    isResizing = true;
    teSelectBox(box);
    var touch = e.touches ? e.touches[0] : e;
    startX = touch.clientX;
    startY = touch.clientY;
    origW = el.offsetWidth;
    origH = el.offsetHeight;
    document.addEventListener('mousemove', onResizeMove);
    document.addEventListener('mouseup', onResizeUp);
    document.addEventListener('touchmove', onResizeMove, {passive:false});
    document.addEventListener('touchend', onResizeUp);
  }
  function onResizeMove(e) {
    if (!isResizing) return;
    e.preventDefault();
    var touch = e.touches ? e.touches[0] : e;
    var dx = touch.clientX - startX;
    var dy = touch.clientY - startY;
    el.style.width = Math.max(40, origW + dx) + 'px';
    el.style.height = Math.max(24, origH + dy) + 'px';
    teFitText(box);
  }
  function onResizeUp() {
    isResizing = false;
    teFitText(box);
    document.removeEventListener('mousemove', onResizeMove);
    document.removeEventListener('mouseup', onResizeUp);
    document.removeEventListener('touchmove', onResizeMove);
    document.removeEventListener('touchend', onResizeUp);
  }
  resizeHandle.addEventListener('mousedown', onResizeDown);
  resizeHandle.addEventListener('touchstart', onResizeDown);
}

function applyTextEditor() {
  /* Collect all boxes with non-empty text */
  var validBoxes = teState.boxes.filter(function(b){return b.text.trim()});
  if (!validBoxes.length) { alert('Enter text in at least one box'); return; }

  var canvas = document.getElementById('te-canvas');
  var cW = canvas.offsetWidth || 360;
  var cH = canvas.offsetHeight || 640;
  var boxData = validBoxes.map(function(b) {
    var el = b.el;
    return {
      text: b.text.trim(), fontfamily: b.fontfamily, fontsize: b.fontsize, fontcolor: b.fontcolor,
      bold: b.bold, italic: b.italic, bgcolor: b.bgcolor, box_opacity: b.box_opacity,
      x_frac: Math.max(0.02, Math.min(0.98, b.x_frac)),
      y_frac: Math.max(0.02, Math.min(0.98, b.y_frac)),
      w_frac: (el ? el.offsetWidth : 200) / cW,
      h_frac: (el ? el.offsetHeight : 60) / cH,
    };
  });

  if (teState.editId !== null) {
    updateTextGroup(teState.editId, boxData, teState.startTime, teState.endTime);
  } else {
    addTextGroup(boxData, teState.startTime, teState.endTime);
  }
  closeTextEditor();
}

function closeTextEditor() {
  document.getElementById('text-editor-modal').classList.remove('active');
  var canvas = document.getElementById('te-canvas');
  canvas.innerHTML = '';
  canvas.style.backgroundImage = '';
  canvas.classList.remove('te-has-bg');
  teState = {editId: null, startTime: 0, endTime: 3, boxes: [], selectedBox: null, dragging: false};
}

/* ── Video-background preview ──────────────────────────────────────────
 *
 * When the user enables "Display Video Background" we paint the frame
 * that will be playing at the overlay's start time onto the canvas, so
 * they can see where the text sits relative to faces, captions, etc.
 * Finds the topmost video clip on the timeline at teState.startTime,
 * converts that to an in-scene offset, and asks /api/scene-frame for a
 * cached JPEG. No-op when no clip covers that moment.
 */
function teFindClipAtTime(t) {
  // Prefer the highest-numbered (front-most) track that has a clip
  // covering t — that's what will be visible at composite time.
  var best = null;
  for (var i = 0; i < videoItems.length; i++) {
    var v = videoItems[i];
    var s = v.startTime, e = s + v.duration;
    if (t >= s && t < e) {
      if (!best || (v.track || 0) > (best.track || 0)) best = v;
    }
  }
  return best;
}

function teRefreshVideoBg() {
  var cb = document.getElementById('te-show-bg');
  var canvas = document.getElementById('te-canvas');
  if (!cb || !canvas) return;
  if (!cb.checked) {
    canvas.style.backgroundImage = '';
    canvas.classList.remove('te-has-bg');
    return;
  }
  var vi = teFindClipAtTime(teState.startTime);
  if (!vi || vi.clip.id === undefined) {
    canvas.style.backgroundImage = '';
    canvas.classList.remove('te-has-bg');
    return;
  }
  // Offset within the scene = how far past the clip's start on the
  // timeline the overlay begins. The scene's own start_time inside the
  // source video is added server-side by /api/scene-frame.
  var offset = Math.max(0, teState.startTime - vi.startTime);
  var url = '/api/scene-frame/' + vi.clip.id + '?t=' + offset.toFixed(2);
  canvas.style.backgroundImage = "url('" + url + "')";
  canvas.classList.add('te-has-bg');
}

function teToggleVideoBg() {
  teRefreshVideoBg();
}

/* ── Text overlay preview ──────────────────────────────────────────────
 *
 * Plays the segment of the underlying source clip that this overlay
 * applies to, with the current text boxes rendered on top in the same
 * positions as the editor. Approximate (no transitions, no real burn-in
 * font rendering), but instant and good enough to check placement.
 */
var _tpEndHandler = null;
var _tpTimeHandler = null;

function openTextPreview() {
  var vi = teFindClipAtTime(teState.startTime);
  if (!vi || vi.clip.id === undefined) {
    alert('No timeline clip plays at this overlay’s start time. '
        + 'Move the overlay onto a clip first.');
    return;
  }
  // Translate the overlay's global timeline range into in-clip seconds.
  // Clamp to the clip's own bounds so we don't seek past its end.
  var clipDur = vi.duration;
  var inStart = Math.max(0, teState.startTime - vi.startTime);
  var inEnd   = Math.min(clipDur, teState.endTime - vi.startTime);
  if (inEnd <= inStart) inEnd = Math.min(clipDur, inStart + 1.0);

  // Mirror the canvas's text boxes into the preview overlay. Cloning
  // preserves all inline styles (position, size, font, color, bg).
  var srcCanvas = document.getElementById('te-canvas');
  var dst = document.getElementById('tp-overlays');
  dst.innerHTML = '';
  var srcBoxes = srcCanvas.querySelectorAll('.te-box');
  for (var i = 0; i < srcBoxes.length; i++) {
    var clone = srcBoxes[i].cloneNode(true);
    // Strip editor-only state from the clone.
    clone.classList.remove('te-selected');
    clone.removeAttribute('contenteditable');
    dst.appendChild(clone);
  }

  var info = document.getElementById('tp-info');
  info.textContent = (vi.clip.filename || 'clip')
    + '  ' + inStart.toFixed(1) + 's – ' + inEnd.toFixed(1) + 's';

  var video = document.getElementById('tp-video');
  // Pull the same single-clip stream the Scenes page uses — already
  // cached server-side so re-previewing is fast.
  video.src = '/rate/api/clip/' + vi.clip.id;
  video.muted = true;   // explicit so autoplay isn't blocked
  // Remove any handlers from a prior open before wiring fresh ones.
  if (_tpEndHandler)  video.removeEventListener('timeupdate', _tpEndHandler);
  if (_tpTimeHandler) video.removeEventListener('loadedmetadata', _tpTimeHandler);
  _tpTimeHandler = function() {
    try { video.currentTime = inStart; } catch (e) {}
    video.play().catch(function(){ /* autoplay denied; user clicks the controls */ });
  };
  _tpEndHandler = function() {
    if (video.currentTime >= inEnd - 0.05) {
      // Loop the segment so the user can study the placement instead
      // of having to re-open the modal each time.
      try { video.currentTime = inStart; } catch (e) {}
      video.play().catch(function(){});
    }
  };
  video.addEventListener('loadedmetadata', _tpTimeHandler);
  video.addEventListener('timeupdate', _tpEndHandler);

  document.getElementById('text-preview-modal').classList.add('active');
}

function closeTextPreview() {
  var video = document.getElementById('tp-video');
  if (video) {
    if (_tpEndHandler)  video.removeEventListener('timeupdate', _tpEndHandler);
    if (_tpTimeHandler) video.removeEventListener('loadedmetadata', _tpTimeHandler);
    _tpEndHandler = _tpTimeHandler = null;
    try { video.pause(); } catch (e) {}
    video.removeAttribute('src');
    video.load();
  }
  document.getElementById('tp-overlays').innerHTML = '';
  document.getElementById('text-preview-modal').classList.remove('active');
}

/* ── Text overlay presets (save / gallery) ─────────────────────────── */

function _teAllBoxesData() {
  // Serialize every non-empty box in the editor (preserves position, size,
  // colors, font, bold/italic, bg, opacity).
  var canvas = document.getElementById('te-canvas');
  var cW = canvas.offsetWidth || 360;
  var cH = canvas.offsetHeight || 640;
  return teState.boxes
    .filter(function(b){ return b && b.text && b.text.trim(); })
    .map(function(b) {
      var el = b.el;
      return {
        text: b.text.trim(),
        fontfamily: b.fontfamily, fontsize: b.fontsize, fontcolor: b.fontcolor,
        bold: !!b.bold, italic: !!b.italic, bgcolor: b.bgcolor,
        box_opacity: b.box_opacity,
        x_frac: Math.max(0.02, Math.min(0.98, b.x_frac)),
        y_frac: Math.max(0.02, Math.min(0.98, b.y_frac)),
        w_frac: (el ? el.offsetWidth : 200) / cW,
        h_frac: (el ? el.offsetHeight : 60) / cH,
      };
    });
}

async function saveTextOverlay() {
  var boxes = _teAllBoxesData();
  if (!boxes.length) {
    alert('Add some text before saving.');
    return;
  }
  var defaultName = boxes.map(function(b){return b.text}).join(' / ').slice(0, 40);
  var name = prompt('Name this overlay (optional):', defaultName);
  if (name === null) return;  // user cancelled
  var btn = document.querySelector('.te-save');
  if (btn) btn.disabled = true;
  try {
    var r = await fetch('/api/text-presets', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, boxes: boxes}),
    });
    var d = await r.json();
    if (!d.ok) throw new Error('Save failed');
    if (window.pgLog) {
      window.pgLog('[text-preset] saved "' + (name||'untitled') + '" — '
        + boxes.length + ' box' + (boxes.length === 1 ? '' : 'es'), 'ok');
    }
  } catch (e) {
    alert('Save failed: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openTextGallery() {
  document.getElementById('text-gallery-modal').classList.add('active');
  await _refreshTextGallery();
}

function closeTextGallery() {
  document.getElementById('text-gallery-modal').classList.remove('active');
}

async function _refreshTextGallery() {
  var grid = document.getElementById('tg-grid');
  var empty = document.getElementById('tg-empty');
  grid.innerHTML = '<div style="color:#666;padding:20px;text-align:center">Loading...</div>';
  try {
    var presets = await (await fetch('/api/text-presets')).json();
    if (!presets.length) {
      grid.innerHTML = '';
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';
    grid.innerHTML = '';
    presets.forEach(function(p) {
      var item = document.createElement('div');
      item.className = 'tg-item';
      var name = p.name || '(untitled)';
      var safeName = name.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      item.innerHTML = ''
        + '<img src="/api/text-presets/' + p.id + '/thumb" alt="' + safeName + '"/>'
        + '<div class="tg-name">' + safeName + '</div>'
        + '<button class="tg-del" title="Delete preset" data-pid="' + p.id + '">'
        +   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>'
        + '</button>';
      item.addEventListener('click', function(e) {
        if (e.target.closest('.tg-del')) return;
        loadTextPreset(p.id);
      });
      item.querySelector('.tg-del').addEventListener('click', function(e) {
        e.stopPropagation();
        deleteTextPreset(p.id, p.name);
      });
      grid.appendChild(item);
    });
  } catch (e) {
    grid.innerHTML = '<div style="color:#ef4444;padding:20px">Failed to load: ' + e.message + '</div>';
  }
}

async function loadTextPreset(pid) {
  try {
    var p = await (await fetch('/api/text-presets/' + pid)).json();
    // Accept both shapes: {boxes:[...]} (new) or {box:{...}} (legacy).
    var boxes = Array.isArray(p.boxes) ? p.boxes : (p.box ? [p.box] : []);
    if (!boxes.length) return;
    closeTextGallery();
    var canvas = document.getElementById('te-canvas');
    canvas.innerHTML = '';
    teState.boxes = [];
    teState.selectedBox = null;
    boxes.forEach(function(b){ teCreateBox(b); });
    if (teState.boxes.length) teSelectBox(teState.boxes[0]);
  } catch (e) {
    alert('Load failed: ' + e.message);
  }
}

async function deleteTextPreset(pid, name) {
  if (!confirm('Delete overlay "' + (name || 'untitled') + '"?')) return;
  try {
    await fetch('/api/text-presets/' + pid, {method:'DELETE'});
    await _refreshTextGallery();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

init();

// -- Scene preview playback --
function playScene(overlayEl, sceneId) {
  var card = overlayEl.closest('.clip-card');
  var existing = card ? card.querySelector('.scene-video') : null;
  if (existing) {
    /* Toggle pause/play on click */
    if (existing.paused) existing.play().catch(function(){});
    else existing.pause();
    return;
  }
  if (!card) return;
  var video = document.createElement('video');
  video.className = 'scene-video';
  video.src = '/rate/api/clip/' + sceneId;
  video.autoplay = true;
  video.loop = true;
  video.muted = false;
  video.playsInline = true;
  card.appendChild(video);
  overlayEl.style.display = 'none';
  video.play().catch(function(){});
  video.addEventListener('click', function(e) {
    e.stopPropagation();
    if (video.paused) video.play().catch(function(){});
    else video.pause();
  });
  function stopPreview() {
    card.removeEventListener('mouseleave', stopPreview);
    video.pause();
    video.src = '';
    video.remove();
    overlayEl.style.display = '';
  }
  card.addEventListener('mouseleave', stopPreview);
}

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
