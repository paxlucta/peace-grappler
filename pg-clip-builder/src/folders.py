"""folders.py — Virtual folders for the file browser columns.

Folders are app-level groupings of items (source video files for the
Builder/Scenes pages, generated reels for the Library page). They are
entirely "virtual" — they don't mirror anything on the filesystem.

Two kinds:

  - **Smart** folders are computed from rules. They cannot be edited or
    deleted, and the user cannot drop items into them. Built-ins:
      * ``all``    — every item in the scope.
      * ``today``  — items whose mtime is on the current local date.

  - **User** folders are created/renamed/deleted by the user. An item
    can live in at most one user folder; dragging it elsewhere moves
    it (no duplication). Smart folders are independent of these
    memberships.

Scopes (passed as ``?scope=<name>``; defaults to ``source``):

  - ``source``  — analyzed input videos (used by Builder & Scenes).
  - ``library`` — generated reels (used by Generated Videos).

Each scope has its own folder set and its own membership map. Storage:
a single ``data/folders.json`` file::

    {
      "scopes": {
        "source":  {"folders": [...], "memberships": {...}},
        "library": {"folders": [...], "memberships": {...}}
      }
    }

(Legacy single-scope stores written before scopes were introduced are
migrated transparently on first read into the ``source`` scope.)
"""

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

import app_config


folders_bp = Blueprint("folders", __name__)
_lock = threading.Lock()

# Per-profile folder store. Each brand profile gets its own JSON file at
# ``data/profiles_folders/<safe_name>.json`` so switching brands gives a
# clean folder column (matches how DBs are isolated per profile).
_FOLDERS_DIR = app_config.DATA_DIR / "profiles_folders"
_LEGACY_STORE_PATH = app_config.DATA_DIR / "folders.json"

import re as _re
_FILENAME_SAFE = _re.compile(r"[^A-Za-z0-9_\-. ]")

def _safe_profile_name(name):
    return _FILENAME_SAFE.sub("_", (name or "").strip()) or "default"

def _store_path():
    """Resolve the folders.json file for the currently-active brand
    profile. Falls back to the legacy single-file path if app_config
    can't be read."""
    try:
        name = app_config.get_active_profile_name()
        return _FOLDERS_DIR / (_safe_profile_name(name) + ".json")
    except Exception:
        return _LEGACY_STORE_PATH

def _migrate_legacy_store(target):
    """One-time: if the active profile has no folders file yet but the
    legacy single-file store exists, copy it over so the user's existing
    folder layout lands in their current profile instead of vanishing."""
    if target.exists() or not _LEGACY_STORE_PATH.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_LEGACY_STORE_PATH.read_bytes())
    except Exception:
        pass

SCOPES = ("source", "library")
DEFAULT_SCOPE = "source"


# ── Smart-folder catalog ────────────────────────────────────────────────────
#
# Same shape for every scope right now (All / Today). The label of the
# "all" folder is scope-aware so it reads "All Files" on /builder and
# "All Videos" on /library — set in ``_smart_catalog_for_scope`` below.

def _smart_catalog_for_scope(scope):
    label_all = "All Videos" if scope == "library" else "All Files"
    return [
        {"id": "all",       "name": label_all},
        {"id": "today",     "name": "Today"},
        {"id": "favorites", "name": "Favorites"},
    ]


SMART_IDS = {"all", "today", "favorites"}


def _new_id():
    return "f_" + secrets.token_hex(5)


def _resolve_scope():
    scope = (request.args.get("scope") or "").strip().lower()
    if scope not in SCOPES:
        scope = DEFAULT_SCOPE
    return scope


def _resolve_scope_from(body):
    scope = (body.get("scope") or "").strip().lower() if isinstance(body, dict) else ""
    if scope not in SCOPES:
        scope = DEFAULT_SCOPE
    return scope


def _empty_scope():
    return {"folders": [], "memberships": {}, "favorites": []}


def _read_all():
    """Load the whole multi-scope store. Migrates legacy single-scope
    files (``{folders: ..., memberships: ...}``) into ``scopes.source``."""
    path = _store_path()
    _migrate_legacy_store(path)
    if not path.exists():
        return {"scopes": {s: _empty_scope() for s in SCOPES}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"scopes": {s: _empty_scope() for s in SCOPES}}
    if isinstance(raw, dict) and "scopes" in raw and isinstance(raw["scopes"], dict):
        scopes = raw["scopes"]
    else:
        # Legacy layout — lift folders/memberships into the source scope.
        legacy = raw if isinstance(raw, dict) else {}
        scopes = {DEFAULT_SCOPE: {
            "folders": legacy.get("folders") or [],
            "memberships": legacy.get("memberships") or {},
        }}
    # Backfill any missing scopes with empty defaults.
    for s in SCOPES:
        if s not in scopes or not isinstance(scopes[s], dict):
            scopes[s] = _empty_scope()
        if not isinstance(scopes[s].get("folders"), list):
            scopes[s]["folders"] = []
        if not isinstance(scopes[s].get("memberships"), dict):
            scopes[s]["memberships"] = {}
        if not isinstance(scopes[s].get("favorites"), list):
            scopes[s]["favorites"] = []
    return {"scopes": scopes}


