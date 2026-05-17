"""app_config.py — Multi-brand profile config.

Each brand lives in its own profile file at ``data/profiles/<name>.json``
that captures EVERY user-facing setting:

  - brand_name, social_handle, content_domain
  - tag_schema (used by analyzer.py)
  - source_folder (videos/), output_folder (output/)
  - ai: { tasks, providers }   ← used by ai_cli.py

The active profile is tracked at ``data/active_profile.json``. Switching
between brands is one call to :func:`load_profile`.

Profile filename = brand name by default; users can rename via the
"Settings file name" input on /settings (handled in settings_routes.py).
"""

import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
# Profile JSONs live alongside each profile's Input/Output folders under
# ~/Documents/ClipBuilder/ so a user can back up or move their brand
# config + working files as one tree, independent of the app install.
PROFILES_DIR = Path.home() / "Documents" / "ClipBuilder"
# Legacy in-repo profiles directory; migrated on first read (see
# _migrate_profiles_to_documents).
LEGACY_PROFILES_DIR = DATA_DIR / "profiles"
ACTIVE_PATH = DATA_DIR / "active_profile.json"

# App-level (profile-independent) settings live in their own file so that
# switching brand profiles doesn't change which AI provider is used, what
# analysis mode runs, or which Whisper model is loaded. The settings page
# splits these out under its "General" tab.
APP_SETTINGS_PATH = DATA_DIR / "app_settings.json"
GENERAL_KEYS = (
    "analysis_mode",
    "transcribe_provider", "transcribe_model",
    "transcribe_hint",
    "whisper_model", "whisper_language", "whisper_translate",
    "ai",
    "theme",
)

# Legacy paths — read once on first run for migration, then ignored.
LEGACY_APP_CONFIG = DATA_DIR / "app_config.json"
LEGACY_AI_CONFIG = DATA_DIR / "ai_cli_config.json"


# ── Built-in defaults ───────────────────────────────────────────────────────
# Only used for the legacy one-shot migration in _migrate_legacy() and as
# placeholders for fields a new blank profile leaves empty. New profiles
# created via the /settings UI always set their own values.

DEFAULT_BRAND_NAME = "ClipBuilder"
# Name of the always-present, undeletable fallback profile. Auto-created
# (and auto-restored if its JSON is deleted or unreadable) so the app
# always has *something* to load even if the user trashes everything.
DEFAULT_PROFILE_NAME = "Default"
DEFAULT_SOCIAL_HANDLE = ""
DEFAULT_CONTENT_DOMAIN = ""
DEFAULT_SOURCE_FOLDER = "~/Documents/ClipBuilder/Default/Input"
DEFAULT_OUTPUT_FOLDER = "~/Documents/ClipBuilder/Default/Output"

# Analysis mode — drives how the analyzer breaks each video into scenes.
#   "visual" — default; samples frames + asks AI for tag time-ranges.
#              Best for action / sports / motion-heavy content.
#   "speech" — runs local Whisper on the audio, uses transcript segments
#              as scene boundaries, sends frame + spoken text to the AI
#              for per-scene tagging. Best for tutorials, interviews,
#              podcasts, talking-head content.
DEFAULT_ANALYSIS_MODE = "visual"
ANALYSIS_MODES = ("visual", "speech")
DEFAULT_WHISPER_MODEL = "base"
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_WHISPER_LANGUAGE = ""  # empty = auto-detect
DEFAULT_WHISPER_TRANSLATE = False  # if True, transcript is forced to English

# Speech-mode transcription provider. "whisper" runs locally via
# faster-whisper; "openai" and "gemini" call the respective cloud APIs
# (keys read from the environment). Model menus and defaults live in
# transcription.MODELS / transcription.DEFAULT_MODEL.
DEFAULT_TRANSCRIBE_PROVIDER = "whisper"
TRANSCRIBE_PROVIDERS = ("whisper", "openai", "gemini")

