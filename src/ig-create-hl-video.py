#!/usr/bin/env python3
"""
ig-create-hl-video.py — Generate 3 engagement-optimized highlight reels
from existing peacegrappler Instagram content.

Three highlight types (each targets a different engagement lever):

  1. "Top Moments"   — fast-paced compilation of the highest-engagement clips
                       (6-8 clips, 3-4s each, ~30s). Hook-driven, punchy cuts.

  2. "Best Action"   — the most intense fight/training footage
                       (4-5 clips, 6-8s each, ~40s). Longer beats to build drama.

  3. "Rising Reels"  — recent reels with high engagement *rate* but lower reach
                       (6-8 clips, 3-4s each, ~30s). Resurfaces hidden gems.

Each reel gets AI-powered clip selection: Claude vision picks the best segment
from each source video based on visual energy, composition, and action.

Output:  output/highlights-YYYY-MM-DD/highlight-{1,2,3}.mp4
State:   data/ig-hl-video-state.json (tracks used source reels)
"""

import base64
import json
import os
import random
from PIL import Image, ImageDraw, ImageFont
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ── paths & config ────────────────────────────────────────────────────────────

ROOT_DIR   = Path(__file__).parent.parent
ENV_FILE   = ROOT_DIR / ".env"
DB_FILE    = ROOT_DIR / "peacegrappler.db"
OUTPUT_DIR = ROOT_DIR / "output"
STATE_FILE = ROOT_DIR / "data" / "ig-hl-video-state.json"
LOG_FILE   = ROOT_DIR / "logs" / "ig-hl-video.log"

API_VERSION = "v25.0"
CLAUDE_BIN  = "/opt/homebrew/bin/claude"

# How many source reels each highlight type draws from
HL_CONFIG = [
    {
        "name": "top-moments",
        "title": "TOP MOMENTS",
        "clip_count": 4,
        "clip_duration": 10,          # seconds per clip
        "sort": "engagement_score",   # absolute engagement
        "description": "Compilation of our biggest moments — fights, victories, intense training",
    },
    {
        "name": "best-action",
        "title": "BEST ACTION",
        "clip_count": 4,
        "clip_duration": 10,
        "sort": "engagement_score",
        "filter_keywords": ["luta", "fight", "vitoria", "nocaute", "treino",
                            "knockout", "submission", "camp", "sparring",
                            "ground", "octog", "cage", "round"],
        "description": "The most intense fight and training footage — strikes, takedowns, finishes",
    },
    {
        "name": "rising-reels",
        "title": "RISING REELS",
        "clip_count": 4,
        "clip_duration": 10,
        "sort": "engagement_rate",    # high rate but lower absolute reach
        "description": "Hidden gems with high engagement rate — emotional moments, behind the scenes",
    },
]

# ── helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"used_source_ids": [], "created_highlights": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fresh_media_url(media_id, token):
    """Fetch a non-expired CDN URL from the Graph API."""
    params = urllib.parse.urlencode({
        "fields": "media_url,thumbnail_url",
        "access_token": token,
    })
    url = f"https://graph.facebook.com/{API_VERSION}/{media_id}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("media_url", ""), data.get("thumbnail_url", "")
    except Exception as e:
        log(f"  fresh_media_url({media_id}): {e}")
        return "", ""


