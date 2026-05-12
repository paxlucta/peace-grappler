"""db.py — SQLite database schema and helpers.

One database per brand profile so analyzing/generating in one brand never
shows data from another. The path is resolved on each get_db() call from
the active profile name in app_config — switching profiles in /settings
takes effect on the next request, no restart needed for query routing.
"""

import hashlib
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
PROFILES_DB_DIR = DATA_DIR / "profiles_db"
LEGACY_DB_PATH = DATA_DIR / "pg.db"

# Backward-compat alias for any importer that still reads `db.DB_PATH`.
DB_PATH = LEGACY_DB_PATH

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_\-. ]")


def _profile_db_path(name):
    safe = _FILENAME_SAFE.sub("_", (name or "").strip()) or "default"
    return PROFILES_DB_DIR / (safe + ".db")


def _active_db_path():
    """Resolve the DB file for the currently-active brand profile.
    Falls back to the legacy single-DB path if app_config can't be loaded."""
    try:
        import app_config
        name = app_config.get_active_profile_name()
        return _profile_db_path(name)
    except Exception:
        return LEGACY_DB_PATH


def _migrate_legacy_pg_db():
    """One-time: copy data/pg.db → profiles_db/PeaceGrappler.db so the
    user's existing scenes/videos land in the original-brand profile.
    Idempotent — only runs while PeaceGrappler.db doesn't exist yet."""
    if not LEGACY_DB_PATH.exists():
        return
    pg = _profile_db_path("PeaceGrappler")
    if pg.exists():
        return
    PROFILES_DB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEGACY_DB_PATH, pg)
    # WAL/SHM siblings if SQLite is mid-transaction state
    for ext in ("-wal", "-shm"):
        sib = LEGACY_DB_PATH.parent / (LEGACY_DB_PATH.name + ext)
        if sib.exists():
            try:
                shutil.copy2(sib, pg.parent / (pg.name + ext))
            except Exception:
                pass
    print(f"[db] Migrated legacy {LEGACY_DB_PATH.name} → {pg}")


def delete_profile_db(name):
    """Remove the SQLite file (plus WAL/SHM siblings) for *name*. Used by
    app_config.delete_profile so trashing a profile doesn't leak data."""
    p = _profile_db_path(name)
    for ext in ("", "-wal", "-shm"):
        target = p.parent / (p.name + ext)
        if target.exists():
            try:
                target.unlink()
            except Exception:
                pass

SCHEMA = """\
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    duration REAL DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    wide BOOLEAN DEFAULT 0,
    discovered_at TEXT DEFAULT (datetime('now')),
    analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    excluded BOOLEAN DEFAULT 0,
    ignored BOOLEAN DEFAULT 0,
    UNIQUE(video_id, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS scene_tags (
    scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (scene_id, tag)
);

CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    at_time REAL NOT NULL,
    note TEXT,
    dialog TEXT
);

CREATE TABLE IF NOT EXISTS analyzed_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    analyzed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (video_id, tag)
);

CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY,
    scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    graded_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS generated_videos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    duration REAL DEFAULT 0,
    timeline_json TEXT NOT NULL,
    caption TEXT DEFAULT '',
    generated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wizard_research (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    result_json TEXT NOT NULL,
    researched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wizard_feedback (
    id INTEGER PRIMARY KEY,
    generated_video_id INTEGER NOT NULL REFERENCES generated_videos(id) ON DELETE CASCADE,
    feedback TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS text_overlay_presets (
    id INTEGER PRIMARY KEY,
    name TEXT,
    data_json TEXT NOT NULL,
    thumbnail BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    language TEXT NOT NULL DEFAULT '',
    is_translation BOOLEAN DEFAULT 0,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_video_time
    ON transcripts(video_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_transcripts_text
    ON transcripts(video_id, language);

CREATE TABLE IF NOT EXISTS imported_externals (
    platform     TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    title        TEXT,
    page_url     TEXT,
    local_path   TEXT,
    video_id     INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    imported_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (platform, external_id)
);
"""


