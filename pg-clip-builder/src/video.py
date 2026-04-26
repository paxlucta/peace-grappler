"""video.py — Shared FFmpeg functions for PeaceGrappler."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
ASSETS_DIR = ROOT_DIR / "assets"
OUTPUT_DIR = ROOT_DIR / "output"
THUMB_DIR = ROOT_DIR / ".cache" / "thumbnails"
VIDEO_DIR = ROOT_DIR / "videos"
XFADE_DUR = 0.7

TRANSITIONS = [
    "fade", "fadeblack", "fadewhite",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright",
    "circlecrop", "circleopen", "circleclose",
    "radial", "dissolve",
    "smoothleft", "smoothright",
    "diagtl", "diagbr",
    "horzopen", "horzclose", "vertopen", "vertclose",
    "hlslice", "hrslice",
    "zoomin",
    "coverleft", "coverright",
    "revealleft", "revealright",
    "pixelize",
]

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}


def has_audio_stream(path):
    """Check if a file has at least one audio stream."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def get_video_duration(path):
    """Get video duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def get_video_dimensions(path):
    """Return (width, height) or (0, 0) on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split(",")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0


def is_wide_video(path):
    """Return True if video is landscape (width > height)."""
    w, h = get_video_dimensions(path)
    return w > h if w > 0 and h > 0 else False


def find_asset(prefix, extensions=None):
    """Find an asset file by prefix (e.g. 'intro' -> assets/videos/intro.mp4)."""
    if extensions is None:
        extensions = VIDEO_EXTENSIONS
    asset_vid_dir = ASSETS_DIR / "videos"
    if not asset_vid_dir.exists():
        return None
    for f in asset_vid_dir.iterdir():
        if f.stem.lower().startswith(prefix) and f.suffix.lower() in extensions:
            return f
    return None