def get_video_duration(path_or_url):
    """Return video duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path_or_url],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ── source reel selection ─────────────────────────────────────────────────────

def fetch_all_reels(exclude_ids: set) -> list[dict]:
    """Fetch all reels with metrics from the DB, excluding already-used ones."""
    con = sqlite3.connect(str(DB_FILE))
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT
            m.id, m.caption, m.permalink, m.media_url, m.thumbnail_url,
            m.timestamp, m.like_count, m.comments_count,
            MAX(CASE WHEN i.metric='reach'     THEN i.value END) AS reach,
            MAX(CASE WHEN i.metric='views'     THEN i.value END) AS views,
            MAX(CASE WHEN i.metric='shares'    THEN i.value END) AS shares,
            MAX(CASE WHEN i.metric='saved'     THEN i.value END) AS saves,
            MAX(CASE WHEN i.metric='likes'     THEN i.value END) AS likes,
            MAX(CASE WHEN i.metric='comments'  THEN i.value END) AS comments,
            MAX(CASE WHEN i.metric='ig_reels_avg_watch_time' THEN i.value END) AS avg_watch_ms
        FROM ig_media m
        JOIN ig_media_insights i ON m.id = i.media_id
        WHERE m.media_product_type = 'REELS'
        GROUP BY m.id
        HAVING reach IS NOT NULL AND reach > 0
        ORDER BY m.timestamp DESC
    """).fetchall()
    con.close()

    reels = []
    for r in rows:
        rid = r["id"]
        if rid in exclude_ids:
            continue
        reach = r["reach"] or 1
        likes = r["likes"] or r["like_count"] or 0
        comments = r["comments"] or r["comments_count"] or 0
        shares = r["shares"] or 0
        saves = r["saves"] or 0
        interactions = likes + comments + shares + saves
        reels.append({
            "id":               rid,
            "caption":          r["caption"] or "",
            "permalink":        r["permalink"] or "",
            "media_url":        r["media_url"] or "",
            "thumbnail_url":    r["thumbnail_url"] or "",
            "timestamp":        r["timestamp"] or "",
            "reach":            reach,
            "views":            r["views"] or 0,
            "likes":            likes,
            "comments":         comments,
            "shares":           shares,
            "saves":            saves,
            "avg_watch_ms":     r["avg_watch_ms"] or 0,
            "engagement_score": likes + comments * 3 + shares * 5 + saves * 2,
            "engagement_rate":  round(interactions / reach * 100, 2) if reach else 0,
        })
    return reels


def select_sources(reels: list[dict], hl_cfg: dict, already_picked: set) -> list[dict]:
    """Pick source reels for one highlight type."""
    pool = [r for r in reels if r["id"] not in already_picked]

    # Optional keyword filter (for "best action")
    keywords = hl_cfg.get("filter_keywords")
    if keywords:
        filtered = [r for r in pool
                    if any(kw in (r["caption"] or "").lower() for kw in keywords)]
        if len(filtered) >= hl_cfg["clip_count"]:
            pool = filtered

    sort_key = hl_cfg["sort"]
    pool.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

    return pool[:hl_cfg["clip_count"]]


# ── AI clip selection ─────────────────────────────────────────────────────────

CLIP_PROMPT_TEMPLATE = """\
You are a professional MMA video editor cutting clips for a highlight reel.
Highlight: "{hl_title}" — {hl_description}

Source video: {duration:.1f}s long.
Caption: {caption}

I need a single clip from this video. Target ~{clip_dur}s but can be 7-15s — \
whatever gives the most NATURAL cut points.

CRITICAL EDITING RULES — these are NON-NEGOTIABLE:
1. NATURAL START: The clip MUST begin at a natural moment — a camera cut, \
the first word of a sentence, the start of a striking exchange, entrance \
into frame, or the beginning of a grappling sequence. NEVER start mid-action.
2. NATURAL END: The clip MUST end at a natural resting point — end of a combo, \
pause in speech, landed strike with follow-through, celebration beat, camera \
transition, or a submission locked in. NEVER cut mid-sentence or mid-strike.
3. The clip should feel like a COMPLETE moment with its own mini-arc: \
setup -> action -> resolution. A viewer should not feel like it was chopped.
4. Prefer: strikes landing, takedowns, celebrations, intense staredowns, \
training power moves, emotional moments.
5. For talking-head content: pick a complete thought or statement — start at \
the beginning of the sentence, end after the sentence finishes.
6. If the video is shorter than 12s, use the ENTIRE video (start=0, end={duration:.1f}).

Reply with ONLY a JSON object (no markdown fences, no explanation):
{{"start": <seconds>, "end": <seconds>, "reason": "<why these cut points are natural>"}}

Clip must fit within 0 to {duration:.1f}."""


def extract_analysis_frames(media_url: str, duration: float) -> list[tuple[bytes, str]]:
    """Extract 6 evenly-spaced frames for AI analysis."""
    frames = []
    if duration <= 0:
        return frames
    timestamps = [duration * i / 7 for i in range(1, 7)]
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, ts in enumerate(timestamps):
            out = os.path.join(tmpdir, f"frame_{i}.jpg")
            try:
                r = subprocess.run(
                    ["ffmpeg", "-ss", f"{ts:.2f}", "-i", media_url,
                     "-frames:v", "1", "-q:v", "4", "-y", out],
                    capture_output=True, timeout=15,
                )
                if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                    frames.append((open(out, "rb").read(), f"{ts:.1f}s"))
            except Exception:
                pass
    return frames