def get_db():
    """Return a connection to the active brand profile's database, creating
    tables if needed. Each profile gets its own SQLite file so switching
    profiles fully isolates videos/scenes/grades/etc."""
    _migrate_legacy_pg_db()
    path = _active_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Migrate: add caption column if missing (for DBs created before this field)
    try:
        conn.execute("SELECT caption FROM generated_videos LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE generated_videos ADD COLUMN caption TEXT DEFAULT ''")
        conn.commit()
    # Migrate: Drive integration columns
    for table in ("videos", "generated_videos"):
        for col in ("drive_file_id", "drive_link"):
            try:
                conn.execute(f"SELECT {col} FROM {table} LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                conn.commit()
    # Migrate: AI provider attribution (which AI generated which content).
    # `analyzer_provider` is the legacy single-provider column. The two
    # *_analyzer_provider columns split it per analysis mode so the
    # status badges can attribute each pass independently — visual and
    # speech might run under different providers.
    # Each provider column gets a paired model column so the UI can show
    # "claude" as the brand badge with the specific model (e.g.
    # "claude-haiku-4-5") on hover. Provider stays as the user-visible
    # brand key; model captures the version that actually ran.
    _provider_columns = {
        "videos":           ["analyzer_provider",
                             "visual_analyzer_provider",
                             "speech_analyzer_provider",
                             "analyzer_model",
                             "visual_analyzer_model",
                             "speech_analyzer_model"],
        "generated_videos": ["caption_provider", "wizard_provider",
                             "caption_model", "wizard_model"],
        "wizard_research":  ["provider", "model"],
        "transcripts":      ["provider", "model"],
    }
    for table, cols in _provider_columns.items():
        for col in cols:
            try:
                conn.execute(f"SELECT {col} FROM {table} LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                conn.commit()
    # Backfill the per-mode provider columns from the legacy one. Same
    # heuristic as the per-mode timestamp backfill: transcripts → speech,
    # otherwise → visual.
    conn.execute(
        """UPDATE videos
           SET speech_analyzer_provider = COALESCE(speech_analyzer_provider, analyzer_provider)
           WHERE id IN (SELECT DISTINCT video_id FROM transcripts)
             AND analyzer_provider IS NOT NULL"""
    )
    conn.execute(
        """UPDATE videos
           SET visual_analyzer_provider = COALESCE(visual_analyzer_provider, analyzer_provider)
           WHERE id NOT IN (SELECT DISTINCT video_id FROM transcripts)
             AND analyzer_provider IS NOT NULL"""
    )
    # Migrate: per-mode analysis timestamps. The legacy `analyzed_at` is set
    # by both visual and speech runs and can't tell them apart, which makes
    # the analyze-page status badge ambiguous. Track each mode separately
    # and backfill from existing data: any video that already has transcript
    # rows must have had speech analysis run; otherwise we attribute the
    # legacy timestamp to visual.
    needs_backfill = False
    for col in ("visual_analyzed_at", "speech_analyzed_at"):
        try:
            conn.execute(f"SELECT {col} FROM videos LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {col} TEXT")
            needs_backfill = True
    if needs_backfill:
        # Speech: whoever has transcript rows.
        conn.execute(
            """UPDATE videos
               SET speech_analyzed_at = COALESCE(speech_analyzed_at, analyzed_at)
               WHERE id IN (SELECT DISTINCT video_id FROM transcripts)
                 AND analyzed_at IS NOT NULL"""
        )
        # Visual: anyone analyzed who *doesn't* have transcripts.
        conn.execute(
            """UPDATE videos
               SET visual_analyzed_at = COALESCE(visual_analyzed_at, analyzed_at)
               WHERE id NOT IN (SELECT DISTINCT video_id FROM transcripts)
                 AND analyzed_at IS NOT NULL"""
        )
        conn.commit()
    conn.commit()
    return conn


def hash_file(path):
    """Fast fingerprint: SHA-256 of (first 1MB + last 1MB + file size as bytes)."""
    path = Path(path)
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 1024 * 1024:
            f.seek(max(0, size - 1024 * 1024))
            h.update(f.read(1024 * 1024))
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()


def _ffprobe_dimensions(path):
    """Get (width, height) via ffprobe."""
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


def _ffprobe_duration(path):
    """Get duration via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def register_video(path):
    """Hash file, insert/update video record, get dimensions via ffprobe.
    Returns the video row id."""
    path = Path(path)
    file_hash = hash_file(path)
    w, h = _ffprobe_dimensions(path)
    dur = _ffprobe_duration(path)
    wide = w > h if w > 0 and h > 0 else False

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO videos (hash, filename, path, duration, width, height, wide)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   filename=excluded.filename,
                   path=excluded.path,
                   duration=excluded.duration,
                   width=excluded.width,
                   height=excluded.height,
                   wide=excluded.wide""",
            (file_hash, path.name, str(path), dur, w, h, wide),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM videos WHERE hash=?", (file_hash,)).fetchone()
        return row["id"]
    finally:
        conn.close()


def get_video_by_hash(file_hash):
    """Lookup video by hash."""
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM videos WHERE hash=?", (file_hash,)).fetchone()
    finally:
        conn.close()


def get_video_by_id(vid):
    """Lookup video by id."""
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    finally:
        conn.close()


def get_all_videos():
    """Return all videos."""
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM videos ORDER BY filename").fetchall()
    finally:
        conn.close()


def get_all_scenes(include_ignored=False, include_excluded=False):
    """Returns scenes with tags and video info."""
    conn = get_db()
    try:
        conditions = []
        if not include_ignored:
            conditions.append("s.ignored = 0")
        if not include_excluded:
            conditions.append("s.excluded = 0")
        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = conn.execute(f"""
            SELECT s.id, s.video_id, s.start_time, s.end_time,
                   s.excluded, s.ignored,
                   v.path, v.filename, v.wide, v.duration as video_duration,
                   v.analyzer_provider, v.analyzer_model,
                   v.visual_analyzer_provider, v.visual_analyzer_model,
                   v.speech_analyzer_provider, v.speech_analyzer_model
            FROM scenes s
            JOIN videos v ON v.id = s.video_id
            {where}
            ORDER BY v.filename, s.start_time
        """).fetchall()

        scenes = []
        for row in rows:
            tags = conn.execute(
                "SELECT tag FROM scene_tags WHERE scene_id=? ORDER BY tag",
                (row["id"],)
            ).fetchall()
            scenes.append({
                "id": row["id"],
                "video_id": row["video_id"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "excluded": bool(row["excluded"]),
                "ignored": bool(row["ignored"]),
                "video_path": row["path"],
                "video_filename": row["filename"],
                "wide": bool(row["wide"]),
                "video_duration": row["video_duration"],
                "analyzer_provider": row["analyzer_provider"] or "",
                "analyzer_model":    row["analyzer_model"] or "",
                "visual_analyzer_provider": row["visual_analyzer_provider"] or "",
                "visual_analyzer_model":    row["visual_analyzer_model"] or "",
                "speech_analyzer_provider": row["speech_analyzer_provider"] or "",
                "speech_analyzer_model":    row["speech_analyzer_model"] or "",
                "tags": [t["tag"] for t in tags],
            })
        return scenes
    finally:
        conn.close()


def save_analysis(video_id, tags_dict, moments_list, analyzed_tag_names,
                  provider=None, mode=None, model=None):
    """Save analysis results (tags, moments, analyzed_tag_names) to DB.
    tags_dict: {"tag_name": [{"start": 0.0, "end": 5.2}, ...], ...}
    moments_list: [{"at": 3.5, "note": "...", "dialog": "..."}, ...]
    analyzed_tag_names: list of tag names that were analyzed
    mode: 'visual' or 'speech' — sets the per-mode timestamp column so the
        analyze-page status badge can show distinct visual/audio states. If
        None, only the legacy ``analyzed_at`` timestamp is set.
    model: the specific model that ran (e.g. ``"claude-haiku-4-5-20251001"``).
        Stored alongside *provider* so the UI badge can show the brand and
        the version it ran on.
    """
    conn = get_db()
    try:
        # Collect all unique (start, end) ranges and their tags
        range_tags = {}  # (start, end) -> set of tags
        for tag, ranges in tags_dict.items():
            for r in ranges:
                key = (round(r["start"], 1), round(r["end"], 1))
                if key not in range_tags:
                    range_tags[key] = set()
                range_tags[key].add(tag)

        # Insert scenes and tags
        for (start, end), tags in range_tags.items():
            # Insert or ignore the scene
            conn.execute(
                """INSERT OR IGNORE INTO scenes (video_id, start_time, end_time)
                   VALUES (?, ?, ?)""",
                (video_id, start, end),
            )
            scene_row = conn.execute(
                """SELECT id FROM scenes
                   WHERE video_id=? AND start_time=? AND end_time=?""",
                (video_id, start, end),
            ).fetchone()
            if scene_row:
                scene_id = scene_row["id"]
                for tag in tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO scene_tags (scene_id, tag) VALUES (?, ?)",
                        (scene_id, tag),
                    )

        # Insert moments
        for m in moments_list:
            conn.execute(
                """INSERT INTO moments (video_id, at_time, note, dialog)
                   VALUES (?, ?, ?, ?)""",
                (video_id, m.get("at", 0), m.get("note", ""), m.get("dialog")),
            )

        # Record which tags were analyzed
        for tag_name in analyzed_tag_names:
            conn.execute(
                "INSERT OR IGNORE INTO analyzed_tags (video_id, tag) VALUES (?, ?)",
                (video_id, tag_name),
            )

        # Mark video as analyzed and record which provider produced the tags.
        # Also stamp the per-mode timestamp + per-mode provider so the
        # analyze-page can show separate Visual / Audio status badges
        # each with its own AI badge.
        ts_col = None
        prov_col = None
        model_col = None
        if mode == "visual":
            ts_col, prov_col = "visual_analyzed_at", "visual_analyzer_provider"
            model_col = "visual_analyzer_model"
        elif mode == "speech":
            ts_col, prov_col = "speech_analyzed_at", "speech_analyzer_provider"
            model_col = "speech_analyzer_model"
        sets = ["analyzed_at = datetime('now')"]
        params = []
        if ts_col:
            sets.append(f"{ts_col} = datetime('now')")
        if provider:
            sets.append("analyzer_provider = ?")
            params.append(provider)
            if prov_col:
                sets.append(f"{prov_col} = ?")
                params.append(provider)
        if model:
            sets.append("analyzer_model = ?")
            params.append(model)
            if model_col:
                sets.append(f"{model_col} = ?")
                params.append(model)
        params.append(video_id)
        conn.execute(
            f"UPDATE videos SET {', '.join(sets)} WHERE id=?",
            params,
        )

        conn.commit()
    finally:
        conn.close()


def set_scene_ignored(scene_id, ignored):
    """Toggle ignore on a scene."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE scenes SET ignored=? WHERE id=?",
            (1 if ignored else 0, scene_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_scene_excluded(scene_id, excluded):
    """Toggle exclude on a scene."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE scenes SET excluded=? WHERE id=?",
            (1 if excluded else 0, scene_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_scene_tag(scene_id, tag):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO scene_tags (scene_id, tag) VALUES (?, ?)",
            (scene_id, tag),
        )
        conn.commit()
    finally:
        conn.close()


def remove_scene_tag(scene_id, tag):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM scene_tags WHERE scene_id=? AND tag=?",
            (scene_id, tag),
        )
        conn.commit()
    finally:
        conn.close()


def get_tag_vote_signature(min_votes=2, exclude_tag="auto-hidden"):
    """Compute per-tag up/down vote counts from manually graded scenes.

    A scene is "down" if its average grade < 3 AND it does not carry the
    *exclude_tag* (so we don't learn from system-hidden scenes). "up" if
    average grade >= 3.

    Returns dict: tag -> {"up": int, "down": int, "down_rate": float}
    Only includes tags with at least *min_votes* total votes.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT g.scene_id,
                      AVG(g.score) AS avg_score,
                      EXISTS (
                          SELECT 1 FROM scene_tags t
                          WHERE t.scene_id = g.scene_id AND t.tag = ?
                      ) AS is_auto_hidden
               FROM grades g
               GROUP BY g.scene_id""",
            (exclude_tag,),
        ).fetchall()

        sig = {}
        for r in rows:
            scene_id = r["scene_id"]
            avg = r["avg_score"]
            is_down = avg < 3
            if is_down and r["is_auto_hidden"]:
                continue  # don't learn from system-hidden scenes
            tags = conn.execute(
                "SELECT tag FROM scene_tags WHERE scene_id=?", (scene_id,)
            ).fetchall()
            for t in tags:
                tag = t["tag"]
                if tag == exclude_tag:
                    continue
                slot = sig.setdefault(tag, {"up": 0, "down": 0})
                if is_down:
                    slot["down"] += 1
                else:
                    slot["up"] += 1

        result = {}
        for tag, c in sig.items():
            total = c["up"] + c["down"]
            if total < min_votes:
                continue
            c["down_rate"] = c["down"] / total
            result[tag] = c
        return result
    finally:
        conn.close()


