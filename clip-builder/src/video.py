"""video.py — Shared FFmpeg functions for ClipBuilder."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
ASSETS_DIR = ROOT_DIR / "assets"
THUMB_DIR = ROOT_DIR / ".cache" / "thumbnails"
XFADE_DUR = 0.7

# VIDEO_DIR (Analyze source) and OUTPUT_DIR (Library destination) are
# user-configurable on /settings — read from the active brand profile here at
# module load. Restart the app for folder changes to take effect.
try:
    import app_config as _ac
    VIDEO_DIR = _ac.get_source_dir()
    OUTPUT_DIR = _ac.get_output_dir()
except Exception:
    VIDEO_DIR = ROOT_DIR / "videos"
    OUTPUT_DIR = ROOT_DIR / "output"

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
    """Find an asset file by prefix (e.g. 'intro' -> assets/videos/intro.mp4).

    For ``prefix`` "intro" / "outro", an explicit path from the active brand
    profile (``intro_video`` / ``outro_video`` on /settings) wins. If empty
    or missing, falls back to scanning ``assets/videos/`` for files whose
    stem starts with the prefix — the legacy behavior."""
    if extensions is None:
        extensions = VIDEO_EXTENSIONS

    if prefix in ("intro", "outro"):
        try:
            import app_config as _ac
            cfg = _ac.get_config()
            override = (cfg.get(f"{prefix}_video") or "").strip()
        except Exception:
            override = ""
        if override:
            p = Path(override)
            if not p.is_absolute():
                p = ROOT_DIR / p
            if p.exists() and p.is_file():
                return p
            # Configured path is set but missing — log nothing and fall
            # through to the legacy scan so a broken override doesn't
            # silently disable intro/outro entirely.

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


_FACE_CASCADE = None
_FACE_CASCADE_TRIED = False


def _get_face_cascade():
    """Lazily load OpenCV's Haar face cascade. Returns None if OpenCV
    isn't installed or the cascade file can't be located — callers must
    treat face detection as optional."""
    global _FACE_CASCADE, _FACE_CASCADE_TRIED
    if _FACE_CASCADE_TRIED:
        return _FACE_CASCADE
    _FACE_CASCADE_TRIED = True
    try:
        import cv2
        path = os.path.join(cv2.data.haarcascades,
                            "haarcascade_frontalface_default.xml")
        if os.path.exists(path):
            cas = cv2.CascadeClassifier(path)
            if not cas.empty():
                _FACE_CASCADE = cas
    except Exception:
        _FACE_CASCADE = None
    return _FACE_CASCADE


def auto_crop_x_frac(video_path, start, duration, samples=18):
    """Pick a horizontal crop center for a wide clip so a 9:16 window
    captures the most visually interesting content.

    Three signals, in priority order:
    1. Face position (OpenCV Haar cascade, when available) — strongest
       weight, since cutting off a talking head is the worst failure
       mode.
    2. Inter-frame motion — finds the moving subject when no face is
       detected (action shots, profile views).
    3. Grayscale detail — texture / contrast as a tiebreaker.

    The per-column score is convolved with a 9:16-wide rectangular
    window; the window with the highest aggregate score wins.

    Returns ``None`` only if frame extraction failed outright; on every
    other error the function falls back to internal heuristics and
    returns a usable x_frac.
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None

    if duration <= 0:
        return None

    SAMPLE_W = 960  # downscaled width — big enough for small-face detect
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        # Skip the first/last 5% so transitions don't bias the crop.
        for i in range(samples):
            t = start + duration * (0.05 + 0.9 * (i / max(1, samples - 1)))
            fp = os.path.join(tmp, f"f{i:02d}.jpg")
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{t:.3f}",
                     "-i", str(video_path),
                     "-frames:v", "1",
                     "-vf", f"scale={SAMPLE_W}:-2",
                     "-q:v", "5", fp],
                    capture_output=True, timeout=20,
                )
                if r.returncode == 0 and os.path.exists(fp):
                    frames.append(np.asarray(Image.open(fp).convert("L"),
                                             dtype=np.float32))
            except Exception:
                continue

        if len(frames) < 2:
            return None

        h, w = frames[0].shape
        for f in frames[1:]:
            if f.shape != (h, w):
                return None
        target_w = max(1, int(round(h * 9.0 / 16.0)))
        if target_w >= w:
            # Source is already ≤ 9:16 — nothing to crop.
            return 0.5

        # Detail per column: stdev along vertical axis, averaged across frames.
        detail = np.zeros(w, dtype=np.float32)
        for f in frames:
            detail += f.std(axis=0)
        detail /= len(frames)

        # Motion per column: mean abs diff between consecutive frames.
        motion = np.zeros(w, dtype=np.float32)
        for a, b in zip(frames, frames[1:]):
            motion += np.abs(a - b).mean(axis=0)
        motion /= max(1, len(frames) - 1)

        # Face score — Gaussian bump per detected face, accumulated across
        # all frames so a face that appears in most frames dominates over
        # a one-off false positive. The bump width matches the face box
        # so multi-person shots split influence appropriately.
        face_score = np.zeros(w, dtype=np.float32)
        face_hits = 0
        cas = _get_face_cascade()
        if cas is not None:
            xs = np.arange(w, dtype=np.float32)
            # Drop boxes smaller than ~4% of frame height — these are
            # almost always false positives on text glyphs, icons, or
            # background detail (verified on a real test video where
            # 50-px boxes on title text out-voted the 140-px speaker
            # face).
            min_side = max(28, int(round(h * 0.04)))
            for f in frames:
                try:
                    u8 = f.astype("uint8")
                    boxes = cas.detectMultiScale(
                        u8, scaleFactor=1.1, minNeighbors=3,
                        minSize=(min_side, min_side),
                    )
                except Exception:
                    boxes = []
                for (x, y, bw, bh) in boxes:
                    cx = float(x) + float(bw) / 2.0
                    sigma = max(8.0, float(bw) * 0.7)
                    # Weight by face area so a 140×140 real face beats
                    # a 54×54 false positive (~6.7× the weight) even if
                    # both pass the min-size gate.
                    amplitude = float(bw) * float(bh)
                    face_score += (amplitude * np.exp(
                        -((xs - cx) ** 2) / (2.0 * sigma * sigma)
                    )).astype(np.float32)
                    face_hits += 1

        def _norm(v):
            m = float(v.max()) or 1.0
            return v / m

        # Combine. When faces were found in *any* frame, face dominates;
        # motion is the back-up subject signal; detail is a tiebreaker.
        if face_hits > 0:
            score = (0.70 * _norm(face_score)
                     + 0.22 * _norm(motion)
                     + 0.08 * _norm(detail))
        else:
            score = 0.6 * _norm(motion) + 0.4 * _norm(detail)

        # Slide a target_w window; pick the center with the highest sum.
        kernel = np.ones(target_w, dtype=np.float32)
        sums = np.convolve(score, kernel, mode="valid")  # len = w - target_w + 1
        best_left = int(np.argmax(sums))
        # The render filter expects x_frac in [0,1] where 0 = left edge of
        # the source and 1 = right edge of the *valid* crop range. ffmpeg's
        # crop=ih*9/16:ih:(iw-ih*9/16)*frac:0 maps frac=0 → x=0 and frac=1
        # → x=iw-ih*9/16 (=> right edge of frame). Convert center_px to that.
        max_left = w - target_w
        x_frac = best_left / max_left if max_left > 0 else 0.5
        return float(max(0.0, min(1.0, x_frac)))