def ai_select_clip(reel: dict, media_url: str, duration: float,
                   hl_cfg: dict) -> dict:
    """Use Claude vision to pick the best clip from this video."""
    clip_dur = hl_cfg["clip_duration"]

    # If video is shorter than 12s, use the whole thing
    if duration <= 12:
        return {"start": 0, "end": duration, "reason": "video shorter than clip target"}

    frames = extract_analysis_frames(media_url, duration)
    if not frames:
        start = min(1.0, duration * 0.1)
        end = min(start + clip_dur, duration)
        return {"start": start, "end": end, "reason": "fallback (no frames)"}

    prompt = CLIP_PROMPT_TEMPLATE.format(
        hl_title=hl_cfg["title"],
        hl_description=hl_cfg["description"],
        duration=duration,
        caption=(reel.get("caption") or "")[:200],
        clip_dur=clip_dur,
    )

    # Build stream-json message with frames
    content = []
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
    content.append({"type": "text", "text": prompt})

    message = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    })

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print",
             "--input-format", "stream-json",
             "--output-format", "stream-json",
             "--verbose",
             "--model", "claude-haiku-4-5-20251001"],
            input=message, capture_output=True, text=True, timeout=45,
        )
        raw = result.stdout.strip()
        # Parse stream-json: find assistant message with actual text content
        # (skip thinking blocks which have empty text)
        text_content = ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "assistant":
                    c = msg.get("message", {}).get("content", "")
                    if isinstance(c, list):
                        t = " ".join(
                            b.get("text", "") for b in c
                            if b.get("type") == "text" and b.get("text", "").strip()
                        )
                        if t.strip():
                            text_content = t
                    elif isinstance(c, str) and c.strip():
                        text_content = c
            except json.JSONDecodeError:
                continue
        raw = text_content

        # Strip markdown fences
        raw = re.sub(r"^\s*```[a-z]*\s*", "", raw.strip())
        raw = re.sub(r"\s*```\s*$", "", raw).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        parsed = json.loads(raw)

        start = max(0, float(parsed["start"]))
        end = min(duration, float(parsed["end"]))
        # Allow 7-15s range; only clamp if way out of bounds
        if end - start < 5:
            end = min(start + clip_dur, duration)
        if end - start > 18:
            end = start + clip_dur
        # If end pushed past video, shift start back
        if end > duration:
            end = duration
            start = max(0, end - clip_dur)
        return {"start": round(start, 1), "end": round(end, 1),
                "reason": parsed.get("reason", "")}

    except Exception as e:
        log(f"    AI clip selection failed: {e}")
        start = min(1.0, duration * 0.1)
        end = min(start + clip_dur, duration)
        return {"start": start, "end": end, "reason": f"fallback ({e})"}


# ── video processing ──────────────────────────────────────────────────────────