def get_scene_grades():
    """Return grade info for weighted selection.
    Returns dict: scene_id -> {"total_score": int, "times_graded": int}
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT scene_id, SUM(score) as total_score, COUNT(*) as times_graded
               FROM grades GROUP BY scene_id"""
        ).fetchall()
        return {
            row["scene_id"]: {
                "total_score": row["total_score"],
                "times_graded": row["times_graded"],
            }
            for row in rows
        }
    finally:
        conn.close()


def save_grade(scene_id, score):
    """Record a grade for a scene."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO grades (scene_id, score) VALUES (?, ?)",
            (scene_id, score),
        )
        conn.commit()
    finally:
        conn.close()


def get_analyzed_tags(video_id):
    """Return set of tag names already analyzed for a video."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tag FROM analyzed_tags WHERE video_id=?", (video_id,)
        ).fetchall()
        return {r["tag"] for r in rows}
    finally:
        conn.close()


def save_transcripts(video_id, segments, language="", is_translation=False,
                     provider=None, model=None):
    """Persist Whisper transcript segments for a video. Replaces any prior
    rows for the same (video_id, language, is_translation) so re-running
    audio analysis with the same settings overwrites cleanly.

    *segments*: list of ``{"start": float, "end": float, "text": str}`` —
        the same shape audio_analysis.transcribe() returns.
    *language*: ISO code from Whisper's auto-detect (e.g. 'ru'), or '' if
        unknown. For *is_translation=True* this is the SOURCE language —
        the text itself is English regardless.
    *is_translation*: False for the original transcript, True for the
        Whisper-translated English version saved alongside it.
    *provider*/*model*: which Whisper-side stack produced these segments
        (e.g. ``provider='whisper'``, ``model='base'``). Stored on each
        row so the UI can show a brand badge with the specific model on
        hover.
    """
    if not segments:
        return
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM transcripts WHERE video_id=? AND language=? AND is_translation=?",
            (video_id, language or "", 1 if is_translation else 0),
        )
        rows = [
            (video_id, language or "", 1 if is_translation else 0,
             float(s.get("start", 0)), float(s.get("end", 0)),
             (s.get("text") or "").strip(),
             provider or None, model or None)
            for s in segments
            if (s.get("text") or "").strip()
        ]
        conn.executemany(
            """INSERT INTO transcripts
               (video_id, language, is_translation, start_time, end_time, text,
                provider, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def get_transcripts_in_range(video_id, start_time, end_time):
    """Return transcript segments that overlap [start_time, end_time] for a
    given video. Result is grouped by ``(language, is_translation)`` so the
    UI can show source + translation side-by-side.

    Returns: ``[{"language": "ru", "is_translation": False, "segments":
    [{start, end, text}, ...]}, ...]`` — original first, translations after.
    """
    conn = get_db()
    try:
        # Schema may not have provider/model yet on older DBs — pick safe
        # column lists and skip the new fields when they're missing.
        cols = "language, is_translation, start_time, end_time, text"
        has_attr = True
        try:
            conn.execute("SELECT provider, model FROM transcripts LIMIT 0")
            cols += ", provider, model"
        except sqlite3.OperationalError:
            has_attr = False
        rows = conn.execute(
            f"""SELECT {cols}
               FROM transcripts
               WHERE video_id=?
                 AND start_time < ?
                 AND end_time > ?
               ORDER BY is_translation, start_time""",
            (video_id, end_time, start_time),
        ).fetchall()
        groups = {}
        attr = {}  # (language, is_translation) -> (provider, model)
        for r in rows:
            key = (r["language"], bool(r["is_translation"]))
            groups.setdefault(key, []).append({
                "start": r["start_time"],
                "end":   r["end_time"],
                "text":  r["text"],
            })
            if has_attr and key not in attr:
                attr[key] = (r["provider"] or "", r["model"] or "")
        out = []
        for (lang, is_xlat), segs in sorted(groups.items(), key=lambda x: x[0][1]):
            p, m = attr.get((lang, is_xlat), ("", ""))
            out.append({
                "language":       lang,
                "is_translation": is_xlat,
                "segments":       segs,
                "provider":       p,
                "model":          m,
            })
        return out
    finally:
        conn.close()


def get_video_ids_with_transcripts():
    """Set of video_ids that have at least one transcript row in the active
    profile. Used to decide whether to surface the transcript button on
    the Analyze page."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT video_id FROM transcripts"
        ).fetchall()
        return {r["video_id"] for r in rows}
    finally:
        conn.close()


