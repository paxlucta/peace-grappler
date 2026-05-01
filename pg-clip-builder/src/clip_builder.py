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
    add_multiple_text_overlays, build_music_track, concatenate_clips,
    extract_subclip, extract_wide_subclip, find_asset, generate_placeholder,
    get_video_duration, has_audio_stream, is_wide_video, normalize_clip,
    overlay_music, overlay_music_track, pad_clip_to_duration,
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
            clip_data["wide"] = item.get("wide", False)
            clip_data["stack_order"] = item.get("stack_order", 0)
            clip_data["trans_in"] = item.get("trans_in")
            clip_data["trans_out"] = item.get("trans_out")
            clips.append(clip_data)

    if not clips:
        return jsonify({"error": "No valid clips in video track"}), 400

    # Sort by start_time
    clips.sort(key=lambda c: (c["start_time"], c["stack_order"]))

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
        # Build time segments and fill gaps with black placeholders
        segments = _build_time_segments(clips)

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

            # Empty segment = gap → black placeholder
            if not seg_clips:
                gap_dur = seg["end"] - seg["start"]
                gap_path = os.path.join(tmp, f"gap{si:03d}.mp4")
                if generate_placeholder(gap_path, gap_dur, "black"):
                    clip_paths.append(gap_path)
                    if len(clip_paths) > 1:
                        all_transitions.append(None)
                continue

            wide_clips = [c for c in seg_clips if c["wide"]]
            non_wide_clips = [c for c in seg_clips if not c["wide"]]

            if len(wide_clips) >= 2:
                # Stack wide videos vertically, padding each to segment duration
                seg_start = seg["start"]
                seg_dur = seg["end"] - seg_start
                wide_clips.sort(key=lambda c: c["stack_order"])
                wide_paths = []
                for wi, wc in enumerate(wide_clips):
                    raw = os.path.join(tmp, f"seg{si:03d}_wraw{wi}.mp4")
                    if not extract_wide_subclip(wc["video_file"], wc["start"],
                                                wc["duration"], raw):
                        continue
                    # Pad to align within the segment
                    offset = wc["start_time"] - seg_start
                    if offset > 0.05 or (wc["start_time"] + wc["duration"]) < seg["end"] - 0.05:
                        padded = os.path.join(tmp, f"seg{si:03d}_wpad{wi}.mp4")
                        if pad_clip_to_duration(raw, offset, seg_dur, padded):
                            wide_paths.append(padded)
                        else:
                            wide_paths.append(raw)
                    else:
                        wide_paths.append(raw)
                if wide_paths:
                    stacked = os.path.join(tmp, f"seg{si:03d}_stacked.mp4")
                    if stack_wide_videos(wide_paths, stacked):
                        clip_paths.append(stacked)
                        trans = wide_clips[0].get("trans_in")
                        if len(clip_paths) > 1:
                            all_transitions.append(trans)
            elif len(wide_clips) == 1:
                wc = wide_clips[0]
                wp = os.path.join(tmp, f"seg{si:03d}_clip.mp4")
                if extract_subclip(wc["video_file"], wc["start"],
                                   wc["duration"], wp):
                    clip_paths.append(wp)
                    if len(clip_paths) > 1:
                        all_transitions.append(wc.get("trans_in"))

            for ni, nc in enumerate(non_wide_clips):
                np = os.path.join(tmp, f"seg{si:03d}_nw{ni}.mp4")
                if extract_subclip(nc["video_file"], nc["start"],
                                   nc["duration"], np):
                    clip_paths.append(np)
                    if len(clip_paths) > 1:
                        all_transitions.append(nc.get("trans_in"))

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

    # Save to DB
    save_timeline = {
        "video_track": video_track,
        "sound_track": sound_track,
        "text_overlays": text_overlays,
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
  grid-template-columns:50px 1fr;
  grid-template-rows:20px 50px 40px minmax(70px, auto);
  min-width:100%;
}
/* Time ruler */
#time-ruler{
  grid-column:1/-1;height:20px;position:sticky;top:0;
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
  display:flex;align-items:center;justify-content:center;
  padding:0 4px;user-select:none;
}
/* Tracks */
.track{
  position:relative;border-bottom:1px solid #1a1a1a;
  background:#0d0d0d;min-height:40px;
}
#video-track{min-height:70px;border-bottom:none;overflow:hidden}
#sound-track{background:#0c0e0c;z-index:2}
#text-track{background:#0c0c0e;z-index:2}
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
#vt-insert-bar{
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
.vblock .blk-label{right:24px}
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