# UI theme. "default" keeps the existing dark / red-accent look. "art_deco"
# repaints the entire app in gold-on-black art-deco styling inspired by
# the ClipBuilder logo (Poiret One headings, copper/gold accents, etched
# borders). Applied via a global CSS overlay injected by chrome.py.
DEFAULT_THEME = "default"
THEMES = ("default", "art_deco")

SOCIAL_PLATFORMS = ("instagram", "tiktok", "youtube")

# Defaults for the burn-in caption style used by AI Builder. Persisted with
# the brand profile so each brand can have its own caption look.
DEFAULT_CAPTIONS = {
    "font":     "sans",       # sans | serif | mono
    "color":    "#ffffff",
    "bg_on":    False,
    "bg_color": "#000000",
    "position": "bottom",     # bottom | middle | top
}
CAPTION_FONTS    = ("sans", "serif", "mono")
CAPTION_POSITIONS = ("bottom", "middle", "top")


def _validate_hex_color(value):
    """Normalize a hex color string to ``#rrggbb`` lowercase, or return
    empty string for anything that doesn't parse. Accepts ``#rgb``,
    ``#rrggbb``, and bare-hex versions (no leading #)."""
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if not s.startswith("#"):
        s = "#" + s
    if re.match(r"^#[0-9a-fA-F]{3}$", s):
        # Expand shorthand #rgb → #rrggbb
        s = "#" + "".join(ch * 2 for ch in s[1:])
    if re.match(r"^#[0-9a-fA-F]{6}$", s):
        return s.lower()
    return ""


def _validate_captions(value):
    """Normalize the captions sub-config. Falls back to DEFAULT_CAPTIONS for
    any missing/invalid field so the UI always has every key to render."""
    if not isinstance(value, dict):
        return deepcopy(DEFAULT_CAPTIONS)
    def _color(v, fallback):
        v = str(v or "").strip()
        return v if re.match(r"^#[0-9a-fA-F]{6}$", v) else fallback
    font = (str(value.get("font") or "").strip().lower() or DEFAULT_CAPTIONS["font"])
    if font not in CAPTION_FONTS:
        font = DEFAULT_CAPTIONS["font"]
    pos  = (str(value.get("position") or "").strip().lower() or DEFAULT_CAPTIONS["position"])
    if pos not in CAPTION_POSITIONS:
        pos = DEFAULT_CAPTIONS["position"]
    return {
        "font":     font,
        "color":    _color(value.get("color"),    DEFAULT_CAPTIONS["color"]),
        "bg_on":    bool(value.get("bg_on", False)),
        "bg_color": _color(value.get("bg_color"), DEFAULT_CAPTIONS["bg_color"]),
        "position": pos,
    }


def _validate_socials(value):
    """Sanitize a socials dict. Accepts unknown platforms but normalizes the
    known ones so the UI always has fields to render.

    Each slot has: handle, url, cookies (browser name or path to cookies.txt).
    """
    if not isinstance(value, dict):
        return None
    def _slot(s):
        s = s or {}
        if not isinstance(s, dict):
            s = {}
        return {
            "handle":  str(s.get("handle")  or "").strip(),
            "url":     str(s.get("url")     or "").strip(),
            "cookies": str(s.get("cookies") or "").strip(),
        }
    out = {plat: _slot(value.get(plat)) for plat in SOCIAL_PLATFORMS}
    # Pass through any extra platforms the user added by hand.
    for k, v in value.items():
        if k in SOCIAL_PLATFORMS or not isinstance(v, dict):
            continue
        out[k] = _slot(v)
    return out


DEFAULT_SOCIALS = {p: {"handle": "", "url": "", "cookies": ""}
                   for p in SOCIAL_PLATFORMS}