def get_scene_ids_with_transcripts():
    """Set of scene_ids whose [start,end] window overlaps any transcript
    row for the same video. Cheaper than per-scene queries when rendering
    a long Scenes page."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT DISTINCT s.id
               FROM scenes s
               JOIN transcripts t
                 ON t.video_id = s.video_id
                AND t.start_time < s.end_time
                AND t.end_time   > s.start_time"""
        ).fetchall()
        return {r["id"] for r in rows}
    finally:
        conn.close()


def get_transcript_for_clip(video_id, clip_start, clip_end,
                            prefer_translation=False):
    """Return transcript segments that fall inside ``[clip_start, clip_end]``
    on *video_id*, shifted so timestamps are relative to ``clip_start``.

    Used by the burn-in captions pipeline (wizard + builder per-layer
    toggle). Each segment is also clamped to the clip window so partial
    overlaps render only the visible portion.

    *prefer_translation*: when True, picks the English-translation rows
    first if they exist; otherwise returns the source-language rows.
    Caller decides which to render — most users want the source language
    since that matches what's actually being said in the audio.

    Returns ``[{"start": float, "end": float, "text": str}, ...]`` or [].
    """
    if clip_end <= clip_start:
        return []
    conn = get_db()
    try:
        # Two passes — pick the side the user asked for if it has any
        # rows, otherwise fall back to whatever's present.
        for translate_first in ([1, 0] if prefer_translation else [0, 1]):
            rows = conn.execute(
                """SELECT start_time, end_time, text
                   FROM transcripts
                   WHERE video_id=? AND is_translation=?
                     AND start_time < ? AND end_time > ?
                   ORDER BY start_time""",
                (video_id, translate_first, clip_end, clip_start),
            ).fetchall()
            if rows:
                break
        out = []
        for r in rows:
            s = max(float(r["start_time"]), clip_start) - clip_start
            e = min(float(r["end_time"]),   clip_end)   - clip_start
            if e <= s:
                continue
            out.append({"start": round(s, 2), "end": round(e, 2),
                        "text": r["text"]})
        return out
    finally:
        conn.close()