/* Sound blocks */
.sblock{border:1px solid rgba(255,255,255,.15);color:#fff}
.sblock .blk-vol{
  display:flex;gap:2px;align-items:flex-end;height:18px;
  margin-left:auto;flex-shrink:0;
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

#clear-btn{
  background:none;color:#666;border:1px solid #444;padding:3px 10px;
  font-size:11px;border-radius:4px;transition:color .15s,border-color .15s;
}
#clear-btn:hover{color:#e53935;border-color:#e53935}

.controls{display:flex;align-items:center;gap:12px;margin-top:6px}
.tl-check{font-size:12px;color:#888;cursor:pointer;display:flex;align-items:center;gap:4px;user-select:none}
.tl-check input{accent-color:#e53935;cursor:pointer}
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
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/wizard">AI Wizard</a>
    <a href="/builder" class="active">Builder</a>
    <a href="/library">Library</a>
    <a href="/rate">Scenes</a>
    <a href="/analyze">Analyze</a>
  </nav>
</header>

<div class="sub-toolbar">
  <select id="tag-filter" onchange="filterByTag()">
    <option value="">All Tags</option>
  </select>
  <span id="clip-count"></span>
</div>

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
  <div id="multi-timeline">
    <div id="tl-grid">
      <div id="time-ruler"></div>
      <div class="track-label">SND</div>
      <div id="sound-track" class="track"><span class="track-empty">click to add music</span></div>
      <div class="track-label">TXT</div>
      <div id="text-track" class="track"><span class="track-empty">click to add text overlay</span></div>
      <div class="track-label">VID</div>
      <div id="video-track" class="track"><span class="track-empty">drag clips here</span><div id="vt-insert-bar"></div></div>
    </div>
  </div>
  <div class="controls">
    <label class="tl-check"><input type="checkbox" id="include-intro" checked/> Include Intro Video</label>
    <label class="tl-check"><input type="checkbox" id="include-outro" checked/> Include Outro Video</label>
    <button id="gen-btn" onclick="generateVideo()">Generate Video</button>
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
      <label class="te-label">Text</label>
      <input type="color" id="te-fontcolor" value="#ffffff"/>
      <div class="te-sep"></div>
      <button id="te-bold-btn" class="te-btn" onclick="teToggleBold()"><b>B</b></button>
      <button id="te-italic-btn" class="te-btn" onclick="teToggleItalic()"><i>I</i></button>
      <div class="te-sep"></div>
      <label class="te-label">BG</label>
      <input type="color" id="te-bgcolor" value="#000000"/>
      <button id="te-bg-none-btn" class="te-btn" onclick="teToggleBg()" title="Toggle background">&#8416;</button>
    </div>
    <div class="te-canvas-wrap">
      <div id="te-canvas"></div>
    </div>
    <div class="te-footer">
      <button class="te-cancel" onclick="closeTextEditor()">Cancel</button>
      <button class="te-apply" onclick="applyTextEditor()">Apply</button>
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
  setupTracks();

  /* Auto-load timeline from ?load=filename query param */
  var params = new URLSearchParams(window.location.search);
  var loadFile = params.get('load');
  if (loadFile) {
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
  }
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
  ruler.style.width = (50 + w) + 'px';
  for (var s = 0; s <= Math.ceil(dur); s++) {
    var tick = document.createElement('div');
    tick.className = 'ruler-tick' + (s % 5 === 0 ? ' major' : '');
    tick.style.left = (50 + s * CELL_W) + 'px';
    ruler.appendChild(tick);
    if (s % 5 === 0) {
      var lbl = document.createElement('span');
      lbl.className = 'ruler-label';
      lbl.style.left = (50 + s * CELL_W) + 'px';
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
  var track = document.getElementById('video-track');
  track.querySelectorAll('.vblock,.trans-marker').forEach(function(e){e.remove()});
  var dur = Math.max(getVideoDuration(), 10);
  track.style.width = (dur * CELL_W) + 'px';
  var numRows = computeVideoRows();

  /* Adjust track height for stacked rows */
  var baseH = 70;
  track.style.minHeight = (numRows > 1 ? numRows * 50 : baseH) + 'px';

  for (var i = 0; i < videoItems.length; i++) {
    var vi = videoItems[i];
    /* clip block */
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
    var vol = vi.volume !== undefined ? vi.volume : 5;
    var vBars = '';
    var vH = [3,5,7,9,12];
    for (var vv = 0; vv < 5; vv++) {
      vBars += '<div class="vv' + (vv < vol ? ' active' : '') + '" data-lv="' + (vv+1) + '" data-vidx="' + i + '"'
        + ' style="height:' + vH[vv] + 'px" onclick="event.stopPropagation();setVideoVol(this)"></div>';
    }
    var transIn = vi.trans_in || '';
    var transOut = vi.trans_out || '';
    blk.innerHTML = (thumbUrl ? '<img class="vblock-thumb" src="' + thumbUrl + '"/>' : '')
      + '<span class="blk-label">' + vi.duration.toFixed(1) + 's</span>'
      + '<div class="vblk-vol">' + vBars + '</div>'
      + (vi.clip.wide ? '<span class="vblock-wide"></span>' : '')
      + '<span class="vtrans vt-in" data-vidx="' + i + '" data-dir="in">' + (transIn || '+') + '</span>'
      + '<span class="vtrans vt-out" data-vidx="' + i + '" data-dir="out">' + (transOut || '+') + '</span>'
      + '<button class="blk-rm" onclick="event.stopPropagation();removeVideoItem(' + i + ')">&times;</button>';
    blk.dataset.idx = i;
    vi._el = blk;
    /* Transition pill clicks */
    blk.querySelectorAll('.vtrans').forEach(function(pill) {
      pill.addEventListener('click', function(e) {
        e.stopPropagation();
        openVideoTransPicker(parseInt(this.dataset.vidx), this.dataset.dir);
      });
    });
    makeVideoDraggable(blk, vi, i);
    track.appendChild(blk);
  }
  /* update empty label */
  var emp = track.querySelector('.track-empty');
  if (videoItems.length && emp) emp.style.display = 'none';
  else if (!videoItems.length && emp) emp.style.display = '';
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
      + '<button class="blk-rm" style="position:relative;flex-shrink:0" onclick="event.stopPropagation();removeSoundItem(' + si.id + ')">&times;</button>'
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
      /* Vertical drag for wide videos to reorder stack */
      if (data.clip.wide) {
        el.style.top = (startTopPx + (e2.clientY - startY)) + 'px';
      }
    }
    function onUp(e2) {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (!moved) return;
      var newStart = Math.max(0, parseFloat(el.style.left) / CELL_W);
      /* Snap to 0.5s grid */
      newStart = Math.round(newStart * 2) / 2;
      var vidIdx = videoItems.indexOf(data);
      data.startTime = newStart;
      /* Handle vertical reorder for wide videos */
      if (data.clip.wide && vidIdx >= 0) {
        reorderWideStack(vidIdx, e2.clientY - startY);
      }
      if (vidIdx >= 0) resolveVideoOverlaps(vidIdx);
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
  if (dropped.clip.wide) {
    /* Wide video dropped: just sweep non-wide videos to avoid overlapping it */
    sweepNonWideOverlaps();
    return;
  }

  var dropStart = dropped.startTime;
  var dropEnd = dropStart + dropped.duration;

  /* Find the leftmost video that overlaps with the dropped non-wide */
  var overlapped = null;
  for (var i = 0; i < videoItems.length; i++) {
    if (i === droppedIdx) continue;
    var vi = videoItems[i];
    var viEnd = vi.startTime + vi.duration;
    if (vi.startTime < dropEnd && viEnd > dropStart) {
      if (!overlapped || vi.startTime < overlapped.startTime) {
        overlapped = vi;
      }
    }
  }

  if (overlapped) {
    /* Collect: overlapped video + all non-wide videos to its left, excluding the dropped */
    var toPush = [];
    for (var i = 0; i < videoItems.length; i++) {
      if (i === droppedIdx) continue;
      if (!videoItems[i].clip.wide && videoItems[i].startTime <= overlapped.startTime) {
        toPush.push(videoItems[i]);
      }
    }
    /* Sort by startTime to maintain relative order */
    toPush.sort(function(a, b) { return a.startTime - b.startTime; });

    /* Place sequentially after dropped video */
    var cursor = dropEnd;
    for (var j = 0; j < toPush.length; j++) {
      toPush[j].startTime = cursor;
      cursor += toPush[j].duration;
    }
  }

  /* Sweep: ensure no non-wide video overlaps with ANY other video */
  sweepNonWideOverlaps();
}

function sweepNonWideOverlaps() {
  /* Ensure no non-wide video overlaps with any other video (wide or not).
     Wide videos can overlap with each other, but non-wide cannot overlap anything. */
  var nonWide = videoItems.filter(function(v) { return !v.clip.wide; });
  nonWide.sort(function(a, b) { return a.startTime - b.startTime; });

  for (var i = 0; i < nonWide.length; i++) {
    var nw = nonWide[i];
    var nwStart = nw.startTime;
    var nwEnd = nwStart + nw.duration;

    /* Check against ALL other videos */
    var maxEnd = nwStart; /* earliest safe start = current position */
    for (var j = 0; j < videoItems.length; j++) {
      if (videoItems[j] === nw) continue;
      var vs = videoItems[j].startTime;
      var ve = vs + videoItems[j].duration;
      /* If this other video overlaps with our non-wide video, push right */
      if (vs < nwEnd && ve > nwStart) {
        maxEnd = Math.max(maxEnd, ve);
      }
    }
    if (maxEnd > nwStart) {
      nw.startTime = maxEnd;
      nwEnd = maxEnd + nw.duration;
    }
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

function addVideoItem(clip, startTime) {
  /* Prevent adding the same scene twice */
  if (clip.id !== undefined && getTlClipIds().indexOf(clip.id) >= 0) return;
  var dur = clip.duration;
  var st = startTime !== undefined ? startTime : getVideoDuration();
  videoItems.push({
    clip: {id: clip.id, video_file: clip.video_file, start: clip.start, end: clip.end, filename: clip.filename, wide: clip.wide},
    duration: dur,
    startTime: st,
    trans_in: selectedTransition,
    trans_out: null,
    volume: 5,
    stackOrder: 0,
  });
  resolveVideoOverlaps(videoItems.length - 1);
  syncTl();
}

function removeVideoItem(idx) {
  videoItems.splice(idx, 1);
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
  /* Video track: accept clips from grid via SortableJS (drop only, no reorder) */
  var vt = document.getElementById('video-track');
  new Sortable(vt, {
    group: {name:'timeline', pull:false, put:function(to, from, el) {
      return el.classList.contains('clip-card') && !el.classList.contains('in-tl') && !el.classList.contains('ignored');
    }},
    sort: false,
    draggable: '.no-sort-dummy',
    animation: 0,
    onAdd: function(evt) {
      var el = evt.item;
      var id = parseInt(el.dataset.id);
      var clip = allClips.find(function(c){return c.id===id});
      el.remove();
      if (clip) {
        var startTime = Math.max(0, Math.round(lastInsertX / CELL_W * 2) / 2);
        addVideoItem(clip, startTime);
      }
    },
  });

  /* Insertion bar for video track */
  var insertBar = document.getElementById('vt-insert-bar');
  vt.addEventListener('dragover', function(e) {
    var rect = vt.getBoundingClientRect();
    var mouseX = e.clientX - rect.left + vt.parentElement.parentElement.scrollLeft;
    /* Snap to 0.5s grid */
    var snapped = Math.max(0, Math.round(mouseX / (CELL_W / 2)) * (CELL_W / 2));
    lastInsertX = snapped;
    insertBar.style.left = snapped + 'px';
    insertBar.style.display = 'block';
  });
  vt.addEventListener('dragleave', function(e) {
    if (!vt.contains(e.relatedTarget)) insertBar.style.display = 'none';
  });
  vt.addEventListener('drop', function() { insertBar.style.display = 'none'; });
  document.addEventListener('dragend', function() { insertBar.style.display = 'none'; });

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
  return '<div class="' + cls + '" data-id="' + c.id + '" data-tc="' + (c.thumb_count||1) + '" oncontextmenu="showCtx(event,' + c.id + ')">'
    + '<img class="thumb" src="/api/thumbnail/' + c.id + '" loading="lazy"/>'
    + '<div class="play-overlay" onclick="event.stopPropagation();playScene(this,' + c.id + ')"><div class="play-circle"><svg viewBox="0 0 24 24"><polygon points="8,5 19,12 8,19"/></svg></div></div>'
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
  if (!videoItems.length && !soundItems.length && !textItems.length) return;
  if (!window.confirm('Clear the entire timeline?')) return;
  videoItems = []; soundItems = []; textItems = [];
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

function buildTimeline() {
  /* Sort by startTime for generation order */
  var sorted = videoItems.slice().sort(function(a,b){return a.startTime - b.startTime});
  var vt = [];
  for (var i = 0; i < sorted.length; i++) {
    var vi = sorted[i];
    var c = vi.clip;
    var entry = {type:'clip', start_time:vi.startTime, wide:!!c.wide, stack_order:vi.stackOrder||0, volume:vi.volume||5,
      trans_in:vi.trans_in||null, trans_out:vi.trans_out||null};
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
      if (bx.x_frac !== undefined) { o.x_frac = bx.x_frac; o.y_frac = bx.y_frac; }
      if (bx.w_frac) { o.w_frac = bx.w_frac; o.h_frac = bx.h_frac; }
      if (bx.bold) o.bold = true;
      if (bx.italic) o.italic = true;
      if (bx.bgcolor) o.bgcolor = bx.bgcolor;
      tt.push(o);
    }
  }
  return {video_track:vt, sound_track:st, text_overlays:tt,
    include_intro: document.getElementById('include-intro').checked,
    include_outro: document.getElementById('include-outro').checked};
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
    videoItems.push({
      clip: clip ? {id:clip.id, video_file:clip.video_file, start:clip.start, end:clip.end, filename:clip.filename, wide:clip.wide}
                  : {video_file:item.video_file, start:item.start, end:item.end, filename:(item.video_file||'').split('/').pop()},
      duration: dur,
      startTime: item.start_time !== undefined ? item.start_time : getVideoDuration(),
      trans_in: item.trans_in || (videoItems.length > 0 ? (pendTrans || null) : null),
      trans_out: item.trans_out || null,
      volume: item.volume || 5,
      stackOrder: item.stack_order || 0,
    });
    pendTrans = null;
  }
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
}

function teCreateBox(opts) {
  var box = {
    id: ++teNextBoxId,
    text: opts.text || '',
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
      text: b.text.trim(), fontsize: b.fontsize, fontcolor: b.fontcolor,
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
  document.getElementById('te-canvas').innerHTML = '';
  teState = {editId: null, startTime: 0, endTime: 3, boxes: [], selectedBox: null, dragging: false};
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