def _read(scope):
    return _read_all()["scopes"][scope]


def _write_scope(scope, data):
    all_data = _read_all()
    all_data["scopes"][scope] = data
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(all_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Item enumeration per scope ─────────────────────────────────────────────

def _collect_items_for_scope(scope):
    """Return ``{item_id: mtime_or_None}`` for the scope.

    For ``source``: item_id is the source-video filename (so memberships
    survive re-analysis); mtime is the file's mtime on disk.

    For ``library``: item_id is the generated reel's filename (basename
    of its output mp4); mtime is the file's mtime on disk.
    """
    out = {}
    if scope == "source":
        # Read the videos table directly so newly-imported (but not yet
        # analyzed) files show up in All Files / Today immediately —
        # without waiting for scene extraction to populate get_all_scenes.
        try:
            from db import get_all_videos
            rows = get_all_videos()
        except Exception:
            return out
        for r in rows:
            try:
                name = r["filename"]
                video_path = r["path"]
            except Exception:
                continue
            if not name or name in out:
                continue
            p = Path(video_path) if video_path else None
            try:
                mtime = p.stat().st_mtime if p and p.exists() else None
            except Exception:
                mtime = None
            out[name] = mtime
        return out
    if scope == "library":
        try:
            from db import get_all_generated_videos
            vids = get_all_generated_videos()
        except Exception:
            return out
        for v in vids:
            p = Path(v["path"]) if v.get("path") else None
            if not p:
                continue
            name = p.name
            if name in out:
                continue
            try:
                mtime = p.stat().st_mtime if p.exists() else None
            except Exception:
                mtime = None
            out[name] = mtime
        return out
    return out


def _smart_items(item_mtimes, favorites):
    """Return ``{smart_id: [item_id, ...]}`` for every smart folder.

    *favorites* — the user's saved favorites list for this scope. Items
    no longer present (e.g. user deleted the underlying file) are
    silently dropped from the resolved list."""
    all_items = sorted(item_mtimes.keys())
    today = datetime.now().astimezone().date()
    today_items = []
    for fn, mt in item_mtimes.items():
        if mt is None:
            continue
        d = datetime.fromtimestamp(mt, tz=timezone.utc).astimezone().date()
        if d == today:
            today_items.append(fn)
    today_items.sort()
    present = set(item_mtimes.keys())
    fav_items = sorted(fn for fn in (favorites or []) if fn in present)
    return {"all": all_items, "today": today_items, "favorites": fav_items}


# ── Routes ──────────────────────────────────────────────────────────────────

@folders_bp.route("/api/folders/list")
def api_list():
    """Return all folders (smart + user) and current memberships for the
    requested scope. Each entry has the resolved list of item ids so the
    client can render counts and filter without a second roundtrip."""
    scope = _resolve_scope()
    with _lock:
        data = _read(scope)
    item_mtimes = _collect_items_for_scope(scope)
    smart_map = _smart_items(item_mtimes, data.get("favorites") or [])

    smart = []
    for f in _smart_catalog_for_scope(scope):
        smart.append({
            "id": f["id"],
            "name": f["name"],
            "kind": "smart",
            "files": smart_map.get(f["id"], []),
        })

    user = []
    membership = data["memberships"]
    by_folder = {}
    for fn, fid in membership.items():
        by_folder.setdefault(fid, []).append(fn)
    for f in data["folders"]:
        fid = f.get("id")
        if not fid:
            continue
        files = sorted(by_folder.get(fid, []))
        user.append({
            "id": fid,
            "name": f.get("name", "Untitled"),
            "kind": "user",
            "files": files,
        })

    return jsonify({
        "scope": scope,
        "smart": smart,
        "user": user,
        "memberships": membership,
        "favorites": list(data.get("favorites") or []),
    })


@folders_bp.route("/api/folders", methods=["POST"])
def api_create():
    """Create a new user folder in the scope.

    Body: ``{name, scope?}``. Scope defaults to ``source``."""
    body = request.get_json(force=True) or {}
    scope = _resolve_scope_from(body)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    with _lock:
        data = _read(scope)
        new_folder = {"id": _new_id(), "name": name}
        data["folders"].append(new_folder)
        _write_scope(scope, data)
    return jsonify({"id": new_folder["id"], "name": new_folder["name"],
                    "kind": "user", "files": [], "scope": scope})


@folders_bp.route("/api/folders/reorder", methods=["POST"])
def api_reorder():
    """Reorder user folders within a scope. Body: ``{order: [fid, ...]}``.
    Smart folders are not in the list (they have a fixed catalog order);
    any user folder ids missing from *order* are appended to preserve
    them across racy reorder calls."""
    body = request.get_json(force=True) or {}
    scope = _resolve_scope_from(body)
    order = body.get("order") or []
    if not isinstance(order, list):
        return jsonify({"error": "order must be a list"}), 400
    with _lock:
        data = _read(scope)
        by_id = {f.get("id"): f for f in data["folders"] if f.get("id")}
        new_list = []
        seen = set()
        for fid in order:
            if fid in by_id and fid not in seen:
                new_list.append(by_id[fid])
                seen.add(fid)
        for fid, f in by_id.items():
            if fid not in seen:
                new_list.append(f)
        data["folders"] = new_list
        _write_scope(scope, data)
    return jsonify({"ok": True})


@folders_bp.route("/api/folders/<fid>/rename", methods=["POST"])
def api_rename(fid):
    if fid in SMART_IDS:
        return jsonify({"error": "Smart folders cannot be renamed"}), 400
    body = request.get_json(force=True) or {}
    scope = _resolve_scope_from(body)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    with _lock:
        data = _read(scope)
        for f in data["folders"]:
            if f.get("id") == fid:
                f["name"] = name
                _write_scope(scope, data)
                return jsonify({"ok": True})
    return jsonify({"error": "Folder not found"}), 404


@folders_bp.route("/api/folders/<fid>", methods=["DELETE"])
def api_delete(fid):
    if fid in SMART_IDS:
        return jsonify({"error": "Smart folders cannot be deleted"}), 400
    scope = _resolve_scope()
    with _lock:
        data = _read(scope)
        before = len(data["folders"])
        data["folders"] = [f for f in data["folders"] if f.get("id") != fid]
        if len(data["folders"]) == before:
            return jsonify({"error": "Folder not found"}), 404
        data["memberships"] = {fn: f for fn, f in data["memberships"].items()
                               if f != fid}
        _write_scope(scope, data)
    return jsonify({"ok": True})


@folders_bp.route("/api/folders/favorite", methods=["POST"])
def api_favorite():
    """Toggle a file's favorite flag for the scope.

    Body: ``{filename, favorite?, scope?}``. If ``favorite`` is omitted,
    the current state is flipped. Returns the new state.
    """
    body = request.get_json(force=True) or {}
    scope = _resolve_scope_from(body)
    filename = (body.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename required"}), 400
    desired = body.get("favorite", None)
    with _lock:
        data = _read(scope)
        favs = list(data.get("favorites") or [])
        currently = filename in favs
        if desired is None:
            new_state = not currently
        else:
            new_state = bool(desired)
        if new_state and not currently:
            favs.append(filename)
        elif not new_state and currently:
            favs = [fn for fn in favs if fn != filename]
        data["favorites"] = favs
        _write_scope(scope, data)
    return jsonify({"ok": True, "filename": filename,
                    "favorite": new_state, "scope": scope})


@folders_bp.route("/api/folders/membership", methods=["POST"])
def api_membership():
    """Set an item's user-folder membership.

    Body: ``{filename, folder_id, scope?}``. Pass ``folder_id`` as
    ``null`` or empty string to unset. Smart folders cannot be the
    destination — the API rejects them so the client doesn't have to
    police drop targets on its own.
    """
    body = request.get_json(force=True) or {}
    scope = _resolve_scope_from(body)
    filename = (body.get("filename") or "").strip()
    folder_id = body.get("folder_id")
    folder_id = (folder_id or "").strip() if isinstance(folder_id, str) else None
    if not filename:
        return jsonify({"error": "filename required"}), 400
    if folder_id and folder_id in SMART_IDS:
        return jsonify({"error": "Cannot move into a smart folder"}), 400
    with _lock:
        data = _read(scope)
        if folder_id:
            valid = any(f.get("id") == folder_id for f in data["folders"])
            if not valid:
                return jsonify({"error": "Folder not found"}), 404
            data["memberships"][filename] = folder_id
        else:
            data["memberships"].pop(filename, None)
        _write_scope(scope, data)
    return jsonify({"ok": True})