def get_video_transcripts(video_id):
    """All transcript groups for *video_id*. Same shape as
    get_transcripts_in_range() but spans the full video. Used by the
    analyze-page transcript modal."""
    conn = get_db()
    try:
        cols = "language, is_translation, start_time, end_time, text"
        has_attr = True
        try:
            conn.execute("SELECT provider, model FROM transcripts LIMIT 0")
            cols += ", provider, model"
        except sqlite3.OperationalError:
            has_attr = False
        rows = conn.execute(
            f"""SELECT {cols}
               FROM transcripts
               WHERE video_id=?
               ORDER BY is_translation, start_time""",
            (video_id,),
        ).fetchall()
        groups = {}
        attr = {}
        for r in rows:
            key = (r["language"], bool(r["is_translation"]))
            groups.setdefault(key, []).append({
                "start": r["start_time"],
                "end":   r["end_time"],
                "text":  r["text"],
            })
            if has_attr and key not in attr:
                attr[key] = (r["provider"] or "", r["model"] or "")
        out = []
        for (lang, is_xlat), segs in sorted(groups.items(), key=lambda x: x[0][1]):
            p, m = attr.get((lang, is_xlat), ("", ""))
            out.append({
                "language":       lang,
                "is_translation": is_xlat,
                "segments":       segs,
                "provider":       p,
                "model":          m,
            })
        return out
    finally:
        conn.close()


