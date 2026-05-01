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


def _drawtext_y(position):
    """Return FFmpeg y expression for a text position."""
    if position == "top":
        return "h*0.08"
    elif position == "center":
        return "(h-text_h)/2"
    return "h*0.85"


def _escape_drawtext(text):
    """Escape text for FFmpeg drawtext filter."""
    return text.replace("'", "'\\''").replace(":", "\\:")


def add_text_overlay(video_path, text, out_path, position="bottom",
                     fontsize=42, fontcolor="white", box_opacity=0.5,
                     start_time=0.3, end_time=None):
    """Add a text overlay to a video using FFmpeg drawtext."""
    safe_text = _escape_drawtext(text)
    y_expr = _drawtext_y(position)
    end_t = end_time if end_time is not None else 9999

    vf = (
        f"drawtext=text='{safe_text}':"
        f"fontsize={fontsize}:fontcolor={fontcolor}:"
        f"x=(w-text_w)/2:y={y_expr}:"
        f"box=1:boxcolor=black@{box_opacity}:boxborderw=8:"
        f"enable='between(t,{start_time},{end_t})'"
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


def _find_system_font():
    """Find a usable TrueType font on the system."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Geneva.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _parse_color(color_str, default=(255, 255, 255, 255)):
    """Parse a color string (#hex, 0xhex, or name) to RGBA tuple."""
    if not isinstance(color_str, str):
        return default
    if color_str.startswith("#") and len(color_str) >= 7:
        return (int(color_str[1:3], 16), int(color_str[3:5], 16),
                int(color_str[5:7], 16), 255)
    if color_str.startswith("0x") and len(color_str) >= 8:
        return (int(color_str[2:4], 16), int(color_str[4:6], 16),
                int(color_str[6:8], 16), 255)
    names = {"white": (255,255,255,255), "black": (0,0,0,255),
             "red": (255,0,0,255), "yellow": (255,255,0,255)}
    return names.get(color_str, default)


def _wrap_text(text, font, max_width, draw):
    """Word-wrap text to fit within max_width pixels. Returns list of lines."""
    words = text.split()
    if not words:
        return [text]
    lines = []
    current = words[0]
    for word in words[1:]:
        test = current + " " + word
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _render_text_image(text, width, height, fontsize, fontcolor, bold,
                       box_opacity, x_frac, y_frac, position,
                       italic=False, bgcolor="#000000",
                       w_frac=None, h_frac=None):
    """Render text onto a transparent RGBA image using Pillow.

    WYSIWYG: when w_frac/h_frac are provided, text is auto-sized to fill
    the box area, word-wrapped, and centered — matching the editor preview.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_path = _find_system_font()
    color = _parse_color(fontcolor)

    if w_frac and h_frac and x_frac is not None and y_frac is not None:
        # WYSIWYG mode: replicate the editor's auto-fit behavior
        box_w = int(width * w_frac)
        box_h = int(height * h_frac)
        box_cx = int(width * x_frac)
        box_cy = int(height * y_frac)

        # Binary search for largest font that fits in the box
        # Must fit BOTH width and height
        lo, hi, best_size = 6, max(box_w, box_h), 6
        while lo <= hi:
            mid = (lo + hi) // 2
            try:
                test_font = ImageFont.truetype(font_path, mid) if font_path else ImageFont.load_default()
            except Exception:
                test_font = ImageFont.load_default()
            lines = _wrap_text(text, test_font, box_w, draw)
            total_h = 0
            max_line_w = 0
            for line in lines:
                bb = draw.textbbox((0, 0), line, font=test_font)
                total_h += bb[3] - bb[1]
                max_line_w = max(max_line_w, bb[2] - bb[0])
            total_h += int(mid * 0.15) * max(0, len(lines) - 1)
            if total_h <= box_h and max_line_w <= box_w:
                best_size = mid
                lo = mid + 1
            else:
                hi = mid - 1

        try:
            font = ImageFont.truetype(font_path, best_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        lines = _wrap_text(text, font, box_w, draw)
        line_heights = []
        line_widths = []
        for line in lines:
            bb = draw.textbbox((0, 0), line, font=font)
            line_widths.append(bb[2] - bb[0])
            line_heights.append(bb[3] - bb[1])
        spacing = int(best_size * 0.15)
        total_h = sum(line_heights) + spacing * max(0, len(lines) - 1)

        # Box top-left from center
        bx = box_cx - box_w // 2
        by = box_cy - box_h // 2

        # Draw background
        if box_opacity > 0:
            pad = 5
            bg_rgba = _parse_color(bgcolor, (0, 0, 0, 255))
            bg_fill = (bg_rgba[0], bg_rgba[1], bg_rgba[2], int(box_opacity * 255))
            box_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            box_draw = ImageDraw.Draw(box_overlay)
            box_draw.rounded_rectangle(
                [bx - pad, by - pad, bx + box_w + pad, by + box_h + pad],
                radius=4, fill=bg_fill,
            )
            img = Image.alpha_composite(img, box_overlay)
            draw = ImageDraw.Draw(img)

        # Draw lines centered in box
        cursor_y = by + (box_h - total_h) // 2
        for i, line in enumerate(lines):
            lx = bx + (box_w - line_widths[i]) // 2
            draw.text((lx, cursor_y), line, font=font, fill=color)
            cursor_y += line_heights[i] + spacing

        return img

    # Legacy fallback: simple centered text
    try:
        font = ImageFont.truetype(font_path, fontsize) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    if x_frac is not None and y_frac is not None:
        tx = int(width * x_frac - tw / 2)
        ty = int(height * y_frac - th / 2)
    else:
        tx = int((width - tw) / 2)
        if position == "top":
            ty = int(height * 0.08)
        elif position == "center":
            ty = int((height - th) / 2)
        else:
            ty = int(height * 0.85)

    if box_opacity > 0:
        pad = 5
        bg_rgba = _parse_color(bgcolor, (0, 0, 0, 255))
        bg_fill = (bg_rgba[0], bg_rgba[1], bg_rgba[2], int(box_opacity * 255))
        box_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        box_draw = ImageDraw.Draw(box_overlay)
        box_draw.rounded_rectangle(
            [tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
            radius=4, fill=bg_fill,
        )
        img = Image.alpha_composite(img, box_overlay)
        draw = ImageDraw.Draw(img)

    draw.text((tx, ty), text, font=font, fill=color)
    return img


def add_multiple_text_overlays(video_path, overlays, out_path):
    """Apply multiple text overlays using Pillow + FFmpeg overlay filter.

    Uses Pillow to render text to transparent PNG images, then composites
    them onto the video using FFmpeg's overlay filter with enable expressions.

    overlays: list of dicts with keys:
        text, start_time, end_time, position ("top"/"center"/"bottom"),
        fontsize (default 42), fontcolor (default "white"), box_opacity (default 0.5),
        x_frac, y_frac (optional fractional position)
    """
    if not overlays:
        shutil.copy2(str(video_path), out_path)
        return True

    # Video is always 1080x1920
    W, H = 1080, 1920

    tmp_dir = os.path.dirname(out_path)
    inputs = ["-i", str(video_path)]
    filter_parts = []
    prev_label = "[0:v]"

    for idx, ov in enumerate(overlays):
        text = ov["text"]
        fs = ov.get("fontsize", 42)
        fc = ov.get("fontcolor", "white")
        bold = ov.get("bold", False)
        italic = ov.get("italic", False)
        bo = ov.get("box_opacity", 0.5)
        bgcolor = ov.get("bgcolor", "#000000")
        s = ov["start_time"]
        e = ov["end_time"]
        x_frac = ov.get("x_frac")
        y_frac = ov.get("y_frac")
        w_frac = ov.get("w_frac")
        h_frac = ov.get("h_frac")
        position = ov.get("position", "bottom")

        # Render text to PNG
        try:
            img = _render_text_image(text, W, H, fs, fc, bold, bo,
                                     x_frac, y_frac, position,
                                     italic=italic, bgcolor=bgcolor,
                                     w_frac=w_frac, h_frac=h_frac)
            png_path = os.path.join(tmp_dir, f"_txt_{idx}.png")
            img.save(png_path)
        except Exception as exc:
            print(f"[text-overlay] Pillow render failed: {exc}")
            continue

        input_idx = len(inputs) // 2  # each -i adds 2 args
        inputs.extend(["-i", png_path])
        out_label = f"[txt{idx}]"
        filter_parts.append(
            f"{prev_label}[{input_idx}:v]overlay=0:0:"
            f"enable='between(t,{s},{e})'{out_label}"
        )
        prev_label = out_label

    if not filter_parts:
        shutil.copy2(str(video_path), out_path)
        return True

    vf = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", vf,
        "-map", prev_label, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", out_path,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"[text-overlay] FFmpeg failed: {r.stderr.decode('utf-8', errors='replace')[-800:]}")
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception as exc:
        print(f"[text-overlay] Exception: {exc}")
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