def extract_subclip(video_path, start, duration, out_path):
    """Extract a sub-clip, normalize to 1080x1920@30fps, guarantee audio."""
    _has_audio = has_audio_stream(video_path)
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
          "setsar=1,fps=30")

    if _has_audio:
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
               "-t", f"{duration:.2f}", "-vf", vf,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-filter_complex", f"[0:v]{vf}[vout]",
               "-map", "[vout]", "-map", "1:a",
               "-t", f"{duration:.2f}",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def extract_wide_subclip(video_path, start, duration, out_path):
    """Extract a subclip scaled for wide (landscape) display without portrait padding."""
    _has_audio = has_audio_stream(video_path)
    vf = "scale=1080:-2,setsar=1,fps=30"

    if _has_audio:
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
               "-t", f"{duration:.2f}", "-vf", vf,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(video_path),
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-filter_complex", f"[0:v]{vf}[vout]",
               "-map", "[vout]", "-map", "1:a",
               "-t", f"{duration:.2f}",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def normalize_clip(in_path, out_path):
    """Normalize a video clip to 1080x1920 @ 30fps with guaranteed audio."""
    dur = get_video_duration(str(in_path))
    _has_audio = has_audio_stream(in_path)
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
          "setsar=1,fps=30")

    if _has_audio:
        cmd = ["ffmpeg", "-y", "-i", str(in_path), "-vf", vf,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(in_path),
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-filter_complex", f"[0:v]{vf}[vout]",
               "-map", "[vout]", "-map", "1:a",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
               "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}",
               "-movflags", "+faststart", out_path]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def generate_placeholder(out_path, duration, color="black"):
    """Generate a solid color video at 1080x608 resolution."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", f"color=c={color}:s=1080x608:d={duration:.2f}:r=30",
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
             "-t", f"{duration:.2f}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def concatenate_clips(clip_paths, out_path, transitions=None, xfade_dur=XFADE_DUR):
    """Concatenate clips with per-pair transitions or hard cuts.

    transitions: list of N-1 entries, each a transition name string or None
                 (hard cut).  If the whole list is None, all gaps are hard cuts.
    """
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], out_path)
        return True

    n = len(clip_paths)
    if transitions is None:
        transitions = [None] * (n - 1)

    # All hard cuts -> simple concat
    if all(t is None for t in transitions):
        return _concat_fallback(clip_paths, out_path)

    # All transitions -> single-pass xfade
    if all(t is not None for t in transitions):
        if _xfade_all(clip_paths, transitions, out_path, xfade_dur):
            return True
        return _concat_fallback(clip_paths, out_path)

    # Mixed: group consecutive clips that share transitions
    groups = []
    cur_clips = [clip_paths[0]]
    cur_trans = []
    for i in range(1, n):
        if transitions[i - 1] is not None:
            cur_clips.append(clip_paths[i])
            cur_trans.append(transitions[i - 1])
        else:
            groups.append((cur_clips, cur_trans))
            cur_clips = [clip_paths[i]]
            cur_trans = []
    groups.append((cur_clips, cur_trans))

    tmp_dir = os.path.dirname(out_path)
    group_outputs = []
    for gi, (g_clips, g_trans) in enumerate(groups):
        if len(g_clips) == 1:
            group_outputs.append(g_clips[0])
        else:
            g_out = os.path.join(tmp_dir, f"_grp_{gi}.mp4")
            if _xfade_all(g_clips, g_trans, g_out, xfade_dur):
                group_outputs.append(g_out)
            elif _concat_fallback(g_clips, g_out):
                group_outputs.append(g_out)
            else:
                return False

    if len(group_outputs) == 1:
        shutil.copy2(group_outputs[0], out_path)
        return True
    return _concat_fallback(group_outputs, out_path)


def _xfade_all(clip_paths, transitions, out_path, xfade_dur):
    """Apply xfade transitions to a sequence of clips."""
    durations = []
    for cp in clip_paths:
        d = get_video_duration(cp)
        if d <= 0:
            return False
        durations.append(d)

    min_dur = min(durations)
    actual_xfade = min(xfade_dur, min_dur * 0.4)
    if actual_xfade < 0.1:
        return False

    n = len(clip_paths)
    inputs = []
    for cp in clip_paths:
        inputs.extend(["-i", cp])

    vfilters = []
    afilters = []
    offset = durations[0] - actual_xfade
    prev_v = "[0:v]"
    prev_a = "[0:a]"

    for i in range(1, n):
        out_v = f"[v{i}]" if i < n - 1 else "[vout]"
        out_a = f"[a{i}]" if i < n - 1 else "[aout]"
        transition = transitions[i - 1]
        vfilters.append(
            f"{prev_v}[{i}:v]xfade=transition={transition}"
            f":duration={actual_xfade}:offset={offset:.3f}{out_v}"
        )
        afilters.append(
            f"{prev_a}[{i}:a]acrossfade=d={actual_xfade}{out_a}"
        )
        offset += durations[i] - actual_xfade
        prev_v = out_v
        prev_a = out_a

    filter_complex = ";".join(vfilters + afilters)

    try:
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs +
            ["-filter_complex", filter_complex,
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and os.path.exists(out_path):
            return True
    except Exception:
        pass
    return False


def _concat_fallback(clip_paths, out_path):
    """Simple concat using ffmpeg concat demuxer."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")
        list_file = f.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file,
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    finally:
        os.unlink(list_file)


