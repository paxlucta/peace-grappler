"""transcription.py — Dispatcher for audio→text transcription.

Wraps the local Whisper backend (audio_analysis.py) plus optional cloud
providers (OpenAI, Gemini) behind a single ``transcribe()`` entry point.
analyze_speech() calls this; the active provider is chosen on /settings.

Return shape matches audio_analysis.transcribe():
    {"segments": [{"start", "end", "text"}], "language": "<iso>"}

All providers honor the same cache key
(``content_hash | provider | model | language | translate``) so flipping
back to a previously-used provider re-uses its cached transcript.

Cloud providers read API keys from the environment:
    - openai → OPENAI_API_KEY
    - gemini → GEMINI_API_KEY (or GOOGLE_API_KEY)
"""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
CACHE_DIR = ROOT_DIR / ".cache" / "transcripts"

PROVIDERS = ("whisper", "openai", "gemini")
DEFAULT_PROVIDER = "whisper"

# Per-provider model menus. The "model" value is what we pass to the
# backend; the UI gets ``label`` to render.
MODELS = {
    "whisper": [
        {"value": "tiny",     "label": "tiny — 39 MB, fastest"},
        {"value": "base",     "label": "base — 74 MB, balanced"},
        {"value": "small",    "label": "small — 244 MB, better accuracy"},
        {"value": "medium",   "label": "medium — 769 MB, very accurate"},
        {"value": "large-v3", "label": "large-v3 — 1.5 GB, best quality"},
    ],
    "openai": [
        # whisper-1 is currently the only OpenAI model that returns
        # segment-level timestamps via response_format=verbose_json.
        {"value": "whisper-1", "label": "whisper-1 — OpenAI hosted Whisper"},
    ],
    "gemini": [
        {"value": "gemini-2.5-flash-lite",
         "label": "gemini-2.5-flash-lite — cheapest"},
        {"value": "gemini-2.5-flash",
         "label": "gemini-2.5-flash — default"},
        {"value": "gemini-2.0-flash",
         "label": "gemini-2.0-flash"},
        {"value": "gemini-2.5-pro",
         "label": "gemini-2.5-pro — best quality"},
    ],
}
DEFAULT_MODEL = {
    "whisper": "base",
    "openai":  "whisper-1",
    "gemini":  "gemini-2.5-flash",
}

# OpenAI's upload limit on /audio/transcriptions.
_OPENAI_MAX_BYTES = 25 * 1024 * 1024
# Conservative cap for inline_data in a single Gemini generateContent call.
_GEMINI_INLINE_MAX_BYTES = 19 * 1024 * 1024


def auth_status(provider):
    """Return (configured: bool, hint: str). Local whisper is always 'configured'."""
    if provider == "whisper":
        return (True, "")
    if provider == "openai":
        return (bool(os.environ.get("OPENAI_API_KEY")),
                "Set OPENAI_API_KEY in your .env or environment.")
    if provider == "gemini":
        ok = bool(os.environ.get("GEMINI_API_KEY")
                  or os.environ.get("GOOGLE_API_KEY"))
        return (ok, "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in your .env.")
    return (False, f"Unknown transcription provider: {provider}")


def models_for(provider):
    """List of {value,label} dicts for the picker."""
    return MODELS.get(provider, [])


def default_model_for(provider):
    return DEFAULT_MODEL.get(provider, "")


# ── Cache + audio helpers ──────────────────────────────────────────────────

def _hash_audio(video_path):
    """Cheap content fingerprint — first 1MB + last 1MB + size. Matches the
    fingerprint shape used by audio_analysis so its cache hits remain hot."""
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


def _cache_path(content_hash, provider, model, language, translate, hint=""):
    safe_lang = (language or "auto").replace("/", "_")
    suffix = ".en" if translate else ""
    # Hash the hint into the cache key so changing it invalidates cleanly.
    if hint:
        import hashlib as _h
        suffix += "." + _h.sha1(hint.encode("utf-8")).hexdigest()[:8]
    return CACHE_DIR / (
        f"{content_hash}.{provider}.{model}.{safe_lang}{suffix}.json"
    )


