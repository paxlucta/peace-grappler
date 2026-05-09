"""ai_cli.py — Provider-agnostic AI CLI dispatch.

Wraps the local AI CLI tools (claude / codex / gemini) behind a single
`call_ai(prompt, frames=..., model=..., timeout=..., on_log=...)` entry point.

The active provider + per-provider settings (binary path, default model) live
in `data/ai_cli_config.json` and can be edited from the /settings page.

Capabilities matrix:
  - claude  : full (text + image frames, stream-json protocol)
  - codex   : text-only (frames are dropped with a warning)
  - gemini  : text-only (frames are dropped with a warning)
"""

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
CONFIG_PATH = DATA_DIR / "ai_cli_config.json"


TASKS = ("analysis", "wizard", "captions")

TASK_LABELS = {
    "analysis": "Video analysis",
    "wizard":   "Wizard reasoning",
    "captions": "Caption generation",
}

TASK_DESCRIPTIONS = {
    "analysis": (
        "Per-video tagging — picks scenes, energy, motion, dialog. Needs "
        "image (or video) understanding."
    ),
    "wizard": (
        "Strategy + reel composition — research best practices, pick scenes, "
        "decide on music and ordering. Pure text reasoning."
    ),
    "captions": (
        "Hooks, captions, hashtags. Short copywriting tasks."
    ),
}

# Default routing: analysis → Gemini (native video + audio), reasoning →
# Claude (better long-form reasoning + structured output).
TASK_DEFAULTS = {
    "analysis": "gemini",
    "wizard":   "claude",
    "captions": "claude",
}


PROVIDER_DEFAULTS = {
    "claude": {
        "bin": "claude",
        "model": "claude-haiku-4-5-20251001",
        "label": "Claude Code",
        "supports_images": True,
        "homepage": "https://github.com/anthropics/claude-code",
        # Common Anthropic model IDs the CLI accepts. Users can edit the
        # active "Default model" field freely — this list only seeds the
        # picker dropdown.
        "models": [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
    },
    "codex": {
        "bin": "codex",
        "model": "gpt-5",
        "label": "Codex CLI",
        "supports_images": False,
        "homepage": "https://github.com/openai/codex",
        "models": [
            "gpt-5",
            "gpt-5-codex",
            "gpt-5-mini",
            "o3",
        ],
    },
    "gemini": {
        "bin": "gemini",
        "model": "gemini-2.5-flash",
        "label": "Gemini CLI",
        "supports_images": True,  # via @file references in the prompt
        "homepage": "https://github.com/google-gemini/gemini-cli",
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ],
    },
}


# ── Config ───────────────────────────────────────────────────────────────────
#
# AI settings live inside the active brand profile (see app_config.py) under
# the "ai" key, so one Save on /settings persists *everything* and switching
# brand profiles also switches AI routing.

def _load_config():
    import app_config
    return app_config.get_ai_block()


def _save_config(cfg):
    import app_config
    app_config.set_ai_block(cfg)


def get_config():
    """Return the full user-facing config:

      {
        "tasks":     {"analysis": "gemini", "wizard": "claude", "captions": "claude"},
        "providers": {"claude": {bin, model, bin_found, ...}, ...},
        "task_meta": {"analysis": {label, description}, ...},
      }
    """
    raw = _load_config()

    # Resolve task → provider map. Legacy single-provider configs (older
    # versions of this file) are migrated by routing every task through the
    # legacy provider.
    legacy = raw.get("provider")
    raw_tasks = raw.get("tasks") or {}
    tasks = {}
    for t in TASKS:
        chosen = raw_tasks.get(t) or legacy or TASK_DEFAULTS[t]
        if chosen not in PROVIDER_DEFAULTS:
            chosen = TASK_DEFAULTS[t]
        tasks[t] = chosen

    providers = {}
    for key, defaults in PROVIDER_DEFAULTS.items():
        user = (raw.get("providers") or {}).get(key) or {}
        cfg = dict(defaults)
        cfg.update({k: v for k, v in user.items() if v not in (None, "")})
        bin_path = cfg.get("bin", "")
        resolved = shutil.which(bin_path) if bin_path else None
        cfg["bin_resolved"] = resolved
        cfg["bin_found"] = bool(resolved)
        providers[key] = cfg

    return {
        "tasks": tasks,
        "providers": providers,
        "task_meta": {
            t: {"label": TASK_LABELS[t], "description": TASK_DESCRIPTIONS[t]}
            for t in TASKS
        },
    }