def download_video(media_url: str, out_path: str) -> bool:
    """Download a video from CDN to local file."""
    try:
        req = urllib.request.Request(media_url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(out_path, "wb") as f:
                shutil.copyfileobj(r, f)
        return os.path.getsize(out_path) > 0
    except Exception as e:
        log(f"    Download failed: {e}")
        return False


def extract_clip(video_path: str, start: float, end: float,
                 out_path: str) -> bool:
    """Extract a clip from a local video file."""
    duration = end - start
    try:
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-ss", f"{start:.2f}",
             "-i", video_path,
             "-t", f"{duration:.2f}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception as e:
        log(f"    Extract clip failed: {e}")
        return False



def _render_text_image(path, text, subtext="", width=1080, height=1920):
    """Render centered text on a dark background using Pillow."""
    img = Image.new("RGB", (width, height), (17, 17, 23))
    draw = ImageDraw.Draw(img)
    font_main = font_sub = None
    for font_path in ["/System/Library/Fonts/Helvetica.ttc",
                      "/System/Library/Fonts/SFNSDisplay.ttf",
                      "/Library/Fonts/Arial.ttf"]:
        if os.path.exists(font_path):
            try:
                font_main = ImageFont.truetype(font_path, 56)
                font_sub = ImageFont.truetype(font_path, 32)
                break
            except Exception:
                continue
    if font_main is None:
        font_main = ImageFont.load_default()
        font_sub = font_main
    bbox = draw.textbbox((0, 0), text, font=font_main)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, height // 2 - 40), text, fill="white", font=font_main)
    if subtext:
        bbox2 = draw.textbbox((0, 0), subtext, font=font_sub)
        tw2 = bbox2[2] - bbox2[0]
        draw.text(((width - tw2) // 2, height // 2 + 30), subtext,
                  fill=(180, 180, 180), font=font_sub)
    img.save(path, "PNG")


def create_branded_card(out_path: str, text: str, subtext: str = "",
                        duration: float = 2.0) -> bool:
    """Create a branded intro/outro card using Pillow + ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        png_path = f.name
    try:
        _render_text_image(png_path, text, subtext)
        fade_in, fade_out = 0.4, 0.4
        vf = (f"loop=loop={int(duration * 30)}:size=1:start=0,"
              f"setpts=N/{30}/TB,"
              f"fade=t=in:st=0:d={fade_in},"
              f"fade=t=out:st={duration - fade_out}:d={fade_out}")
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", png_path,
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
             "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-c:a", "aac", "-b:a", "128k",
             "-t", f"{duration}", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", out_path],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False
    finally:
        if os.path.exists(png_path):
            os.unlink(png_path)


def normalize_clip(clip_path: str, out_path: str) -> bool:
    """Normalize a clip to 1080x1920 @ 30fps with consistent audio."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", clip_path,
             "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
                    "setsar=1,fps=30",
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def concatenate_with_crossfades(clip_paths: list[str], out_path: str,
                                xfade_dur: float = 0.5) -> bool:
    """Concatenate normalized clips with xfade transitions between them."""
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], out_path)
        return True

    # Get durations for offset calculations
    durations = []
    for cp in clip_paths:
        d = get_video_duration(cp)
        if d <= 0:
            return False
        durations.append(d)

    # Build xfade filter chain:
    #   [0:v][1:v]xfade=transition=fade:duration=D:offset=O[v01];
    #   [v01][2:v]xfade=...
    # Audio: acrossfade for each pair
    n = len(clip_paths)
    inputs = []
    for cp in clip_paths:
        inputs.extend(["-i", cp])

    vfilters = []
    afilters = []
    offset = durations[0] - xfade_dur
    prev_v = "[0:v]"
    prev_a = "[0:a]"

    for i in range(1, n):
        out_v = f"[v{i}]" if i < n - 1 else "[vout]"
        out_a = f"[a{i}]" if i < n - 1 else "[aout]"
        vfilters.append(
            f"{prev_v}[{i}:v]xfade=transition=fade:duration={xfade_dur}:offset={offset:.3f}{out_v}"
        )
        afilters.append(
            f"{prev_a}[{i}:a]acrossfade=d={xfade_dur}{out_a}"
        )
        offset += durations[i] - xfade_dur
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
             "-movflags", "+faststart",
             out_path],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0 and os.path.exists(out_path):
            return True
        log(f"    xfade failed: {r.stderr[-200:]}")
    except Exception as e:
        log(f"    xfade exception: {e}")

    # Fallback to simple concat if xfade fails
    log("    Falling back to simple concat")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")
        list_file = f.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file,
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0 and os.path.exists(out_path)
    finally:
        os.unlink(list_file)


# ── main pipeline ─────────────────────────────────────────────────────────────