def search_scene_ids_by_text(query):
    """Loose text search across transcripts → set of scene_ids whose time
    window overlaps a matching transcript row.

    Loose-matching strategy: split the query on whitespace; every token
    must appear (case-insensitive substring) in the SAME transcript row.
    This catches \"praktika 234\" or \"234 practice\" without requiring
    word-order to match what Whisper produced. Pure substring would be
    too strict for multi-word queries; full-text search is overkill for
    the dataset size we expect.
    """
    q = (query or "").strip()
    if not q:
        return set()
    tokens = [t for t in q.split() if t]
    if not tokens:
        return set()
    where_clauses = " AND ".join(["t.text LIKE ? COLLATE NOCASE"] * len(tokens))
    params = [f"%{tok}%" for tok in tokens]
    sql = f"""SELECT DISTINCT s.id
              FROM scenes s
              JOIN transcripts t
                ON t.video_id = s.video_id
               AND t.start_time < s.end_time
               AND t.end_time   > s.start_time
              WHERE {where_clauses}"""
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
        return {r["id"] for r in rows}
    finally:
        conn.close()


def delete_scene(scene_id):
    """Hard-delete a scene and its dependent rows. Used by the Cut Scene
    confirmation modal's Discard button so the user can throw away an
    accidental cut without it cluttering the Scenes page."""
    conn = get_db()
    try:
        # ON DELETE CASCADE on scene_tags / grades takes care of those.
        conn.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
        conn.commit()
    finally:
        conn.close()