def _extract_audio_m4a(video_path, dest_m4a, bitrate="64k"):
    """Pull the audio track out as 16 kHz mono AAC/m4a for upload to a
    cloud provider. Small enough to fit OpenAI's 25 MB and Gemini's
    inline_data caps for tens of minutes of speech."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "aac", "-b:a", bitrate, str(dest_m4a),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    return r.returncode == 0 and os.path.exists(dest_m4a)


# ── Dispatcher ─────────────────────────────────────────────────────────────

def transcribe(video_path, provider="whisper", model=None,
               language=None, translate=False, on_log=None,
               on_progress=None, hint=""):
    """Transcribe *video_path*. Returns ``{"segments": [...], "language": "..."}``.

    *provider*: one of ``PROVIDERS``. Unknown falls back to ``whisper``.
    *model*: provider-specific model id. ``None`` → ``DEFAULT_MODEL[provider]``.
    *language*: ISO code (e.g. 'ru'). ``None`` = auto-detect.
    *translate*: produce English text regardless of source language.
    *on_progress*: optional ``callable(frac, label)``; called periodically
        for long-running steps so the caller can drive a progress bar.
        Whisper-local emits per-segment heartbeats; cloud providers emit
        coarser milestones (extract → upload → parse).
    *hint*: optional free-text instruction for the model — most useful
        for code-switching audio (e.g. ``"Mixes Russian and English;
        keep each in its native script."``). Goes into Gemini's prompt
        verbatim and into Whisper's ``initial_prompt``.
    """
    log = on_log or (lambda m: None)
    progress = on_progress or (lambda frac, label="": None)
    hint = (hint or "").strip()
    empty = {"segments": [], "language": ""}
    video_path = Path(video_path)
    if not video_path.exists():
        log(f"transcribe: missing file {video_path}")
        return empty

    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        log(f"Unknown transcription provider {provider!r}; using whisper.")
        provider = "whisper"
    model = (model or "").strip() or DEFAULT_MODEL.get(provider, "")

    ok, hint = auth_status(provider)
    if not ok:
        log(f"{provider} transcription is not configured. {hint}")
        return empty

    content_hash = _hash_audio(video_path)
    cache_file = _cache_path(content_hash, provider, model, language,
                             translate, hint)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            log(f"Using cached transcript "
                f"({len(cached.get('segments', []))} segments).")
            return {
                "segments": cached.get("segments", []),
                "language": (cached.get("detected_language")
                             or cached.get("language") or ""),
            }
        except Exception:
            pass

    if provider == "whisper":
        # Local Whisper has its own cache too; harmless overlap.
        import audio_analysis
        out = audio_analysis.transcribe(
            video_path, model=model, language=language,
            translate=translate, on_log=log, on_progress=progress,
            initial_prompt=hint or None,
        )
    elif provider == "openai":
        out = _openai(video_path, model, language, translate, log, progress,
                      hint)
    elif provider == "gemini":
        out = _gemini(video_path, model, language, translate, log, progress,
                      hint)
    else:
        return empty

    if out.get("segments"):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({
                "provider":          provider,
                "model":             model,
                "language":          language or "",
                "detected_language": out.get("language") or "",
                "translate":         bool(translate),
                "segments":          out.get("segments", []),
            }, ensure_ascii=False))
        except Exception:
            pass
    return out


# ── OpenAI Whisper API ─────────────────────────────────────────────────────

def _openai(video_path, model, language, translate, log, progress, hint=""):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log("OPENAI_API_KEY missing.")
        return {"segments": [], "language": ""}

    progress(0.05, "extracting audio")
    log(f"Extracting audio from {video_path.name}...")
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.m4a"
        if not _extract_audio_m4a(video_path, audio):
            log("ffmpeg audio extraction failed.")
            return {"segments": [], "language": ""}
        size = audio.stat().st_size
        if size > _OPENAI_MAX_BYTES:
            log(f"Audio is {size / 1024 / 1024:.1f} MB — over OpenAI's "
                f"25 MB limit. Use a shorter video or switch to local Whisper.")
            return {"segments": [], "language": ""}

        endpoint = "translations" if translate else "transcriptions"
        url = f"https://api.openai.com/v1/audio/{endpoint}"
        log(f"OpenAI {model} / {language or 'auto'} / "
            f"{'translate→English' if translate else 'transcribe'}...")

        boundary = "----pgform" + hex(int(time.time() * 1000))[2:]
        parts = []

        def add_field(name, value):
            parts.append((
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode())

        def add_file(name, filename, data, ctype="audio/mp4"):
            parts.append((
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode())
            parts.append(data)
            parts.append(b"\r\n")

        add_field("model", model)
        add_field("response_format", "verbose_json")
        if not translate:
            # `translations` endpoint always outputs English and does not
            # accept this field.
            add_field("timestamp_granularities[]", "segment")
            if language:
                add_field("language", language)
        # OpenAI Whisper accepts a `prompt` field that biases vocabulary
        # — not a true multilingual hint, but useful for proper nouns,
        # spellings, and signaling code-switching.
        if hint:
            add_field("prompt", hint)
        with open(audio, "rb") as f:
            audio_bytes = f.read()
        add_file("file", "audio.m4a", audio_bytes)
        parts.append((f"--{boundary}--\r\n").encode())
        payload = b"".join(parts)

        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        progress(0.30, f"uploading {len(audio_bytes) / 1024 / 1024:.1f} MB")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                progress(0.90, "parsing response")
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            log(f"OpenAI transcription HTTP {e.code}: {err[:300]}")
            return {"segments": [], "language": ""}
        except Exception as e:
            log(f"OpenAI transcription failed: {e}")
            return {"segments": [], "language": ""}

    segments = []
    for s in (data.get("segments") or []):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        segments.append({
            "start": round(float(s.get("start", 0)), 2),
            "end":   round(float(s.get("end", 0)), 2),
            "text":  txt,
        })
    # Translations endpoint may not return segments; fall back to whole-file.
    if not segments and (data.get("text") or "").strip():
        segments.append({
            "start": 0.0,
            "end":   float(data.get("duration") or 0.0),
            "text":  data["text"].strip(),
        })
    detected = (data.get("language") or "").strip().lower()
    log(f"OpenAI returned {len(segments)} segments "
        f"(detected language: {detected or '?'}).")
    progress(1.0, "transcription complete")
    return {"segments": segments, "language": detected}


# ── Gemini ─────────────────────────────────────────────────────────────────

_GEMINI_PROMPT_TEMPLATE = (
    "Transcribe the speech in this audio. Output a single JSON array, no "
    "code fences and no commentary. Each element must have keys: "
    "`start` (number, seconds from the beginning of the audio), "
    "`end` (number, seconds), and `text` (string). "
    "Break the transcript into natural speech segments roughly 2–10 seconds "
    "long; do not output one giant segment. "
    "If the audio mixes languages (code-switching), keep each phrase in "
    "its original language and original script — do not translate or "
    "transliterate.\n"
    "{LANG_HINT}{XLAT_HINT}{USER_HINT}"
)


def _gemini_files_upload(api_key, audio_bytes, log, progress):
    """Upload *audio_bytes* via the Gemini Files API and wait for the
    resource to reach state=ACTIVE. Returns the file resource dict
    (with ``uri`` / ``mime_type`` / ``name``) on success, or ``None``.

    Used when the audio is too large for the inline_data path in
    generateContent. Files API accepts up to 2 GB per upload."""
    boundary = "----pgfiles" + hex(int(time.time() * 1000))[2:]
    meta = json.dumps({"file": {"display_name": "audio.m4a"}})
    parts = []
    parts.append(f"--{boundary}\r\n"
                 f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                 f"{meta}\r\n".encode())
    parts.append(f"--{boundary}\r\n"
                 f"Content-Type: audio/mp4\r\n\r\n".encode())
    parts.append(audio_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    upload_url = (
        "https://generativelanguage.googleapis.com/upload/v1beta/files"
        f"?uploadType=multipart&key={api_key}"
    )
    req = urllib.request.Request(
        upload_url, data=payload, method="POST",
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    progress(0.10, f"uploading {len(audio_bytes) / 1024 / 1024:.1f} MB to Files API")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            up = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        log(f"Gemini Files upload HTTP {e.code}: {err[:300]}")
        return None
    except Exception as e:
        log(f"Gemini Files upload failed: {e}")
        return None

    file_res = up.get("file") if isinstance(up, dict) else None
    if not file_res or not file_res.get("name"):
        log(f"Gemini Files upload returned no file resource: {str(up)[:200]}")
        return None
    name = file_res["name"]      # e.g. "files/abc123"
    file_id = name.split("/", 1)[-1]
    poll_url = (f"https://generativelanguage.googleapis.com/v1beta/"
                f"{name}?key={api_key}")
    # Audio files usually flip to ACTIVE within 1-3s. Cap at 60s so a
    # stuck upload doesn't hang the worker indefinitely.
    deadline = time.time() + 60
    state = (file_res.get("state") or "").upper()
    while state == "PROCESSING" and time.time() < deadline:
        time.sleep(1.5)
        try:
            with urllib.request.urlopen(poll_url, timeout=30) as r:
                file_res = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log(f"Gemini Files poll failed: {e}")
            return None
        state = (file_res.get("state") or "").upper()
    if state != "ACTIVE":
        log(f"Gemini Files upload didn't become ACTIVE (state={state}).")
        return None
    log(f"Gemini Files: {file_id} ready (state={state}).")
    return file_res


def _gemini_files_delete(api_key, name, log):
    """Best-effort cleanup of an uploaded file. Quota for Files is 20 GB
    per project, so we tidy up after every transcription pass."""
    if not name:
        return
    try:
        del_url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"{name}?key={api_key}")
        req = urllib.request.Request(del_url, method="DELETE")
        urllib.request.urlopen(req, timeout=30).close()
    except Exception as e:
        log(f"(Gemini Files cleanup warning: {e})")


def _gemini(video_path, model, language, translate, log, progress, hint=""):
    api_key = (os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        log("GEMINI_API_KEY missing.")
        return {"segments": [], "language": ""}

    progress(0.05, "extracting audio")
    log(f"Extracting audio from {video_path.name}...")
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.m4a"
        if not _extract_audio_m4a(video_path, audio):
            log("ffmpeg audio extraction failed.")
            return {"segments": [], "language": ""}
        audio_bytes = audio.read_bytes()

    size = len(audio_bytes)

    lang_hint = (f"Source language code: {language}. " if language else "")
    xlat_hint = ("Translate the text fields to English. "
                 if translate else "")
    user_hint = (f"User note: {hint}\n" if hint else "")
    prompt = (_GEMINI_PROMPT_TEMPLATE
              .replace("{LANG_HINT}", lang_hint)
              .replace("{XLAT_HINT}", xlat_hint)
              .replace("{USER_HINT}", user_hint))

    # Choose the transport: inline for small audio (lower latency, no
    # extra roundtrips), Files API for anything over the inline cap so
    # we don't crash on long videos.
    uploaded_file = None
    if size <= _GEMINI_INLINE_MAX_BYTES:
        audio_part = {"inline_data": {
            "mime_type": "audio/mp4",
            "data": base64.b64encode(audio_bytes).decode(),
        }}
    else:
        log(f"Audio is {size / 1024 / 1024:.1f} MB — using Gemini Files API "
            f"(inline cap is {_GEMINI_INLINE_MAX_BYTES / 1024 / 1024:.0f} MB).")
        uploaded_file = _gemini_files_upload(api_key, audio_bytes, log, progress)
        if not uploaded_file:
            return {"segments": [], "language": ""}
        audio_part = {"file_data": {
            "mime_type": uploaded_file.get("mimeType") or "audio/mp4",
            "file_uri": uploaded_file["uri"],
        }}

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    body = {
        "contents": [{
            "parts": [audio_part, {"text": prompt}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }
    log(f"Gemini {model} / {language or 'auto'} / "
        f"{'translate→English' if translate else 'transcribe'}...")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"},
    )
    progress(0.30, "running model")

    # Heartbeat — generateContent is a single blocking HTTP call that can
    # easily sit for minutes on a long audio file. Without this, the
    # progress bar freezes and the log goes silent, which looks broken.
    # We run a daemon thread that ticks every 5s with elapsed time + a
    # bar that creeps toward 0.85 (asymptotic; never claims completion).
    _stop = threading.Event()
    _t0 = time.monotonic()
    def _heartbeat():
        last_log = 0
        while not _stop.wait(2.0):
            elapsed = time.monotonic() - _t0
            # Half-life ≈ 45s so the bar advances visibly early and
            # slows down as time goes on instead of pinning at 0.85.
            frac = 0.30 + 0.55 * (1 - 0.5 ** (elapsed / 45.0))
            progress(frac, f"running model ({int(elapsed)}s)")
            # Log line every ~15s — chatty enough to feel alive, quiet
            # enough not to spam.
            if elapsed - last_log >= 15:
                last_log = elapsed
                log(f"  still waiting on Gemini ({int(elapsed)}s)...")
    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            _stop.set()
            progress(0.90, "parsing response")
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _stop.set()
        err = e.read().decode("utf-8", errors="replace")
        log(f"Gemini transcription HTTP {e.code}: {err[:400]}")
        if uploaded_file:
            _gemini_files_delete(api_key, uploaded_file.get("name"), log)
        return {"segments": [], "language": ""}
    except Exception as e:
        _stop.set()
        log(f"Gemini transcription failed: {e}")
        if uploaded_file:
            _gemini_files_delete(api_key, uploaded_file.get("name"), log)
        return {"segments": [], "language": ""}
    finally:
        _stop.set()
        log(f"Gemini call returned after {int(time.monotonic() - _t0)}s.")

    if uploaded_file:
        _gemini_files_delete(api_key, uploaded_file.get("name"), log)

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception:
        log(f"Gemini response missing candidates: {str(data)[:300]}")
        return {"segments": [], "language": ""}
    if not text:
        log("Gemini returned an empty response.")
        return {"segments": [], "language": ""}

    # Tolerate fenced code blocks just in case the model ignored the
    # response-mime-type hint.
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl >= 0 and text[:nl].strip().lower() in ("json", ""):
            text = text[nl + 1:]
        text = text.rstrip("`").strip()

    try:
        parsed = json.loads(text)
    except Exception as e:
        log(f"Gemini JSON parse failed: {e}; first 200 chars: {text[:200]!r}")
        return {"segments": [], "language": ""}
    if not isinstance(parsed, list):
        log("Gemini response was not a JSON array.")
        return {"segments": [], "language": ""}

    segments = []
    for s in parsed:
        if not isinstance(s, dict):
            continue
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        try:
            start = float(s.get("start", 0))
            end   = float(s.get("end", start))
        except Exception:
            continue
        segments.append({
            "start": round(start, 2),
            "end":   round(end, 2),
            "text":  txt,
        })
    log(f"Gemini returned {len(segments)} segments.")
    progress(1.0, "transcription complete")
    # Gemini doesn't tell us a detected ISO code; pass through the hint.
    return {"segments": segments, "language": (language or "").lower()}