DEFAULT_TAGS = {
    "activity": [
        "grappling", "striking", "punching", "kicking", "takedown", "submission",
        "ground-and-pound", "clinch", "sprawl", "guard-pass", "sweep", "mount",
        "back-control", "arm-bar", "choke", "triangle", "knee-bar", "leg-lock",
        "wrestling", "judo-throw", "elbow", "knee-strike",
        "training", "sparring", "drilling", "pad-work", "bag-work", "warm-up",
        "stretching", "conditioning", "weightlifting", "running",
        "interview", "press-conference", "weigh-in", "face-off",
        "walkout", "entrance", "celebration", "corner-advice",
        "crowd", "audience-reaction", "referee", "judges",
        "promo", "graphic", "text-overlay", "logo", "intro", "outro",
        "behind-the-scenes", "travel", "eating", "lifestyle",
        "slow-motion", "replay", "highlight-reel", "talking", "posing", "photo",
    ],
    "setting": [
        "octagon", "cage", "ring", "gym", "outdoor", "beach", "street", "hotel",
        "arena", "backstage", "locker-room", "studio",
    ],
    "camera": [
        "close-up", "medium-shot", "wide-shot", "overhead", "pov", "handheld",
        "steady", "tracking", "slow-pan",
    ],
    "energy": [
        "high-energy", "medium-energy", "low-energy",
    ],
    "quality": [
        "low-quality",
    ],
}


# ── File helpers ────────────────────────────────────────────────────────────

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_\-. ]")


def _sanitize_name(name):
    name = (name or "").strip()
    name = _FILENAME_SAFE.sub("_", name)
    return name or "default"


def _profile_path(name):
    return PROFILES_DIR / (_sanitize_name(name) + ".json")


def _migrate_profiles_to_documents():
    """One-shot copy of legacy in-repo profile JSONs to ~/Documents/ClipBuilder.

    Idempotent: only copies files whose destination doesn't already exist,
    and leaves the legacy files in place (non-destructive — the user can
    delete data/profiles/ themselves once they've verified the move).
    """
    if not LEGACY_PROFILES_DIR.exists():
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    for src in LEGACY_PROFILES_DIR.glob("*.json"):
        dst = PROFILES_DIR / src.name
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass


def _blank_profile_dict(name):
    """Shape used for both `Default` auto-restore and `create_blank_profile`."""
    return {
        "profile_name":   name,
        "brand_name":     name,
        "content_domain": "",
        "source_folder":  f"~/Documents/ClipBuilder/{name}/Input",
        "output_folder":  f"~/Documents/ClipBuilder/{name}/Output",
        "tag_schema":     {},
        "socials":        deepcopy(DEFAULT_SOCIALS),
    }


def _ensure_default_profile():
    """Guarantee ``~/Documents/ClipBuilder/Default.json`` exists and is
    valid JSON. Rewrites a blank one if it's missing or corrupt.

    Called from list_profiles() so the Default always appears in the
    picker and the user can never end up with zero profiles.
    """
    p = _profile_path(DEFAULT_PROFILE_NAME)
    needs_write = True
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                needs_write = False
        except Exception:
            needs_write = True   # corrupt — overwrite with a blank shell
    if needs_write:
        _write_profile_raw(DEFAULT_PROFILE_NAME,
                           _blank_profile_dict(DEFAULT_PROFILE_NAME))


def list_profiles():
    """Return sorted list of available profile names (filenames sans .json)."""
    _migrate_profiles_to_documents()
    _ensure_default_profile()
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


# ── Active profile pointer ──────────────────────────────────────────────────

def get_active_profile_name():
    """Return the active profile name, falling back to first available
    or the default brand if none exist yet."""
    if ACTIVE_PATH.exists():
        try:
            data = json.loads(ACTIVE_PATH.read_text())
            n = (data.get("profile") or "").strip()
            if n and _profile_path(n).exists():
                return n
        except Exception:
            pass
    profiles = list_profiles()
    if profiles:
        return profiles[0]
    return DEFAULT_BRAND_NAME


def _set_active(name):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(json.dumps({"profile": _sanitize_name(name)}, indent=2))


def load_profile(name):
    """Switch the active profile. Errors if the profile doesn't exist."""
    if not _profile_path(name).exists():
        raise FileNotFoundError(f"No profile named '{name}'")
    _set_active(name)