def delete_video(video_id, remove_file=True):
    """Hard-delete a source video + every scene/moment/transcript/analyzed-tag
    row that referenced it. Used by the trash button on /analyze.

    Returns ``(removed_path, scene_count)``. The CASCADE foreign keys on
    scenes / moments / transcripts / scene_tags / analyzed_tags clean up
    automatically; ``imported_externals`` uses SET NULL so the import
    history record stays around with ``video_id=NULL`` (so re-scanning
    doesn't try to download the same external twice).

    When *remove_file* is True (default), the underlying file on disk is
    also unlinked. Pass False if the caller wants to keep the file but
    just drop the DB record.
    """
    conn = get_db()
    path = None
    scene_count = 0
    try:
        row = conn.execute(
            "SELECT path FROM videos WHERE id=?", (video_id,)
        ).fetchone()
        if row:
            path = row["path"]
        scene_row = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE video_id=?",
            (video_id,),
        ).fetchone()
        if scene_row:
            scene_count = scene_row["n"]
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        conn.commit()
    finally:
        conn.close()
    if remove_file and path:
        try:
            from pathlib import Path
            p = Path(path)
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            # File may be missing or unlinkable (locked / read-only) — the
            # DB row is already gone, so callers shouldn't fail the whole
            # delete over a stuck file.
            pass
    return path, scene_count


def create_scene(video_id, start_time, end_time, tags=None):
    """Insert a new scene + tags. Returns the new scene id. Used by the
    \"Cut Scene from selection\" flow on the Analyze page."""
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO scenes (video_id, start_time, end_time)
               VALUES (?, ?, ?)""",
            (video_id, round(float(start_time), 2), round(float(end_time), 2)),
        )
        scene_id = cur.lastrowid
        for tag in (tags or []):
            tag = (tag or "").strip()
            if not tag:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO scene_tags (scene_id, tag) VALUES (?, ?)",
                (scene_id, tag),
            )
        conn.commit()
        return scene_id
    finally:
        conn.close()


def search_transcripts(query, limit=200):
    """Substring search across all stored transcripts in the active profile.
    Returns ``[{video_id, filename, language, is_translation, start, end,
    text}, ...]`` ordered by video then time. Case-insensitive."""
    q = (query or "").strip()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT t.video_id, v.filename, t.language, t.is_translation,
                      t.start_time, t.end_time, t.text
               FROM transcripts t
               JOIN videos v ON v.id = t.video_id
               WHERE t.text LIKE ? COLLATE NOCASE
               ORDER BY t.video_id, t.start_time
               LIMIT ?""",
            (f"%{q}%", int(limit)),
        ).fetchall()
        return [{
            "video_id":       r["video_id"],
            "filename":       r["filename"],
            "language":       r["language"],
            "is_translation": bool(r["is_translation"]),
            "start":          r["start_time"],
            "end":            r["end_time"],
            "text":           r["text"],
        } for r in rows]
    finally:
        conn.close()


def get_scene_by_id(scene_id):
    """Return a scene dict with video info and tags."""
    conn = get_db()
    try:
        row = conn.execute("""
            SELECT s.id, s.video_id, s.start_time, s.end_time,
                   s.excluded, s.ignored,
                   v.path, v.filename, v.wide
            FROM scenes s
            JOIN videos v ON v.id = s.video_id
            WHERE s.id = ?
        """, (scene_id,)).fetchone()
        if not row:
            return None
        tags = conn.execute(
            "SELECT tag FROM scene_tags WHERE scene_id=? ORDER BY tag",
            (row["id"],)
        ).fetchall()
        return {
            "id": row["id"],
            "video_id": row["video_id"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "excluded": bool(row["excluded"]),
            "ignored": bool(row["ignored"]),
            "video_path": row["path"],
            "video_filename": row["filename"],
            "wide": bool(row["wide"]),
            "tags": [t["tag"] for t in tags],
        }
    finally:
        conn.close()


def save_generated_video(path, duration, timeline, caption="",
                         caption_provider=None, wizard_provider=None,
                         caption_model=None, wizard_model=None):
    """Save a generated video record with its timeline as JSON.

    *caption_provider* / *wizard_provider* record which AI produced the caption
    and the wizard plan (scene picks + narrative). They surface as small
    badges in the library UI. The paired *_model fields store the specific
    model version (e.g. ``claude-haiku-4-5-20251001``) shown on hover.
    """
    import json
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO generated_videos "
            "(path, duration, timeline_json, caption, "
            " caption_provider, wizard_provider, "
            " caption_model, wizard_model) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(path), duration, json.dumps(timeline), caption,
             caption_provider, wizard_provider,
             caption_model, wizard_model),
        )
        conn.commit()
    finally:
        conn.close()


