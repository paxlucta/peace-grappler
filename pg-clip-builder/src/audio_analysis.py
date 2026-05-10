"""audio_analysis.py — Local Whisper transcription for the speech-mode
analyzer.

Used when a brand profile sets ``analysis_mode = "speech"`` (talking-head
content like tutorials, interviews, podcasts). Visual-mode profiles
(MMA / sports / action) skip this entirely.

Transcripts are cached per (content_hash, model, language) under
``.cache/transcripts/`` so re-analysis (e.g. tag-schema edits) doesn't
re-transcribe the audio.

Model files come from Hugging Face on first use and are cached by the
underlying ``faster-whisper`` package under ``~/.cache/huggingface/hub/``.
"""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
CACHE_DIR = ROOT_DIR / ".cache" / "transcripts"


# ── Model lifecycle ─────────────────────────────────────────────────────────
#
# WhisperModel is heavyweight to construct (loads weights into memory) so
# we keep one per (size, device) and reuse it across calls.

_models = {}


def _get_model(size):
    """Return a cached WhisperModel for *size* ('tiny' / 'base' / 'small' /
    'medium' / 'large-v3'). Downloads the model on first use."""
    from faster_whisper import WhisperModel
    key = (size,)
    if key in _models:
        return _models[key]
    # CPU is the safest default; faster-whisper picks Metal acceleration on
    # Apple Silicon automatically when device='auto'. compute_type='int8'
    # keeps memory low; quality is still good for transcript-driven
    # segmentation.
    _models[key] = WhisperModel(
        size, device="auto", compute_type="int8",
    )
    return _models[key]


# ── Transcription ───────────────────────────────────────────────────────────

def _hash_audio(video_path):
    """Cheap content fingerprint — first 1MB + last 1MB + size — same shape
    as db.hash_file. Used as the transcript cache key."""
    p = Path(video_path)
    size = p.stat().st_size
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 1024 * 1024:
            f.seek(max(0, size - 1024 * 1024))
            h.update(f.read(1024 * 1024))
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()[:32]


def _cache_path(content_hash, model, language, translate=False):
    safe_lang = (language or "auto").replace("/", "_")
    suffix = ".en" if translate else ""
    return CACHE_DIR / f"{content_hash}.{model}.{safe_lang}{suffix}.json"


def _extract_audio_wav(video_path, dest_wav):
    """Pull the audio track out as 16 kHz mono WAV (Whisper's native format).
    Skips re-encoding any video stream — quick even for long files."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(dest_wav),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    return r.returncode == 0 and os.path.exists(dest_wav)


def transcribe(video_path, model="base", language=None, translate=False,
               on_log=None):
    """Transcribe *video_path* and return ``{"segments": [...], "language":
    "..."}`` where:

        segments = [{"start": float, "end": float, "text": str}, ...]
        language = ISO code Whisper auto-detected (e.g. 'ru'), or '' if unknown.

    *model*: Whisper size — tiny / base / small / medium / large-v3.
    *language*: ISO code (e.g. 'ru', 'en'). None = auto-detect per chunk.
    *translate*: if True, run Whisper in 'translate' mode — the source
        audio is transcribed AND translated to English in one pass. Useful
        for multilingual content where you want a uniform English
        transcript downstream regardless of source language.

    On no-audio / failure returns ``{"segments": [], "language": ""}``.
    Cached per (content_hash, model, language, translate-flag).
    """
    log = on_log or (lambda m: None)
    empty = {"segments": [], "language": ""}
    video_path = Path(video_path)
    if not video_path.exists():
        log(f"transcribe: missing file {video_path}")
        return empty

    content_hash = _hash_audio(video_path)
    cache_file = _cache_path(content_hash, model, language, translate=translate)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            log(f"Using cached transcript ({len(cached.get('segments', []))} segments).")
            # Older caches stored only `language` (the requested code or
            # ''); newer ones add `detected_language`. Prefer the detected
            # value when present so callers get the actual spoken language.
            detected = cached.get("detected_language") or cached.get("language") or ""
            return {
                "segments": cached.get("segments", []),
                "language": detected,
            }
        except Exception:
            pass

    # Extract audio to a temp WAV.
    log(f"Extracting audio from {video_path.name}...")
    detected_language = ""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        if not _extract_audio_wav(video_path, wav):
            log("ffmpeg audio extraction failed.")
            return empty

        task_label = "translate→English" if translate else "transcribe"
        log(f"Whisper '{model}' / {language or 'auto-detect'} / {task_label}...")
        try:
            m = _get_model(model)
        except Exception as e:
            log(f"Whisper model load failed: {e}")
            return empty

        try:
            segments_iter, info = m.transcribe(
                str(wav),
                language=(language or None),
                task=("translate" if translate else "transcribe"),
                beam_size=5,
                vad_filter=True,           # skip non-speech regions
                vad_parameters={"min_silence_duration_ms": 500},
            )
            segments = []
            for seg in segments_iter:
                txt = (seg.text or "").strip()
                if not txt:
                    continue
                segments.append({
                    "start": round(float(seg.start), 2),
                    "end":   round(float(seg.end), 2),
                    "text":  txt,
                })
            detected_language = getattr(info, "language", "") or ""
            log(f"Transcribed {len(segments)} segments "
                f"(detected language: {detected_language or '?'}).")
        except Exception as e:
            log(f"Whisper transcription failed: {e}")
            return empty

    # Cache.
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "model":             model,
            "language":          language or "",
            "detected_language": detected_language,
            "translate":         bool(translate),
            "segments":          segments,
        }, ensure_ascii=False))
    except Exception:
        pass

    return {"segments": segments, "language": detected_language}


def clear_cache():
    """Wipe all cached transcripts. Not currently surfaced in the UI but
    useful when changing Whisper model preferences across the board."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
