"""crop_presets.py — Save/load free-mode crop layouts.

A preset is a named collection of source+destination rectangle pairs the
user has built in the crop modal's free mode. Saving lets them re-apply
the same multi-window composite to other clips without re-drawing each
rectangle.

Storage: ``data/crop_presets.json`` — same shape as the text-overlay
gallery (a list of ``{id, name, rects, modified}`` entries).
"""

import json
import secrets
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

import app_config


crop_presets_bp = Blueprint("crop_presets", __name__)
_lock = threading.Lock()

_STORE_PATH = app_config.DATA_DIR / "crop_presets.json"


def _now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _new_id():
    return "cp_" + secrets.token_hex(5)


def _read_all():
    if not _STORE_PATH.exists():
        return []
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("presets") if isinstance(raw, dict) else raw
    return items if isinstance(items, list) else []


def _write_all(items):
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(
        json.dumps({"presets": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


@crop_presets_bp.route("/api/crop-presets")
def api_list():
    """Return every saved preset, newest first."""
    with _lock:
        items = list(_read_all())
    items.sort(key=lambda p: p.get("modified") or "", reverse=True)
    return jsonify(items)


@crop_presets_bp.route("/api/crop-presets", methods=["POST"])
def api_save():
    """Save (or update) a preset. Body: ``{id?, name, rects}``."""
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    rects = body.get("rects") or []
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not isinstance(rects, list):
        return jsonify({"error": "rects must be a list"}), 400
    pid = (body.get("id") or "").strip()
    with _lock:
        items = _read_all()
        if pid:
            for p in items:
                if p.get("id") == pid:
                    p["name"] = name
                    p["rects"] = rects
                    p["modified"] = _now_iso()
                    _write_all(items)
                    return jsonify(p)
        new_p = {
            "id": _new_id(),
            "name": name,
            "rects": rects,
            "modified": _now_iso(),
        }
        items.append(new_p)
        _write_all(items)
    return jsonify(new_p)


@crop_presets_bp.route("/api/crop-presets/<pid>", methods=["DELETE"])
def api_delete(pid):
    with _lock:
        items = _read_all()
        new_items = [p for p in items if p.get("id") != pid]
        if len(new_items) == len(items):
            return jsonify({"error": "Not found"}), 404
        _write_all(new_items)
    return jsonify({"ok": True})