def update_video_caption(video_id, caption, provider=None, model=None):
    """Update the caption for a generated video.

    *provider* / *model*: which AI + which specific version produced this
    caption (e.g. ``provider='claude', model='claude-haiku-4-5-20251001'``).
    Surfaces as a small badge next to the caption with the model on hover.
    """
    conn = get_db()
    try:
        sets = ["caption = ?"]
        params = [caption]
        if provider is not None:
            sets.append("caption_provider = ?")
            params.append(provider)
        if model is not None:
            sets.append("caption_model = ?")
            params.append(model)
        params.append(video_id)
        conn.execute(
            f"UPDATE generated_videos SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def get_generated_video(path):
    """Look up a generated video by path. Returns dict with timeline or None."""
    import json
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM generated_videos WHERE path=?", (str(path),)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "path": row["path"],
            "duration": row["duration"],
            "timeline": json.loads(row["timeline_json"]),
            "caption": row["caption"] or "",
            "generated_at": row["generated_at"],
        }
    finally:
        conn.close()


def set_video_drive_info(video_id, drive_file_id, drive_link):
    """Record Drive source for an inbox-pulled video."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE videos SET drive_file_id=?, drive_link=? WHERE id=?",
            (drive_file_id, drive_link, video_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_video_by_drive_file_id(drive_file_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM videos WHERE drive_file_id=?", (drive_file_id,)
        ).fetchone()
    finally:
        conn.close()


def set_generated_video_drive_info(video_id, drive_file_id, drive_link):
    """Record Drive upload info for a generated video."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE generated_videos SET drive_file_id=?, drive_link=? WHERE id=?",
            (drive_file_id, drive_link, video_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_generated_video_by_id(video_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM generated_videos WHERE id=?", (video_id,)
        ).fetchone()
    finally:
        conn.close()


def save_text_preset(name, data_json, thumbnail_bytes):
    """Persist a text overlay preset. Returns the new row id."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO text_overlay_presets (name, data_json, thumbnail) "
            "VALUES (?, ?, ?)",
            (name or "", data_json, thumbnail_bytes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_text_presets():
    """Return list of {id, name, created_at} (no thumbnails)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at FROM text_overlay_presets "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [
            {"id": r["id"], "name": r["name"] or "", "created_at": r["created_at"]}
            for r in rows
        ]
    finally:
        conn.close()


def get_text_preset(preset_id):
    """Return the full preset dict including data_json. None if missing."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, data_json, created_at "
            "FROM text_overlay_presets WHERE id=?", (preset_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"] or "",
            "data_json": row["data_json"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def get_text_preset_thumbnail(preset_id):
    """Return the thumbnail BLOB (bytes) or None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT thumbnail FROM text_overlay_presets WHERE id=?",
            (preset_id,),
        ).fetchone()
        return row["thumbnail"] if row else None
    finally:
        conn.close()


def delete_text_preset(preset_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM text_overlay_presets WHERE id=?", (preset_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Imported external videos (per profile) ─────────────────────────────────

def get_imported_external_ids(platform):
    """Set of external_ids already imported for *platform* in the active
    profile DB. Used to filter the import-modal listing."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT external_id FROM imported_externals WHERE platform=?",
            (platform,),
        ).fetchall()
        return {r["external_id"] for r in rows}
    finally:
        conn.close()


def list_imported_externals():
    """All imported videos across platforms, newest first."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT platform, external_id, title, page_url, local_path, "
            "video_id, imported_at FROM imported_externals "
            "ORDER BY imported_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_imported_external(platform, external_id, title=None,
                              page_url=None, local_path=None, video_id=None):
    """Mark a (platform, external_id) as imported. Idempotent."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO imported_externals "
            "(platform, external_id, title, page_url, local_path, video_id, "
            " imported_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (platform, external_id, title, page_url,
             str(local_path) if local_path else None,
             video_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_generated_videos():
    """Return all generated videos, newest first."""
    import json
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM generated_videos ORDER BY generated_at DESC"
        ).fetchall()
        keys_avail = set(rows[0].keys()) if rows else set()
        return [{
            "id": r["id"],
            "path": r["path"],
            "duration": r["duration"],
            "timeline": json.loads(r["timeline_json"]),
            "caption": r["caption"] or "",
            "generated_at": r["generated_at"],
            "drive_file_id": r["drive_file_id"] if "drive_file_id" in keys_avail else None,
            "drive_link": r["drive_link"] if "drive_link" in keys_avail else None,
            "caption_provider": r["caption_provider"] if "caption_provider" in keys_avail else None,
            "wizard_provider": r["wizard_provider"] if "wizard_provider" in keys_avail else None,
            "caption_model": r["caption_model"] if "caption_model" in keys_avail else None,
            "wizard_model":  r["wizard_model"]  if "wizard_model"  in keys_avail else None,
        } for r in rows]
    finally:
        conn.close()