def create_blank_profile(name):
    """Create a fresh profile with empty content_domain/tag_schema and
    per-profile Documents folders, then make it active.

    Raises ValueError if *name* is empty or already taken. Writes the JSON
    directly so the strict ``_validate_tags`` rule (no empty schemas) doesn't
    reject the deliberately-blank tag dict.
    """
    clean = _sanitize_name((name or "").strip())
    if not clean:
        raise ValueError("Profile name is required.")
    if _profile_path(clean).exists():
        raise ValueError(
            f"A profile named '{clean}' already exists. "
            f"Load it from the picker, or pick a different name."
        )
    _write_profile_raw(clean, _blank_profile_dict(clean))
    _set_active(clean)
    return clean


def delete_profile(name):
    """Delete a profile (config file + its SQLite database). Picks a
    fallback active profile if the deleted one was active.

    Refuses to delete the always-on ``Default`` profile — it's the
    guaranteed-present fallback the app re-creates on next read.
    """
    if _sanitize_name(name) == DEFAULT_PROFILE_NAME:
        raise ValueError(
            f"The '{DEFAULT_PROFILE_NAME}' profile cannot be deleted."
        )
    p = _profile_path(name)
    if p.exists():
        p.unlink()
    # Also drop the per-profile DB so switching back later doesn't surface
    # stale rows.
    try:
        import db
        db.delete_profile_db(name)
    except Exception:
        pass
    # And the per-profile folders/memberships store so the Folders column
    # doesn't inherit the deleted profile's layout on its next creation.
    try:
        import folders as _folders_mod
        fp = _folders_mod._FOLDERS_DIR / (
            _folders_mod._safe_profile_name(name) + ".json"
        )
        if fp.exists():
            fp.unlink()
    except Exception:
        pass
    if get_active_profile_name() == name:
        remaining = list_profiles()
        if remaining:
            _set_active(remaining[0])


# ── Read / write the active profile ─────────────────────────────────────────

def _read_profile_raw(name):
    p = _profile_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_profile_raw(name, data):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _profile_path(name).write_text(json.dumps(data, indent=2))
    _ensure_profile_folders(data)


def _ensure_profile_folders(data):
    """Create the profile's source/output folders if they don't exist.

    Called from every profile write so a freshly-saved profile is
    immediately usable (Analyze/Library pages can scan their folders
    without the user manually creating directories first). Silent on
    permission errors — the UI will surface the missing-folder problem
    on its own when the user tries to use it.
    """
    for key in ("source_folder", "output_folder"):
        p = (data.get(key) or "").strip()
        if not p:
            continue
        try:
            Path(os.path.expandvars(os.path.expanduser(p))).mkdir(
                parents=True, exist_ok=True
            )
        except Exception:
            pass


def _migrate_legacy():
    """First-run combine of the old split configs into one profile dict.
    Idempotent because we only run when no profiles exist yet."""
    out = {}
    if LEGACY_APP_CONFIG.exists():
        try:
            data = json.loads(LEGACY_APP_CONFIG.read_text())
            for k in ("brand_name", "social_handle", "content_domain",
                      "tag_schema"):
                if k in data and data[k]:
                    out[k] = data[k]
        except Exception:
            pass
    if LEGACY_AI_CONFIG.exists():
        try:
            data = json.loads(LEGACY_AI_CONFIG.read_text())
            ai_block = {}
            for k in ("tasks", "providers", "provider"):
                if k in data:
                    ai_block[k] = data[k]
            if ai_block:
                out["ai"] = ai_block
        except Exception:
            pass
    return out


def _ensure_profile():
    """Idempotently make sure at least one profile exists on disk and the
    active pointer points to it. Migrates legacy files on first run."""
    if list_profiles():
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    legacy = _migrate_legacy()
    name = (legacy.get("brand_name") or DEFAULT_BRAND_NAME).strip()
    legacy["profile_name"] = name
    _write_profile_raw(name, legacy)
    _set_active(name)