def build_highlight(hl_cfg: dict, sources: list[dict], token: str,
                    work_dir: str) -> str | None:
    """Build one highlight reel. Returns output path or None."""
    hl_name = hl_cfg["name"]
    log(f"\n  Building highlight: {hl_cfg['title']} ({len(sources)} sources)")

    clip_paths = []
    for i, reel in enumerate(sources):
        log(f"    [{i+1}/{len(sources)}] {reel['id']} — "
            f"{reel['caption'][:50]}...")

        # Get fresh video URL
        media_url, _ = fresh_media_url(reel["id"], token)
        if not media_url:
            log(f"    Skipping — no media URL")
            continue

        # Download video
        video_path = os.path.join(work_dir, f"source_{hl_name}_{i}.mp4")
        if not download_video(media_url, video_path):
            continue

        # Get duration
        duration = get_video_duration(video_path)
        if duration < 3:
            log(f"    Skipping — too short ({duration:.1f}s)")
            continue
        log(f"    Duration: {duration:.1f}s")

        # AI picks the best segment
        clip_info = ai_select_clip(reel, video_path, duration, hl_cfg)
        log(f"    AI picked: {clip_info['start']:.1f}s-{clip_info['end']:.1f}s"
            f" ({clip_info.get('reason', '')})")

        # Extract the clip
        clip_path = os.path.join(work_dir, f"clip_{hl_name}_{i}.mp4")
        if extract_clip(video_path, clip_info["start"], clip_info["end"],
                        clip_path):
            clip_paths.append(clip_path)
        else:
            log(f"    Clip extraction failed")

        # Clean up source to save disk space
        os.remove(video_path)

    if not clip_paths:
        log(f"  No clips extracted for {hl_name}")
        return None

    # Normalize all clips to consistent format
    log(f"  Normalizing {len(clip_paths)} clips...")
    normalized = []
    for i, cp in enumerate(clip_paths):
        norm_path = os.path.join(work_dir, f"norm_{hl_name}_{i}.mp4")
        if normalize_clip(cp, norm_path):
            normalized.append(norm_path)

    if not normalized:
        log(f"  Normalization failed")
        return None

    # Create branded intro and outro cards
    intro_path = os.path.join(work_dir, f"intro_{hl_name}.mp4")
    outro_path = os.path.join(work_dir, f"outro_{hl_name}.mp4")
    create_branded_card(intro_path, "PEACEGRAPPLER", hl_cfg["title"], duration=2.0)
    create_branded_card(outro_path, "PEACEGRAPPLER", "@peacegrappler", duration=2.5)

    # Build final sequence: intro + clips with crossfades + outro
    all_parts = []
    if os.path.exists(intro_path):
        all_parts.append(intro_path)
    all_parts.extend(normalized)
    if os.path.exists(outro_path):
        all_parts.append(outro_path)

    log(f"  Joining {len(all_parts)} parts with crossfades...")
    final_path = os.path.join(work_dir, f"final_{hl_name}.mp4")
    if not concatenate_with_crossfades(all_parts, final_path, xfade_dur=0.5):
        log(f"  Concatenation failed")
        return None

    log(f"  Highlight {hl_name} ready: {final_path}")
    return final_path


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    log(f"=== Highlight Video Creation — {today} ===")

    env = load_env()
    token = env.get("TOKEN")
    if not token:
        log("ERROR: TOKEN not found in .env")
        sys.exit(1)

    if not DB_FILE.exists():
        log(f"ERROR: Database not found: {DB_FILE}")
        sys.exit(1)

    state = load_state()
    used_ids = set(state.get("used_source_ids", []))

    # Also exclude any highlight videos we previously created (by their IDs)
    created_ids = set()
    for h in state.get("created_highlights", []):
        created_ids.update(h.get("source_ids", []))

    exclude_ids = used_ids | created_ids

    # Fetch all eligible reels
    all_reels = fetch_all_reels(exclude_ids)
    log(f"Eligible source reels: {len(all_reels)} (excluding {len(exclude_ids)} used)")

    if len(all_reels) < 5:
        log("Not enough source reels to create highlights. Need at least 5.")
        sys.exit(0)

    # Select sources for each highlight type
    already_picked = set()  # avoid using same reel in multiple highlights
    hl_sources = []
    for cfg in HL_CONFIG:
        sources = select_sources(all_reels, cfg, already_picked)
        hl_sources.append((cfg, sources))
        already_picked.update(r["id"] for r in sources)
        log(f"  {cfg['name']}: selected {len(sources)} source reels")

    # Create output directory
    out_dir = OUTPUT_DIR / f"highlights-{today}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build each highlight in a temp working directory
    new_highlights = []
    all_used_ids = set()

    with tempfile.TemporaryDirectory() as work_dir:
        for idx, (cfg, sources) in enumerate(hl_sources, 1):
            if not sources:
                log(f"  Skipping {cfg['name']} — no sources")
                continue

            final_path = build_highlight(cfg, sources, token, work_dir)
            if not final_path:
                continue

            # Copy to output directory
            out_name = f"highlight-{idx}-{cfg['name']}.mp4"
            out_path = out_dir / out_name
            shutil.copy2(final_path, str(out_path))

            source_ids = [r["id"] for r in sources]
            all_used_ids.update(source_ids)

            new_highlights.append({
                "date": today,
                "name": cfg["name"],
                "title": cfg["title"],
                "file": str(out_path),
                "source_ids": source_ids,
                "source_count": len(sources),
            })
            log(f"\n  Saved: {out_path}")
            log(f"  Size: {os.path.getsize(str(out_path)) / 1024 / 1024:.1f} MB")

    # Update state
    state["used_source_ids"] = list(used_ids | all_used_ids)
    state.setdefault("created_highlights", []).extend(new_highlights)
    save_state(state)

    log(f"\n=== Done — {len(new_highlights)} highlight(s) created in {out_dir} ===")

    # Summary
    for h in new_highlights:
        log(f"  {h['title']}: {h['file']} ({h['source_count']} sources)")


if __name__ == "__main__":
    main()
