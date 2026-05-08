"""db.py — SQLite database schema and helpers for PeaceGrappler."""

import hashlib
import sqlite3
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DB_PATH = ROOT_DIR / "data" / "pg.db"

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
"""


def get_db():
    """Return a connection to the database, creating tables if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
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
                   v.path, v.filename, v.wide, v.duration as video_duration
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
                "tags": [t["tag"] for t in tags],
            })
        return scenes
    finally:
        conn.close()


def save_analysis(video_id, tags_dict, moments_list, analyzed_tag_names):
    """Save analysis results (tags, moments, analyzed_tag_names) to DB.
    tags_dict: {"tag_name": [{"start": 0.0, "end": 5.2}, ...], ...}
    moments_list: [{"at": 3.5, "note": "...", "dialog": "..."}, ...]
    analyzed_tag_names: list of tag names that were analyzed
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

        # Mark video as analyzed
        conn.execute(
            "UPDATE videos SET analyzed_at = datetime('now') WHERE id=?",
            (video_id,),
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


def save_generated_video(path, duration, timeline, caption=""):
    """Save a generated video record with its timeline as JSON."""
    import json
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO generated_videos (path, duration, timeline_json, caption) "
            "VALUES (?,?,?,?)",
            (str(path), duration, json.dumps(timeline), caption),
        )
        conn.commit()
    finally:
        conn.close()


def update_video_caption(video_id, caption):
    """Update the caption for a generated video."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE generated_videos SET caption=? WHERE id=?",
            (caption, video_id),
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


def get_all_generated_videos():
    """Return all generated videos, newest first."""
    import json
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM generated_videos ORDER BY generated_at DESC"
        ).fetchall()
        return [{
            "id": r["id"],
            "path": r["path"],
            "duration": r["duration"],
            "timeline": json.loads(r["timeline_json"]),
            "caption": r["caption"] or "",
            "generated_at": r["generated_at"],
            "drive_file_id": r["drive_file_id"] if "drive_file_id" in r.keys() else None,
            "drive_link": r["drive_link"] if "drive_link" in r.keys() else None,
        } for r in rows]
    finally:
        conn.close()