def _fill_defaults(raw, profile_name):
    """Apply built-in defaults for any missing field.

    Legacy migration: if the profile JSON still has a top-level
    ``social_handle`` (from before the per-platform Social Channels block),
    fold it into ``socials.instagram.handle`` so the AI prompts keep
    receiving a useful value via ``domain_vars()``.
    """
    socials = _validate_socials(raw.get("socials")) or deepcopy(DEFAULT_SOCIALS)
    legacy_handle = (raw.get("social_handle") or "").strip()
    if legacy_handle and not socials.get("instagram", {}).get("handle"):
        socials["instagram"]["handle"] = legacy_handle

    mode = (raw.get("analysis_mode") or "").strip().lower()
    if mode not in ANALYSIS_MODES:
        mode = DEFAULT_ANALYSIS_MODE
    whisper_model = (raw.get("whisper_model") or "").strip()
    if whisper_model not in WHISPER_MODELS:
        whisper_model = DEFAULT_WHISPER_MODEL
    transcribe_provider = (raw.get("transcribe_provider") or "").strip().lower()
    if transcribe_provider not in TRANSCRIBE_PROVIDERS:
        transcribe_provider = DEFAULT_TRANSCRIBE_PROVIDER
    transcribe_model = (raw.get("transcribe_model") or "").strip()
    theme = (raw.get("theme") or "").strip().lower()
    if theme not in THEMES:
        theme = DEFAULT_THEME
    return {
        "profile_name":   (raw.get("profile_name") or profile_name or "").strip()
                          or DEFAULT_BRAND_NAME,
        "brand_name":     (raw.get("brand_name") or "").strip()
                          or DEFAULT_BRAND_NAME,
        # If the profile JSON has the key at all (even empty string), honor
        # the user's choice — a blank content_domain means "don't seed any
        # niche language into prompts." Only fall back to the default when
        # the key is missing entirely (legacy profiles pre-dating this UI).
        "content_domain": ((raw.get("content_domain") or "").strip()
                           if "content_domain" in raw
                           else DEFAULT_CONTENT_DOMAIN),
        "source_folder":  (raw.get("source_folder") or "").strip()
                          or DEFAULT_SOURCE_FOLDER,
        "output_folder":  (raw.get("output_folder") or "").strip()
                          or DEFAULT_OUTPUT_FOLDER,
        # Per-brand intro/outro override. Empty = fall back to the legacy
        # assets/videos/intro* scan in find_asset().
        "intro_video":    (raw.get("intro_video") or "").strip(),
        "outro_video":    (raw.get("outro_video") or "").strip(),
        # Brand accent color (#RRGGBB) — extracted by the Settings Wizard
        # from the brand's website and used for the per-profile pill in
        # the header. Empty falls back to the app's default purple.
        "brand_color":    _validate_hex_color(raw.get("brand_color"))
                          or "",
        # Same explicit-key rule as content_domain: a profile that stored
        # tag_schema={} on purpose (blank profile) should stay blank rather
        # than getting seeded with the MMA defaults.
        "tag_schema":     (_validate_tags(raw.get("tag_schema")) or {}
                           if "tag_schema" in raw
                           else deepcopy(DEFAULT_TAGS)),
        "socials":        socials,
        "ai":             raw.get("ai") or {},
        "analysis_mode":       mode,
        "transcribe_provider": transcribe_provider,
        "transcribe_model":    transcribe_model,
        "transcribe_hint":     (raw.get("transcribe_hint") or "").strip(),
        "whisper_model":       whisper_model,
        "whisper_language":    (raw.get("whisper_language") or "").strip(),
        "whisper_translate":   bool(raw.get("whisper_translate", False)),
        "theme":               theme,
        "captions":            _validate_captions(raw.get("captions")),
    }