def overlay_music(video_path, music_path, out_path):
    """Mix background music under the video's original audio."""
    video_dur = get_video_duration(str(video_path))
    if video_dur <= 0:
        return False

    fade_out_start = max(0, video_dur - 2.0)
    vid_has_audio = has_audio_stream(video_path)

    try:
        if vid_has_audio:
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-i", str(video_path),
                 "-stream_loop", "-1", "-i", str(music_path),
                 "-filter_complex",
                 f"[1:a]volume=0.18,afade=t=out:st={fade_out_start:.2f}:d=2.0[music];"
                 f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                 "-map", "0:v", "-map", "[aout]",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k",
                 "-shortest", "-movflags", "+faststart", out_path],
                capture_output=True, text=True, timeout=120,
            )
        else:
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-i", str(video_path),
                 "-stream_loop", "-1", "-i", str(music_path),
                 "-filter_complex",
                 f"[1:a]volume=0.25,afade=t=out:st={fade_out_start:.2f}:d=2.0[aout]",
                 "-map", "0:v", "-map", "[aout]",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k",
                 "-t", f"{video_dur:.2f}",
                 "-movflags", "+faststart", out_path],
                capture_output=True, text=True, timeout=120,
            )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def build_music_track(segments, total_dur, out_path):
    """Build a continuous audio track from segments.
    Each segment: {"start": float, "duration": float, "music": path_or_None,
                   "volume": 0-5}
    """
    if not segments:
        return False

    inputs = []
    afilters = []
    idx = 0

    for seg in segments:
        dur = seg["duration"]
        if dur <= 0:
            continue
        vol_level = seg.get("volume", 3)
        music_vol = vol_level / 5.0 * 0.7
        if seg["music"] and music_vol > 0:
            inputs.extend(["-stream_loop", "-1", "-i", str(seg["music"])])
            afilters.append(
                f"[{idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
                f"volume={music_vol:.3f}[s{idx}]"
            )
            idx += 1
        else:
            inputs.extend(["-f", "lavfi", "-i",
                           f"anullsrc=r=44100:cl=stereo:d={dur:.3f}"])
            afilters.append(f"[{idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[s{idx}]")
            idx += 1

    if idx == 0:
        return False
    if idx == 1:
        afilters.append("[s0]asetpts=PTS-STARTPTS[aout]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(idx))
        afilters.append(f"{concat_in}concat=n={idx}:v=0:a=1[aout]")

    fade_start = max(0, total_dur - 2.0)
    afilters.append(
        f"[aout]afade=t=out:st={fade_start:.2f}:d=2.0[final]"
    )

    filter_str = ";".join(afilters)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs +
            ["-filter_complex", filter_str,
             "-map", "[final]",
             "-c:a", "aac", "-b:a", "192k", out_path],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def overlay_music_track(video_path, music_track_path, out_path, segments=None):
    """Overlay a pre-built music audio track onto a video.
    segments (optional): used to attenuate original audio per-segment.
    """
    vid_has_audio = has_audio_stream(video_path)
    try:
        if vid_has_audio:
            # Build original audio volume expression from segments
            orig_vol_expr = "1.0"
            if segments:
                parts = []
                for seg in segments:
                    vol_level = seg.get("volume", 3)
                    orig_vol = 1.0 - vol_level / 5.0
                    t0 = seg["start"]
                    t1 = t0 + seg["duration"]
                    parts.append(
                        f"between(t\\,{t0:.3f}\\,{t1:.3f})*{orig_vol:.3f}"
                    )
                if parts:
                    orig_vol_expr = "+".join(parts)

            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-i", str(video_path),
                 "-i", str(music_track_path),
                 "-filter_complex",
                 f"[0:a]volume='{orig_vol_expr}':eval=frame[orig];"
                 f"[orig][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                 "-map", "0:v", "-map", "[aout]",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k",
                 "-shortest", "-movflags", "+faststart", out_path],
                capture_output=True, text=True, timeout=120,
            )
        else:
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-i", str(video_path),
                 "-i", str(music_track_path),
                 "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k",
                 "-shortest", "-movflags", "+faststart", out_path],
                capture_output=True, text=True, timeout=120,
            )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def stack_split_videos(top_path, bottom_path, out_path):
    """Stack two videos vertically in 1080x1920 frame.

    Each video gets half the frame (960px), scaled with
    force_original_aspect_ratio=decrease and padded/centered with black.
    Uses vstack. If one is shorter, uses tpad=stop_mode=clone to freeze
    its last frame. Mixes audio with amix.
    """
    top_dur = get_video_duration(top_path)
    bottom_dur = get_video_duration(bottom_path)
    max_dur = max(top_dur, bottom_dur)

    top_pad = ""
    bottom_pad = ""
    if top_dur < max_dur - 0.05:
        top_pad = f",tpad=stop_mode=clone:stop_duration={max_dur - top_dur:.3f}"
    if bottom_dur < max_dur - 0.05:
        bottom_pad = f",tpad=stop_mode=clone:stop_duration={max_dur - bottom_dur:.3f}"

    filter_complex = (
        f"[0:v]scale=1080:960:force_original_aspect_ratio=decrease,"
        f"pad=1080:960:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30{top_pad}[top];"
        f"[1:v]scale=1080:960:force_original_aspect_ratio=decrease,"
        f"pad=1080:960:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30{bottom_pad}[bot];"
        f"[top][bot]vstack=inputs=2[vout];"
        f"[0:a][1:a]amix=inputs=2:duration=longest[aout]"
    )

    try:
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-i", top_path, "-i", bottom_path,
             "-filter_complex", filter_complex,
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-c:a", "aac", "-b:a", "192k",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def process_track(items, tmp_dir, prefix, resolve_clip_fn):
    """Process a list of timeline items (clips, transitions, mute, placeholders)
    into a single video.

    resolve_clip_fn: callback that takes an item dict and returns a clip data dict
                     with keys: video_file, start, end, duration
                     (or None if not found)

    Returns (video_path, duration) or (None, 0) on failure.
    """
    clip_paths = []
    transitions = []
    muted = False
    idx = 0

    for item in items:
        itype = item.get("type", "")
        if itype == "mute":
            muted = True
        elif itype == "unmute":
            muted = False
        elif itype == "transition":
            if clip_paths:
                transitions.append(item.get("name", "fade"))
        elif itype == "placeholder":
            dur = item.get("duration", 5)
            color = item.get("color", "black")
            out = os.path.join(tmp_dir, f"{prefix}_ph_{idx:03d}.mp4")
            if generate_placeholder(out, dur, color):
                if muted:
                    muted_out = os.path.join(tmp_dir, f"{prefix}_ph_{idx:03d}_m.mp4")
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", out,
                         "-c:v", "copy", "-an",
                         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                         "-c:a", "aac", "-shortest", muted_out],
                        capture_output=True, timeout=30,
                    )
                    if os.path.exists(muted_out):
                        clip_paths.append(muted_out)
                    else:
                        clip_paths.append(out)
                else:
                    clip_paths.append(out)
                # Pad transitions list
                while len(transitions) < len(clip_paths) - 1:
                    transitions.append(None)
                idx += 1
        elif itype == "clip":
            clip_data = resolve_clip_fn(item)
            if clip_data:
                out = os.path.join(tmp_dir, f"{prefix}_clip_{idx:03d}.mp4")
                if extract_wide_subclip(clip_data["video_file"], clip_data["start"],
                                        clip_data["duration"], out):
                    if muted:
                        muted_out = os.path.join(tmp_dir, f"{prefix}_clip_{idx:03d}_m.mp4")
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", out,
                             "-c:v", "copy", "-an",
                             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-c:a", "aac", "-shortest", muted_out],
                            capture_output=True, timeout=30,
                        )
                        if os.path.exists(muted_out):
                            clip_paths.append(muted_out)
                        else:
                            clip_paths.append(out)
                    else:
                        clip_paths.append(out)
                    # Pad transitions list
                    while len(transitions) < len(clip_paths) - 1:
                        transitions.append(None)
                    idx += 1

    if not clip_paths:
        return (None, 0)

    if len(clip_paths) == 1:
        total_dur = get_video_duration(clip_paths[0])
        return (clip_paths[0], total_dur)

    out_path = os.path.join(tmp_dir, f"{prefix}_joined.mp4")
    # Trim transitions to correct length
    transitions = transitions[:len(clip_paths) - 1]
    if concatenate_clips(clip_paths, out_path, transitions):
        total_dur = get_video_duration(out_path)
        return (out_path, total_dur)

    return (None, 0)