def set_task_provider(task, provider_name):
    if task not in TASKS:
        raise ValueError(f"Unknown task: {task}")
    if provider_name not in PROVIDER_DEFAULTS:
        raise ValueError(f"Unknown provider: {provider_name}")
    cfg = _load_config()
    cfg.setdefault("tasks", {})
    cfg["tasks"][task] = provider_name
    # Strip legacy single-provider field once tasks are configured.
    cfg.pop("provider", None)
    _save_config(cfg)


def set_provider_settings(name, bin_path=None, model=None):
    if name not in PROVIDER_DEFAULTS:
        raise ValueError(f"Unknown provider: {name}")
    cfg = _load_config()
    cfg.setdefault("providers", {})
    cfg["providers"].setdefault(name, {})
    if bin_path is not None:
        cfg["providers"][name]["bin"] = bin_path.strip() or None
    if model is not None:
        cfg["providers"][name]["model"] = model.strip() or None
    _save_config(cfg)


def get_provider_for_task(task):
    if task not in TASKS:
        raise ValueError(f"Unknown task: {task}")
    return get_config()["tasks"][task]


# ── Dispatch ─────────────────────────────────────────────────────────────────

def call_ai(prompt_text, task="wizard", frames=None, model=None,
            timeout=300, on_log=None, provider=None):
    """Send *prompt_text* (and optionally *frames*) to the CLI configured for
    *task*. Returns the model's text response (or None on failure).

    *task*: one of "analysis" / "wizard" / "captions". Determines which
    provider is invoked, per the user's /settings page configuration.

    *frames*: list of (jpeg_bytes, label) tuples. If the resolved provider
    has no image support, frames are dropped with a single log warning.

    *model*: optional override; otherwise the resolved provider's configured
    model is used.

    *provider*: optional override that bypasses the task→provider mapping.
    Use when a single call needs to target a specific provider regardless of
    which one is configured for the task (e.g. user picks a model in a
    dropdown for one-off research refresh).

    *on_log*: optional callable(msg) for streaming status messages back to a
    caller's progress UI.
    """
    if task not in TASKS:
        raise ValueError(f"Unknown task: {task}")
    cfg = get_config()
    if provider:
        if provider not in PROVIDER_DEFAULTS:
            raise ValueError(f"Unknown provider: {provider}")
        name = provider
    else:
        name = cfg["tasks"][task]
    settings = cfg["providers"][name]
    log = on_log or (lambda m: None)

    if not settings["bin_found"]:
        log(f"AI CLI '{settings['bin']}' not found on PATH. "
            f"Install it or change provider on /settings.")
        return None

    use_model = (model or settings.get("model") or "").strip() or None

    if frames and not settings["supports_images"]:
        log(f"{settings['label']} does not support image input — running "
            f"text-only (analysis quality will degrade).")
        frames = None

    if name == "claude":
        return _call_claude(settings["bin_resolved"], prompt_text, frames,
                            use_model, timeout, log)
    if name == "codex":
        return _call_codex(settings["bin_resolved"], prompt_text, use_model,
                           timeout, log)
    if name == "gemini":
        return _call_gemini(settings["bin_resolved"], prompt_text, frames,
                            use_model, timeout, log)
    log(f"Unknown provider: {name}")
    return None


# ── claude ───────────────────────────────────────────────────────────────────