def extract_wide_subclip_autocrop(video_path, start, duration, out_path,
                                  x_frac=None):
    """Like extract_subclip but for wide sources: crops a 9:16 window
    around the auto-detected subject region (or *x_frac* if provided)
    and outputs 1080x1920@30fps. No black bars.

    Falls back to a centered crop if detection fails.
    """
    if x_frac is None:
        x_frac = auto_crop_x_frac(video_path, start, duration)
        if x_frac is None:
            x_frac = 0.5
    x_frac = max(0.0, min(1.0, float(x_frac)))

    _has_audio = has_audio_stream(video_path)
    # ih*9/16 = target portrait width; offset interpolates from 0 to (iw - that).
    vf = (f"crop=ih*9/16:ih:(iw-ih*9/16)*{x_frac:.4f}:0,"
          "scale=1080:1920,setsar=1,fps=30")

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


def generate_placeholder(out_path, duration, color="black", width=1080, height=1920):
    """Generate a solid color video. Default 1080x1920 (portrait)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i",
             f"color=c={color}:s={width}x{height}:d={duration:.2f}:r=30",
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


def composite_layered_segment(placements, seg_dur, out_path):
    """Composite a single 1080x1920 segment from layered clips.

    *placements*: list of dicts, one per clip active during this segment:
        - source_path  (str): path to the source video file
        - source_start (float): offset into the source where this segment reads
        - source_dur   (float): how long to read (== segment duration)
        - is_wide      (bool): wide source → renders into a 1080x640 slot
        - layer        (int 0..2): z-order (0 = bottom, 2 = top)
        - position     (str): 'top' | 'center' | 'bottom' — slot for wide clips
        - muted        (bool): if true, this clip's audio is dropped
        - stack_order  (int, optional): tiebreaker within a layer

    Compositing rules:
      - Black 1080x1920 base canvas.
      - Clips are sorted by (layer asc, stack_order asc, list order). Each is
        overlaid on the running canvas. Non-wide covers full frame. Wide
        covers only its slot row (top y=0, center y=640, bottom y=1280).
        Higher layer ⇒ drawn on top ⇒ wins z-order.
      - Audio: every unmuted clip with an audio stream gets mixed via amix.
        If no clip has audio (or all muted), the segment is silent.

    Returns True on success.
    """
    if not placements:
        return generate_placeholder(out_path, seg_dur, "black")

    SLOT_Y = {"top": 0, "center": 640, "bottom": 1280}
    SLOT_H = 640
    W, H = 1080, 1920

    # Stable ordering: layer asc, then stack_order asc, then insertion order.
    placements = sorted(
        enumerate(placements),
        key=lambda e: (e[1]["layer"], e[1].get("stack_order", 0), e[0]),
    )
    placements = [p for _i, p in placements]

    cmd = ["ffmpeg", "-y"]
    # Input 0 — black base canvas (video).
    cmd += ["-f", "lavfi",
            "-i", f"color=c=black:s={W}x{H}:d={seg_dur:.3f}:r=30"]
    # Input 1 — silent audio fallback (used when every clip is muted/silent).
    cmd += ["-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=stereo:d={seg_dur:.3f}"]

    # Inputs 2..N — clip sources (with input-side seek + duration).
    for p in placements:
        cmd += ["-ss", f"{max(0.0, p['source_start']):.3f}",
                "-t", f"{p['source_dur']:.3f}",
                "-i", str(p["source_path"])]

    # Build filter graph.
    filters = []

    # Per-clip video processing. Three modes:
    #   1. free_crops (a list of {src,dst,z} fractions) → split the source
    #      into N copies, crop+scale each to its destination rectangle. The
    #      output of THIS branch is N labels [v{i}_0]..[v{i}_K-1] which the
    #      overlay chain below composites in z-order at their dst positions.
    #   2. wide clip with single-strip crop_x_frac → existing behavior
    #      (one full-frame [v{i}]).
    #   3. plain clip → existing behavior.
    free_crop_outputs = {}   # i -> list of (label, x_px, y_px, z) tuples
    for i, p in enumerate(placements):
        src_idx = i + 2
        free_crops = p.get("free_crops") or []
        if free_crops:
            # Normalize each rectangle. Drop anything degenerate so the
            # filter graph doesn't blow up on a 0-pixel scale.
            norm = []
            for rc in free_crops:
                try:
                    s = rc.get("src") or {}
                    d = rc.get("dst") or {}
                    sx = max(0.0, min(1.0, float(s.get("x_frac", 0))))
                    sy = max(0.0, min(1.0, float(s.get("y_frac", 0))))
                    sw = max(0.001, min(1.0, float(s.get("w_frac", 0))))
                    sh = max(0.001, min(1.0, float(s.get("h_frac", 0))))
                    dx = max(0.0, min(1.0, float(d.get("x_frac", 0))))
                    dy = max(0.0, min(1.0, float(d.get("y_frac", 0))))
                    dw = max(0.001, min(1.0, float(d.get("w_frac", 0))))
                    dh = max(0.001, min(1.0, float(d.get("h_frac", 0))))
                except Exception:
                    continue
                if sx + sw > 1.0: sw = 1.0 - sx
                if sy + sh > 1.0: sh = 1.0 - sy
                if dx + dw > 1.0: dw = 1.0 - dx
                if dy + dh > 1.0: dh = 1.0 - dy
                z = int(rc.get("z", 0) or 0)
                norm.append({
                    "sx": sx, "sy": sy, "sw": sw, "sh": sh,
                    "dx": dx, "dy": dy, "dw": dw, "dh": dh, "z": z,
                })
            if norm:
                # split=N so we can run N independent crop+scale chains
                # from the same source frame.
                n = len(norm)
                split_outs = "".join(f"[s{i}_{k}]" for k in range(n))
                filters.append(
                    f"[{src_idx}:v]setpts=PTS-STARTPTS,setsar=1,fps=30,"
                    f"split={n}{split_outs}"
                )
                outs = []
                for k, rc in enumerate(norm):
                    dst_w = max(2, int(round(W * rc["dw"])))
                    dst_h = max(2, int(round(H * rc["dh"])))
                    # crop in source-pixel space, then resize to the
                    # destination's pixel size on the 1080x1920 canvas.
                    filters.append(
                        f"[s{i}_{k}]"
                        f"crop=iw*{rc['sw']:.5f}:ih*{rc['sh']:.5f}:"
                        f"iw*{rc['sx']:.5f}:ih*{rc['sy']:.5f},"
                        f"scale={dst_w}:{dst_h}[v{i}_{k}]"
                    )
                    x_px = int(round(W * rc["dx"]))
                    y_px = int(round(H * rc["dy"]))
                    outs.append((f"v{i}_{k}", x_px, y_px, rc["z"]))
                # Lowest z first → overlaid earlier → drawn under.
                outs.sort(key=lambda t: t[3])
                free_crop_outputs[i] = outs
                continue   # skip the legacy single-output branch below

        # Legacy paths — single [v{i}] output.
        crop_frac = p.get("crop_x_frac")
        wide_cropped = p["is_wide"] and crop_frac is not None
        target_h = H if (not p["is_wide"] or wide_cropped) else SLOT_H
        if wide_cropped:
            f = max(0.0, min(1.0, float(crop_frac)))
            filters.append(
                f"[{src_idx}:v]setpts=PTS-STARTPTS,"
                f"crop=ih*9/16:ih:(iw-ih*9/16)*{f:.4f}:0,"
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps=30[v{i}]"
            )
        else:
            filters.append(
                f"[{src_idx}:v]setpts=PTS-STARTPTS,"
                f"scale={W}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps=30[v{i}]"
            )

    # Overlay chain — bottom layer first, top layer last.
    #
    # Each placement contributes either ONE overlay step (legacy paths)
    # or N overlay steps (free-mode), composited at their own dst
    # position in z-order before moving on to the next placement.
    prev_label = "[0:v]"
    overlay_steps = []   # list of (label_no_brackets, x, y)
    for i, p in enumerate(placements):
        if i in free_crop_outputs:
            for lbl, x, y, _z in free_crop_outputs[i]:
                overlay_steps.append((lbl, x, y))
            continue
        wide_cropped = p["is_wide"] and p.get("crop_x_frac") is not None
        if p["is_wide"] and not wide_cropped:
            y = SLOT_Y.get(p.get("position") or "top", 0)
        else:
            y = 0
        overlay_steps.append((f"v{i}", 0, y))

    # Now emit the actual overlay filters in order.
    for idx, (lbl, x, y) in enumerate(overlay_steps):
        is_last = (idx == len(overlay_steps) - 1)
        out_label = "[vout]" if is_last else f"[ov{idx}]"
        filters.append(
            f"{prev_label}[{lbl}]overlay=x={x}:y={y}:shortest=0{out_label}"
        )
        prev_label = out_label

    # Audio mixing — only unmuted clips that actually have an audio stream.
    audio_labels = []
    for i, p in enumerate(placements):
        if p.get("muted"):
            continue
        if not has_audio_stream(p["source_path"]):
            continue
        src_idx = i + 2
        filters.append(f"[{src_idx}:a]asetpts=PTS-STARTPTS[a{i}]")
        audio_labels.append(f"[a{i}]")

    if not audio_labels:
        # Route silence input through a labeled noop so -map can reference it.
        filters.append("[1:a]asetpts=PTS-STARTPTS[asilent]")
        audio_src = "[asilent]"
    elif len(audio_labels) == 1:
        audio_src = audio_labels[0]
    else:
        joined = "".join(audio_labels)
        filters.append(
            f"{joined}amix=inputs={len(audio_labels)}:duration=longest:"
            f"dropout_transition=0[amix]"
        )
        audio_src = "[amix]"

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", audio_src,
        "-t", f"{seg_dur:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace")[-1200:]
            print(f"[composite-layered] ffmpeg failed: {err}")
            return False
        return os.path.exists(out_path)
    except Exception as exc:
        print(f"[composite-layered] Exception: {exc}")
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


def pad_clip_to_duration(clip_path, offset, target_duration, out_path):
    """Pad a clip with black frames so it fills *target_duration* seconds.

    *offset* seconds of black are prepended; black is appended so the total
    reaches *target_duration*.  Audio is silence-padded to match.
    """
    clip_dur = get_video_duration(clip_path)
    tail = max(0, target_duration - offset - clip_dur)

    if offset < 0.05 and tail < 0.05:
        shutil.copy2(clip_path, out_path)
        return True

    vf = []
    af = []
    if offset > 0.05:
        vf.append(f"tpad=start_duration={offset:.3f}:start_mode=add:color=black")
        af.append(f"adelay={int(offset * 1000)}|{int(offset * 1000)}")
    if tail > 0.05:
        vf.append(f"tpad=stop_duration={tail:.3f}:stop_mode=add:color=black")
        af.append(f"apad=pad_dur={tail:.3f}")

    vf_str = ",".join(vf) if vf else "null"
    af_str = ",".join(af) if af else "anull"

    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", clip_path,
             "-vf", vf_str, "-af", af_str,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def stack_wide_videos(paths, out_path):
    """Stack 1-3 wide (landscape) videos vertically in a 1080x1920 portrait frame.

    Each video gets an equal share of the vertical space.
    Clips should be pre-padded to the same duration (see pad_clip_to_duration).
    If durations differ, shorter clips show black (not frozen frame).
    Audio is mixed from all inputs.
    """
    n = len(paths)
    if n < 1:
        return False
    if n == 1:
        return normalize_clip(paths[0], out_path)

    durations = [get_video_duration(p) for p in paths]
    max_dur = max(durations)
    slot_h = 1920 // n

    vf_parts = []
    labels = []
    for i, p in enumerate(paths):
        pad_expr = ""
        if durations[i] < max_dur - 0.05:
            pad_expr = (f",tpad=stop_duration={max_dur - durations[i]:.3f}"
                        f":stop_mode=add:color=black")
        vf_parts.append(
            f"[{i}:v]scale=1080:{slot_h}:force_original_aspect_ratio=decrease,"
            f"pad=1080:{slot_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=30{pad_expr}[v{i}]"
        )
        labels.append(f"[v{i}]")

    stack = "".join(labels) + f"vstack=inputs={n}[vout]"
    audio_labels = "".join(f"[{i}:a]" for i in range(n))
    audio = f"{audio_labels}amix=inputs={n}:duration=longest[aout]"
    filter_complex = ";".join(vf_parts) + ";" + stack + ";" + audio

    inputs = []
    for p in paths:
        inputs.extend(["-i", p])

    try:
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs +
            ["-filter_complex", filter_complex,
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


def _find_system_font(fontfamily=None):
    """Find a usable TrueType font on the system.

    If fontfamily is given, look for it in assets/fonts first, then
    system font directories.
    """
    if fontfamily:
        # Check assets/fonts
        fonts_dir = ASSETS_DIR / "fonts"
        if fonts_dir.is_dir():
            for ext in (".ttf", ".otf", ".ttc"):
                p = fonts_dir / (fontfamily + ext)
                if p.exists():
                    return str(p)
            # Case-insensitive fallback
            for f in fonts_dir.iterdir():
                if f.stem.lower() == fontfamily.lower() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    return str(f)
        # Check system fonts by name
        system_dirs = [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/Library/Fonts",
        ]
        for d in system_dirs:
            dp = Path(d)
            if dp.is_dir():
                for f in dp.iterdir():
                    if f.stem.lower() == fontfamily.lower() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                        return str(f)

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
                       w_frac=None, h_frac=None, fontfamily=None):
    """Render text onto a transparent RGBA image using Pillow.

    WYSIWYG: when w_frac/h_frac are provided, text is auto-sized to fill
    the box area, word-wrapped, and centered — matching the editor preview.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_path = _find_system_font(fontfamily)
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

    ANIM_DUR = 0.4  # seconds for enter/exit animation

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
        fontfamily = ov.get("fontfamily")
        trans_in = ov.get("trans_in", "fade")
        trans_out = ov.get("trans_out", "fade")

        # Render text to PNG
        try:
            img = _render_text_image(text, W, H, fs, fc, bold, bo,
                                     x_frac, y_frac, position,
                                     italic=italic, bgcolor=bgcolor,
                                     w_frac=w_frac, h_frac=h_frac,
                                     fontfamily=fontfamily)
            png_path = os.path.join(tmp_dir, f"_txt_{idx}.png")
            img.save(png_path)
        except Exception as exc:
            print(f"[text-overlay] Pillow render failed: {exc}")
            continue

        dur = e - s
        input_idx = idx + 1  # input 0 is the video, overlays are 1, 2, 3...
        # Loop the still image and give it a duration so fade/timing works
        inputs.extend(["-loop", "1", "-t", f"{dur + 1:.2f}", "-i", png_path])

        # Build overlay with separate enter/exit animations
        ad = min(ANIM_DUR, dur / 3)
        overlay_label = f"[{input_idx}:v]"
        out_label = f"[txt{idx}]"
        need_fade = trans_in == "fade" or trans_out == "fade"
        use_slide = trans_in.startswith("slide_") or trans_out.startswith("slide_")

        # Apply fade filter on the image input if needed
        cur_label = overlay_label
        if need_fade:
            fade_label = f"[tf{idx}]"
            fade_parts = []
            if trans_in == "fade":
                fade_parts.append(f"fade=t=in:st=0:d={ad}:alpha=1")
            if trans_out == "fade":
                fade_parts.append(f"fade=t=out:st={dur - ad}:d={ad}:alpha=1")
            filter_parts.append(f"{overlay_label}{','.join(fade_parts)}{fade_label}")
            cur_label = fade_label

        # Build x/y expressions for slide effects
        def _slide_enter(direction, axis):
            d = direction.split("_")[1] if "_" in direction else ""
            if axis == "x":
                if d == "left": return f"if(lt(t-{s},{ad}),W-W*(t-{s})/{ad},0)"
                if d == "right": return f"if(lt(t-{s},{ad}),-W+W*(t-{s})/{ad},0)"
            else:
                if d == "up": return f"if(lt(t-{s},{ad}),H-H*(t-{s})/{ad},0)"
                if d == "down": return f"if(lt(t-{s},{ad}),-H+H*(t-{s})/{ad},0)"
            return "0"

        def _slide_exit(direction, axis):
            d = direction.split("_")[1] if "_" in direction else ""
            if axis == "x":
                if d == "left": return f"if(gt(t,{e-ad}),-W*(t-{e-ad})/{ad},0)"
                if d == "right": return f"if(gt(t,{e-ad}),W*(t-{e-ad})/{ad},0)"
            else:
                if d == "up": return f"if(gt(t,{e-ad}),-H*(t-{e-ad})/{ad},0)"
                if d == "down": return f"if(gt(t,{e-ad}),H*(t-{e-ad})/{ad},0)"
            return "0"

        if use_slide:
            enter_x = _slide_enter(trans_in, "x") if trans_in.startswith("slide_") else "0"
            enter_y = _slide_enter(trans_in, "y") if trans_in.startswith("slide_") else "0"
            exit_x = _slide_exit(trans_out, "x") if trans_out.startswith("slide_") else "0"
            exit_y = _slide_exit(trans_out, "y") if trans_out.startswith("slide_") else "0"
            # Combine: during enter phase use enter expr, during exit use exit, else 0
            x_expr = f"if(lt(t,{s+ad}),{enter_x},if(gt(t,{e-ad}),{exit_x},0))"
            y_expr = f"if(lt(t,{s+ad}),{enter_y},if(gt(t,{e-ad}),{exit_y},0))"
            # Simplify if one side is just "0"
            if enter_x == "0" and exit_x == "0": x_expr = "0"
            if enter_y == "0" and exit_y == "0": y_expr = "0"
            filter_parts.append(
                f"{prev_label}{cur_label}overlay="
                f"x='{x_expr}':y='{y_expr}':"
                f"enable='between(t,{s},{e})'{out_label}"
            )
        else:
            filter_parts.append(
                f"{prev_label}{cur_label}overlay=0:0:"
                f"enable='between(t,{s},{e})':"
                f"shortest=0{out_label}"
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