def _read_app_settings():
    """Raw read of the app-level settings file. Returns ``{}`` if missing."""
    try:
        if APP_SETTINGS_PATH.exists():
            return json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_app_settings(data):
    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _migrate_general_from_profile_once():
    """First-run migration: pull any general (app-wide) fields out of
    every existing brand profile into ``app_settings.json``, then strip
    them from the profile JSONs so future saves don't drift apart.

    Idempotent: if the app-settings file already exists, this is a no-op.
    The active profile's values win over older ones (first-write wins is
    fine because we never re-migrate after this).
    """
    if APP_SETTINGS_PATH.exists():
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    collected = {}
    active = ""
    try:
        if ACTIVE_PATH.exists():
            active = (json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
                      .get("active") or "")
    except Exception:
        active = ""
    # Walk profiles, preferring the active one for any conflicting fields.
    candidates = []
    if active:
        candidates.append(active)
    for p in PROFILES_DIR.glob("*.json"):
        if p.stem not in candidates:
            candidates.append(p.stem)
    for name in candidates:
        try:
            raw = json.loads((_profile_path(name)).read_text(encoding="utf-8"))
        except Exception:
            continue
        for k in GENERAL_KEYS:
            if k in raw and k not in collected:
                collected[k] = raw[k]
    _write_app_settings(collected)
    # Now strip the lifted keys from every profile JSON so the source of
    # truth lives only in app_settings.json going forward.
    for p in PROFILES_DIR.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        before = dict(raw)
        for k in GENERAL_KEYS:
            raw.pop(k, None)
        if raw != before:
            p.write_text(json.dumps(raw, indent=2, ensure_ascii=False),
                         encoding="utf-8")


def get_app_settings():
    """Return the app-level (profile-independent) settings dict with
    defaults filled in for any missing field. Source of truth for
    ``analysis_mode`` / ``whisper_*`` / ``ai``."""
    _migrate_general_from_profile_once()
    raw = _read_app_settings() or {}
    mode = (raw.get("analysis_mode") or "").strip().lower()
    if mode not in ANALYSIS_MODES:
        mode = DEFAULT_ANALYSIS_MODE
    whisper_model = (raw.get("whisper_model") or "").strip()
    if whisper_model not in WHISPER_MODELS:
        whisper_model = DEFAULT_WHISPER_MODEL
    transcribe_provider = (raw.get("transcribe_provider") or "").strip().lower()
    if transcribe_provider not in TRANSCRIBE_PROVIDERS:
        transcribe_provider = DEFAULT_TRANSCRIBE_PROVIDER
    transcribe_model = (raw.get("transcribe_model") or "").strip()
    theme = (raw.get("theme") or "").strip().lower()
    if theme not in THEMES:
        theme = DEFAULT_THEME
    return {
        "analysis_mode":       mode,
        "transcribe_provider": transcribe_provider,
        "transcribe_model":    transcribe_model,
        "transcribe_hint":     (raw.get("transcribe_hint") or "").strip(),
        "whisper_model":       whisper_model,
        "whisper_language":    (raw.get("whisper_language") or "").strip(),
        "whisper_translate":   bool(raw.get("whisper_translate", False)),
        "theme":               theme,
        "ai":                  raw.get("ai") or {},
    }


def set_app_settings(**fields):
    """Partial update of the app-level settings file. Only keys in
    ``GENERAL_KEYS`` are accepted; everything else is ignored."""
    _migrate_general_from_profile_once()
    raw = _read_app_settings() or {}
    if "analysis_mode" in fields and fields["analysis_mode"] is not None:
        m = (fields["analysis_mode"] or "").strip().lower()
        if m and m not in ANALYSIS_MODES:
            raise ValueError(
                f"analysis_mode must be one of {ANALYSIS_MODES}, got {m!r}"
            )
        raw["analysis_mode"] = m or DEFAULT_ANALYSIS_MODE
    if "whisper_model" in fields and fields["whisper_model"] is not None:
        m = (fields["whisper_model"] or "").strip()
        if m and m not in WHISPER_MODELS:
            raise ValueError(
                f"whisper_model must be one of {WHISPER_MODELS}, got {m!r}"
            )
        raw["whisper_model"] = m or DEFAULT_WHISPER_MODEL
    if "whisper_language" in fields and fields["whisper_language"] is not None:
        raw["whisper_language"] = (fields["whisper_language"] or "").strip()
    if "whisper_translate" in fields and fields["whisper_translate"] is not None:
        raw["whisper_translate"] = bool(fields["whisper_translate"])
    if "transcribe_provider" in fields and fields["transcribe_provider"] is not None:
        p = (fields["transcribe_provider"] or "").strip().lower()
        if p and p not in TRANSCRIBE_PROVIDERS:
            raise ValueError(
                f"transcribe_provider must be one of {TRANSCRIBE_PROVIDERS}, "
                f"got {p!r}"
            )
        raw["transcribe_provider"] = p or DEFAULT_TRANSCRIBE_PROVIDER
    if "transcribe_model" in fields and fields["transcribe_model"] is not None:
        raw["transcribe_model"] = (fields["transcribe_model"] or "").strip()
    if "transcribe_hint" in fields and fields["transcribe_hint"] is not None:
        raw["transcribe_hint"] = (fields["transcribe_hint"] or "").strip()
    if "theme" in fields and fields["theme"] is not None:
        t = (fields["theme"] or "").strip().lower()
        if t and t not in THEMES:
            raise ValueError(f"theme must be one of {THEMES}, got {t!r}")
        raw["theme"] = t or DEFAULT_THEME
    if "ai" in fields and fields["ai"] is not None:
        if not isinstance(fields["ai"], dict):
            raise ValueError("ai must be a dict")
        raw["ai"] = fields["ai"]
    _write_app_settings(raw)