def _call_claude(bin_path, prompt_text, frames, model, timeout, log):
    """Claude Code CLI via stream-json protocol. Supports image frames."""
    content = []
    if frames:
        for jpeg_bytes, label in frames:
            content.append({"type": "text", "text": f"[Frame at {label}]"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(jpeg_bytes).decode(),
                },
            })
    content.append({"type": "text", "text": prompt_text})

    message = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    })

    cmd = [bin_path, "--print",
           "--input-format", "stream-json",
           "--output-format", "stream-json",
           "--verbose"]
    if model:
        cmd += ["--model", model]

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd, input=message, capture_output=True, text=True,
                timeout=timeout,
            )
            raw = result.stdout.strip()
            stderr = (result.stderr or "").strip()

            if result.returncode != 0 and not raw:
                err_msg = stderr[:200] or "unknown error"
                low = err_msg.lower()
                if "auth" in low or "login" in low or "api key" in low:
                    log("Claude CLI not authenticated. Run 'claude' in "
                        "Terminal to sign in.")
                    return None
                log(f"Claude CLI error: {err_msg}")
                if attempt < max_retries:
                    log(f"Retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(5)
                    continue
                return None

            text = ""
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "assistant":
                    continue
                c = msg.get("message", {}).get("content", "")
                if isinstance(c, list):
                    t = " ".join(
                        b.get("text", "") for b in c
                        if b.get("type") == "text" and b.get("text", "").strip()
                    )
                    if t.strip():
                        text = t
                elif isinstance(c, str) and c.strip():
                    text = c
            if text:
                return text
            if attempt < max_retries:
                log(f"Empty response, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(5)
        except Exception as exc:
            if attempt < max_retries:
                log(f"Attempt failed ({exc}), retrying ({attempt + 1}/{max_retries})...")
                time.sleep(5)
            else:
                raise
    return None


# ── codex (text-only) ────────────────────────────────────────────────────────

def _call_codex(bin_path, prompt_text, model, timeout, log):
    """OpenAI Codex CLI in non-interactive `exec` mode. Plain text in/out."""
    cmd = [bin_path, "exec"]
    if model:
        cmd += ["--model", model]
    cmd.append("-")  # explicit stdin marker; codex tolerates omission too
    try:
        result = subprocess.run(
            cmd, input=prompt_text, capture_output=True, text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        log(f"Codex CLI not found at {bin_path}")
        return None
    if result.returncode != 0:
        err = (result.stderr or "").strip()[:200]
        low = err.lower()
        if "auth" in low or "login" in low or "api key" in low:
            log("Codex CLI not authenticated. Run 'codex login' in Terminal.")
        else:
            log(f"Codex CLI error: {err or 'unknown error'}")
        return None
    text = (result.stdout or "").strip()
    return text or None


# ── gemini ───────────────────────────────────────────────────────────────────

def _call_gemini(bin_path, prompt_text, frames, model, timeout, log):
    """Google Gemini CLI: non-interactive prompt via -p flag.

    When *frames* are supplied, each frame is written to a temporary JPEG and
    referenced from the prompt with Gemini's @file syntax — the CLI inlines
    them as multimodal parts. Cleanup happens whether the call succeeds or
    fails.
    """
    import tempfile
    cmd = [bin_path]
    if model:
        cmd += ["-m", model]

    if frames:
        tmpdir = tempfile.mkdtemp(prefix="pg_gemini_")
        try:
            refs = []
            for i, (jpeg_bytes, label) in enumerate(frames):
                fn = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
                with open(fn, "wb") as fh:
                    fh.write(jpeg_bytes)
                refs.append(f"[Frame at {label}] @{fn}")
            full_prompt = "\n".join(refs) + "\n\n" + prompt_text
            cmd += ["-p", full_prompt]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )
            except FileNotFoundError:
                log(f"Gemini CLI not found at {bin_path}")
                return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        cmd += ["-p", prompt_text]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            log(f"Gemini CLI not found at {bin_path}")
            return None

    if result.returncode != 0:
        err = (result.stderr or "").strip()[:200]
        low = err.lower()
        if "auth" in low or "login" in low or "api key" in low:
            log("Gemini CLI not authenticated. Run 'gemini auth' in Terminal.")
        else:
            log(f"Gemini CLI error: {err or 'unknown error'}")
        return None
    text = (result.stdout or "").strip()
    # Strip leading "Loaded cached credentials." style noise some versions emit.
    text = re.sub(r"^[A-Z][^\n]*credentials\.\s*\n+", "", text)
    return text or None