def process_split_section(top_items, bottom_items, tmp_dir, prefix, out_path,
                          resolve_clip_fn):
    """Process both tracks of a split section and stack them."""
    top_path, top_dur = process_track(top_items, tmp_dir, f"{prefix}_top",
                                      resolve_clip_fn)
    bottom_path, bottom_dur = process_track(bottom_items, tmp_dir, f"{prefix}_bot",
                                            resolve_clip_fn)

    if not top_path and not bottom_path:
        return False

    if top_path and not bottom_path:
        ph_path = os.path.join(tmp_dir, f"{prefix}_bot_ph.mp4")
        generate_placeholder(ph_path, top_dur, "black")
        bottom_path = ph_path

    if bottom_path and not top_path:
        ph_path = os.path.join(tmp_dir, f"{prefix}_top_ph.mp4")
        generate_placeholder(ph_path, bottom_dur, "black")
        top_path = ph_path

    return stack_split_videos(top_path, bottom_path, out_path)


def extract_wide_split(video_path, start, duration, out_path):
    """Extract a wide scene as split-screen (top + bottom) filling 1080x1920.

    Top and bottom show the same clip but bottom starts slightly later
    and uses a different crop region, creating a dynamic split effect.
    """
    _has_audio = has_audio_stream(video_path)
    offset = min(0.3, duration * 0.15)

    # Top: left crop region, bottom: right crop region (shifted)
    # Both scaled to fill 1080x960
    filter_complex = (
        f"[0:v]trim=start={start:.2f}:duration={duration:.2f},setpts=PTS-STARTPTS,"
        f"scale=-1:960,crop=1080:960:(iw-1080)/2:0,setsar=1,fps=30[top];"
        f"[0:v]trim=start={start + offset:.2f}:duration={duration:.2f},setpts=PTS-STARTPTS,"
        f"scale=-1:960,crop=1080:960:(iw-1080)/2:0,setsar=1,fps=30,"
        f"fade=t=in:st=0:d=0.4[bot];"
        f"[top][bot]vstack=inputs=2[vout]"
    )

    if _has_audio:
        filter_complex += (
            f";[0:a]atrim=start={start:.2f}:duration={duration:.2f},"
            f"asetpts=PTS-STARTPTS[aout]"
        )
        map_args = ["-map", "[vout]", "-map", "[aout]"]
    else:
        map_args = ["-map", "[vout]",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        # Re-add null audio input after filter
        filter_complex += ";[1:a]atrim=0:" + f"{duration:.2f}[aout]"
        map_args = ["-map", "[vout]", "-map", "[aout]"]

    try:
        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        if not _has_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-filter_complex", filter_complex] + map_args + [
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def add_text_overlay(video_path, text, out_path, position="bottom",
                     fontsize=42, fontcolor="white", box_opacity=0.5):
    """Add a text overlay to a video using FFmpeg drawtext."""
    # Escape text for FFmpeg
    safe_text = text.replace("'", "'\\''").replace(":", "\\:")
    if position == "top":
        y_expr = "h*0.08"
    elif position == "center":
        y_expr = "(h-text_h)/2"
    else:
        y_expr = "h*0.85"

    vf = (
        f"drawtext=text='{safe_text}':"
        f"fontsize={fontsize}:fontcolor={fontcolor}:"
        f"x=(w-text_w)/2:y={y_expr}:"
        f"box=1:boxcolor=black@{box_opacity}:boxborderw=8:"
        f"enable='between(t,0.3,99)'"
    )

    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "copy", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def detect_beats(music_path):
    """Detect beat positions in a music file using FFmpeg + onset detection.

    Returns {"bpm": int, "beats": [float, ...]} with beat timestamps in seconds.
    Pure Python + FFmpeg, no extra dependencies.
    """
    import struct

    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(music_path),
             "-ac", "1", "-ar", "22050",
             "-f", "f32le", "-acodec", "pcm_f32le",
             "pipe:1"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or len(result.stdout) < 100:
            return {"bpm": 120, "beats": []}

        raw = result.stdout
        n_samples = len(raw) // 4
        samples = struct.unpack(f"{n_samples}f", raw)

        sr = 22050
        hop = sr // 10  # 100ms windows

        # Compute RMS energy per window
        energy = []
        for i in range(0, n_samples - hop, hop):
            window = samples[i:i + hop]
            rms = (sum(s * s for s in window) / hop) ** 0.5
            energy.append(rms)

        if len(energy) < 4:
            return {"bpm": 120, "beats": []}

        # Compute onset strength (difference in energy)
        onset = [0.0]
        for i in range(1, len(energy)):
            diff = max(0, energy[i] - energy[i - 1])
            onset.append(diff)

        # Adaptive threshold: mean + 0.5 * std
        mean_onset = sum(onset) / len(onset)
        variance = sum((o - mean_onset) ** 2 for o in onset) / len(onset)
        std_onset = variance ** 0.5
        threshold = mean_onset + 0.5 * std_onset

        # Find peaks above threshold with minimum spacing (150ms)
        min_spacing = 2  # 2 windows = 200ms
        beats = []
        last_beat_idx = -min_spacing
        for i in range(1, len(onset) - 1):
            if (onset[i] > threshold
                    and onset[i] >= onset[i - 1]
                    and onset[i] >= onset[i + 1]
                    and i - last_beat_idx >= min_spacing):
                beats.append(round(i * hop / sr, 3))
                last_beat_idx = i

        # Estimate BPM from beat intervals
        if len(beats) >= 3:
            intervals = [beats[i + 1] - beats[i]
                         for i in range(len(beats) - 1)]
            # Filter outliers (keep intervals within 2x of median)
            intervals.sort()
            median = intervals[len(intervals) // 2]
            filtered = [iv for iv in intervals
                        if median * 0.5 <= iv <= median * 2.0]
            if filtered:
                avg_interval = sum(filtered) / len(filtered)
                bpm = round(60.0 / avg_interval)
                # Clamp to reasonable range
                bpm = max(60, min(200, bpm))
            else:
                bpm = 120
        else:
            bpm = 120

        return {"bpm": bpm, "beats": beats}

    except Exception:
        return {"bpm": 120, "beats": []}
