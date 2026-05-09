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
import re
import shutil
from copy import deepcopy
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
ACTIVE_PATH = DATA_DIR / "active_profile.json"

# Legacy paths — read once on first run for migration, then ignored.
LEGACY_APP_CONFIG = DATA_DIR / "app_config.json"
LEGACY_AI_CONFIG = DATA_DIR / "ai_cli_config.json"


# ── Built-in defaults (preserve current PeaceGrappler/MMA behavior) ─────────

DEFAULT_BRAND_NAME = "PeaceGrappler"
DEFAULT_SOCIAL_HANDLE = "@peacegrappler"
DEFAULT_CONTENT_DOMAIN = "MMA / combat sports"
DEFAULT_SOURCE_FOLDER = "videos"
DEFAULT_OUTPUT_FOLDER = "output"

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


def list_profiles():
    """Return sorted list of available profile names (filenames sans .json)."""
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


def delete_profile(name):
    """Delete a profile (config file + its SQLite database). Picks a
    fallback active profile if the deleted one was active."""
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
    """Apply built-in defaults for any missing field."""
    return {
        "profile_name":   (raw.get("profile_name") or profile_name or "").strip()
                          or DEFAULT_BRAND_NAME,
        "brand_name":     (raw.get("brand_name") or "").strip()
                          or DEFAULT_BRAND_NAME,
        "social_handle":  (raw.get("social_handle") or "").strip()
                          or DEFAULT_SOCIAL_HANDLE,
        "content_domain": (raw.get("content_domain") or "").strip()
                          or DEFAULT_CONTENT_DOMAIN,
        "source_folder":  (raw.get("source_folder") or "").strip()
                          or DEFAULT_SOURCE_FOLDER,
        "output_folder":  (raw.get("output_folder") or "").strip()
                          or DEFAULT_OUTPUT_FOLDER,
        "tag_schema":     _validate_tags(raw.get("tag_schema"))
                          or deepcopy(DEFAULT_TAGS),
        "ai":             raw.get("ai") or {},
    }


def get_config():
    """Return the active profile, fully populated with defaults."""
    _ensure_profile()
    name = get_active_profile_name()
    raw = _read_profile_raw(name)
    return _fill_defaults(raw, name)


def set_config(**fields):
    """Partial update of the active profile.

    Supported keys: brand_name, social_handle, content_domain, source_folder,
    output_folder, tag_schema, ai, profile_name.

    If *profile_name* changes, the underlying file is renamed and the active
    pointer is updated to follow.
    """
    _ensure_profile()
    current_name = get_active_profile_name()
    raw = _read_profile_raw(current_name)

    for k in ("brand_name", "social_handle", "content_domain",
              "source_folder", "output_folder"):
        if k in fields and fields[k] is not None:
            raw[k] = (fields[k] or "").strip()

    if "tag_schema" in fields and fields["tag_schema"] is not None:
        validated = _validate_tags(fields["tag_schema"])
        if validated is None:
            raise ValueError(
                "tag_schema must be a dict of category → list of tag strings"
            )
        raw["tag_schema"] = validated

    if "ai" in fields and fields["ai"] is not None:
        if not isinstance(fields["ai"], dict):
            raise ValueError("ai must be a dict")
        raw["ai"] = fields["ai"]

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
    """Return the raw 'ai' sub-config of the active profile (may be empty).
    ai_cli.py merges this with its own defaults."""
    return get_config().get("ai") or {}


def set_ai_block(ai_dict):
    """Replace the 'ai' sub-config of the active profile."""
    set_config(ai=ai_dict)


# ── Convenience helpers ─────────────────────────────────────────────────────

def get_tag_schema():
    return get_config()["tag_schema"]


def get_all_tags():
    return [t for tags in get_tag_schema().values() for t in tags]


def get_all_tag_set():
    return set(get_all_tags())


def domain_vars():
    c = get_config()
    return {
        "brand":  c["brand_name"],
        "handle": c["social_handle"],
        "domain": c["content_domain"],
    }


def get_source_dir():
    """Resolve the configured source folder to an absolute Path.
    Relative paths are resolved relative to the project root."""
    p = Path(get_config()["source_folder"])
    return p if p.is_absolute() else (ROOT_DIR / p)


def get_output_dir():
    p = Path(get_config()["output_folder"])
    return p if p.is_absolute() else (ROOT_DIR / p)