def get_config():
    """Return the active profile with defaults filled in, overlaid with
    the app-level (profile-independent) general settings — so legacy
    readers like ``cfg.get('analysis_mode')`` keep working without
    knowing about the storage split."""
    _ensure_profile()
    name = get_active_profile_name()
    raw = _read_profile_raw(name)
    cfg = _fill_defaults(raw, name)
    app = get_app_settings()
    # General settings win over any stale copies still embedded in the
    # profile (e.g. older saves before the split). app_settings.json is
    # the single source of truth for these keys.
    cfg.update(app)
    return cfg


def set_config(**fields):
    """Partial update of the active profile.

    Profile-scoped keys (brand_name, content_domain, source_folder,
    output_folder, intro_video, outro_video, tag_schema, socials,
    captions, profile_name) update the active profile JSON.

    General/app-wide keys (analysis_mode, whisper_*, ai) are silently
    forwarded to :func:`set_app_settings` so they never get persisted
    inside the profile file — switching brand profiles no longer
    swaps which AI provider or Whisper model is used.
    """
    _ensure_profile()

    # Route general keys to the app-level store. Don't let them touch the
    # profile JSON at all — that's what was previously causing the AI /
    # Whisper config to follow whichever brand profile was active.
    general_in = {k: fields[k] for k in GENERAL_KEYS
                  if k in fields and fields[k] is not None}
    if general_in:
        set_app_settings(**general_in)

    current_name = get_active_profile_name()
    raw = _read_profile_raw(current_name)

    # Drop the deprecated top-level social_handle; primary handle now lives
    # in socials.instagram.handle (migrated on load by _fill_defaults).
    raw.pop("social_handle", None)
    # Belt-and-suspenders: in case a profile still has these from before
    # the split, scrub them on every save so they can't drift back.
    for k in GENERAL_KEYS:
        raw.pop(k, None)

    for k in ("brand_name", "content_domain",
              "source_folder", "output_folder",
              "intro_video", "outro_video"):
        if k in fields and fields[k] is not None:
            raw[k] = (fields[k] or "").strip()

    if "brand_color" in fields and fields["brand_color"] is not None:
        normalized = _validate_hex_color(fields["brand_color"])
        if fields["brand_color"] and not normalized:
            raise ValueError(
                "brand_color must be a hex color like '#1f4cff' "
                "(got %r)" % fields["brand_color"]
            )
        raw["brand_color"] = normalized

    if "tag_schema" in fields and fields["tag_schema"] is not None:
        incoming = fields["tag_schema"]
        # An explicit empty dict means "blank schema" (e.g. a brand-new
        # profile the user hasn't filled in yet). Persist it as {} instead
        # of rejecting — _fill_defaults honors the explicit empty.
        if isinstance(incoming, dict) and not incoming:
            raw["tag_schema"] = {}
        else:
            validated = _validate_tags(incoming)
            if validated is None:
                raise ValueError(
                    "tag_schema must be a dict of category → list of tag strings"
                )
            raw["tag_schema"] = validated

    if "socials" in fields and fields["socials"] is not None:
        validated = _validate_socials(fields["socials"])
        if validated is None:
            raise ValueError(
                "socials must be a dict of platform → {handle, url}"
            )
        raw["socials"] = validated

    if "captions" in fields and fields["captions"] is not None:
        raw["captions"] = _validate_captions(fields["captions"])

    # Handle rename / fork last so we know which path to write to.
    new_name = fields.get("profile_name")
    if new_name is not None:
        new_name = _sanitize_name(new_name)
        raw["profile_name"] = new_name
        if new_name != current_name:
            # NON-DESTRUCTIVE: save into the new file and switch active to it,
            # but leave the old profile on disk so wizard runs / brand forks
            # never destroy the previous brand's settings. Use delete_profile()
            # via the trash button to remove a profile explicitly.
            new_path = _profile_path(new_name)
            if new_path.exists() and new_path != _profile_path(current_name):
                # Overwriting an existing different profile is also a likely
                # accident — refuse so the user picks a unique name or loads
                # the existing one first.
                raise ValueError(
                    f"A profile named '{new_name}' already exists. "
                    f"Load it from the picker, or pick a different name."
                )
            _write_profile_raw(new_name, raw)
            _set_active(new_name)
            return
    else:
        raw.setdefault("profile_name", current_name)

    _write_profile_raw(current_name, raw)


def _validate_tags(schema):
    if not isinstance(schema, dict) or not schema:
        return None
    out = {}
    for cat, tags in schema.items():
        if not isinstance(cat, str) or not isinstance(tags, list):
            return None
        clean = [str(t).strip() for t in tags if str(t).strip()]
        if not clean:
            continue
        out[cat.strip()] = clean
    return out or None


# ── AI sub-config (reused by ai_cli.py) ─────────────────────────────────────

def get_ai_block():
    """Return the raw 'ai' sub-config (may be empty). This lives at the
    app level (``app_settings.json``) so the configured providers don't
    change when you switch brand profiles. ai_cli.py merges this with
    its own defaults."""
    return get_app_settings().get("ai") or {}


def set_ai_block(ai_dict):
    """Replace the 'ai' sub-config at the app level."""
    set_app_settings(ai=ai_dict)


# ── Convenience helpers ─────────────────────────────────────────────────────

def get_tag_schema():
    return get_config()["tag_schema"]


def get_all_tags():
    return [t for tags in get_tag_schema().values() for t in tags]


def get_all_tag_set():
    return set(get_all_tags())


def domain_vars():
    """Variables for AI prompt formatting.

    {handle} resolves from the Social Channels block — IG first (Reels are
    Instagram-first), then TikTok, then YouTube, then empty. Use whichever
    is set so the AI always has something concrete to reference in captions.
    """
    c = get_config()
    socials = c.get("socials") or {}
    handle = ""
    for plat in ("instagram", "tiktok", "youtube"):
        h = ((socials.get(plat) or {}).get("handle") or "").strip()
        if h:
            handle = h
            break
    return {
        "brand":  c["brand_name"],
        "handle": handle,
        "domain": c["content_domain"],
        "brand_color": c.get("brand_color") or "",
    }


def get_source_dir():
    """Resolve the configured source folder to an absolute Path.
    Expands ``~``/``$HOME`` style paths first so a profile saved with
    ``~/Documents/...`` doesn't get mistaken for a relative path and
    resolved under the project root. Other relative paths are still
    resolved relative to the project root."""
    p = Path(os.path.expandvars(os.path.expanduser(get_config()["source_folder"])))
    return p if p.is_absolute() else (ROOT_DIR / p)


def get_output_dir():
    p = Path(os.path.expandvars(os.path.expanduser(get_config()["output_folder"])))
    return p if p.is_absolute() else (ROOT_DIR / p)
