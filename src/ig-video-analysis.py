#!/usr/bin/env python3
"""
Daily Instagram video analysis.
Fetches all new Reels since last run, scores them against 90-day benchmarks,
and emails an HTML report with actionable feedback.
"""

import os
import sys
import json
import sqlite3
import smtplib
import re
import base64
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
DB_PATH   = ROOT_DIR / "peacegrappler.db"
STATE_FILE = ROOT_DIR / "data" / "video-analysis-state.json"
OUTPUT_DIR = ROOT_DIR / "output"
ENV_FILE   = ROOT_DIR / ".env"

def _load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file(ENV_FILE)

IG_TOKEN = os.environ.get("TOKEN", "")
API_VERSION = "v25.0"

SMTP_USER   = "paxlucta@gmail.com"
SMTP_PASS   = os.environ["GMAIL_PASSWORD_TOKEN"]
RECIPIENTS  = ["abghandour@gmail.com", "marcello1spinelli@gmail.com"]

# ─── state ───────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    # first run: look back 7 days
    default_ts = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"last_run": default_ts}

def save_state(ts: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"last_run": ts}, f)

# ─── database ────────────────────────────────────────────────────────────────

def _anchor(anchor_date):
    # SQL fragment that stands in for 'now'. Backfill anchors to a past date.
    return f"'{anchor_date}'" if anchor_date else "'now'"


def _insight_cutoff(anchor_date):
    """SQL fragment for 'as-of this point in time' filter on ig_media_insights.fetched_at.
    For live mode (anchor_date=None) no filter is applied — picks the most recent snapshot.
    For backfill mode it caps at end-of-anchor-day so we read that day's snapshot, not today's.
    """
    if not anchor_date:
        return ""
    return f"AND fetched_at < datetime('{anchor_date}', '+1 day')"


def _latest_insights_cte(anchor_date):
    """CTE that selects the latest insight row per (media, metric) as-of anchor.
    Returns SQL like: WITH latest_insights AS ( ... )
    Joined later via `JOIN latest_insights li ON li.media_id = m.id AND li.metric = ?`.
    """
    cutoff = _insight_cutoff(anchor_date)
    return f"""
        WITH latest_insights AS (
            SELECT media_id, metric, value
            FROM (
                SELECT media_id, metric, value,
                       ROW_NUMBER() OVER (PARTITION BY media_id, metric ORDER BY fetched_at DESC) AS rn
                FROM ig_media_insights
                WHERE 1=1 {cutoff}
            )
            WHERE rn = 1
        )
    """


def fetch_recent_reels(con, anchor_date=None):
    """Return reels from the 7 days preceding anchor_date (or now) with their as-of metrics."""
    a = _anchor(anchor_date)
    cte = _latest_insights_cte(anchor_date)
    rows = con.execute(f"""
        {cte}
        SELECT
            m.id,
            m.caption,
            m.timestamp,
            m.permalink,
            m.media_url,
            m.thumbnail_url,
            MAX(CASE WHEN li.metric='views'                            THEN li.value END) AS views,
            MAX(CASE WHEN li.metric='reach'                            THEN li.value END) AS reach,
            MAX(CASE WHEN li.metric='shares'                           THEN li.value END) AS shares,
            MAX(CASE WHEN li.metric='saved'                            THEN li.value END) AS saves,
            MAX(CASE WHEN li.metric='likes'                            THEN li.value END) AS likes,
            MAX(CASE WHEN li.metric='comments'                         THEN li.value END) AS comments,
            MAX(CASE WHEN li.metric='ig_reels_avg_watch_time'          THEN li.value END) AS avg_watch_ms,
            MAX(CASE WHEN li.metric='ig_reels_video_view_total_time'   THEN li.value END) AS total_watch_ms
        FROM ig_media m
        JOIN latest_insights li ON li.media_id = m.id
        WHERE m.media_product_type = 'REELS'
          AND m.timestamp >= datetime({a}, '-7 days')
          AND m.timestamp <  datetime({a}, '+1 day')
        GROUP BY m.id
        HAVING reach IS NOT NULL AND reach > 0
        ORDER BY m.timestamp DESC
    """).fetchall()
    return [dict(r) for r in rows]

def fetch_benchmarks(con, anchor_date=None):
    """90-day median-style benchmarks via percentile approximation.
    Picks one as-of snapshot per (media, metric) before averaging, so daily snapshot
    history doesn't double-count.
    """
    a = _anchor(anchor_date)
    cte = _latest_insights_cte(anchor_date)
    rows = con.execute(f"""
        {cte}
        SELECT
            AVG(CASE WHEN li.metric='views'                   THEN li.value END) AS avg_views,
            AVG(CASE WHEN li.metric='reach'                   THEN li.value END) AS avg_reach,
            AVG(CASE WHEN li.metric='shares'                  THEN li.value END) AS avg_shares,
            AVG(CASE WHEN li.metric='saved'                   THEN li.value END) AS avg_saves,
            AVG(CASE WHEN li.metric='likes'                   THEN li.value END) AS avg_likes,
            AVG(CASE WHEN li.metric='comments'                THEN li.value END) AS avg_comments,
            AVG(CASE WHEN li.metric='ig_reels_avg_watch_time' THEN li.value END) AS avg_watch_ms
        FROM ig_media m
        JOIN latest_insights li ON li.media_id = m.id
        WHERE m.media_product_type = 'REELS'
          AND m.timestamp >= datetime({a}, '-90 days')
          AND m.timestamp <  datetime({a}, '+1 day')
    """).fetchone()
    b = dict(rows) if rows else {}

    # p75/p25 thresholds — one as-of value per media per metric, then percentile
    # across media. OFFSET = floor(N * pct/100).
    def percentile(metric, pct):
        result = con.execute(f"""
            {cte}
            , per_media AS (
                SELECT li.value AS val
                FROM ig_media m
                JOIN latest_insights li ON li.media_id = m.id AND li.metric = ?
                WHERE m.media_product_type = 'REELS'
                  AND m.timestamp >= datetime({a}, '-90 days')
                  AND m.timestamp <  datetime({a}, '+1 day')
                  AND li.value IS NOT NULL
            )
            SELECT val FROM per_media ORDER BY val
            LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM per_media) * {pct} / 100.0 AS INT)
        """, (metric,)).fetchone()
        return result[0] if result else None

    b["p75_views"]    = percentile("views",    75)
    b["p75_reach"]    = percentile("reach",    75)
    b["p75_shares"]   = percentile("shares",   75)
    b["p75_saves"]    = percentile("saved",    75)
    b["p75_watch_ms"] = percentile("ig_reels_avg_watch_time", 75)
    b["p25_views"]    = percentile("views",    25)
    b["p25_reach"]    = percentile("reach",    25)
    b["p25_watch_ms"] = percentile("ig_reels_avg_watch_time", 25)
    return b

def fetch_all_reels_for_patterns(con, anchor_date=None):
    """Return last 90 days of reels for pattern mining."""
    a = _anchor(anchor_date)
    cte = _latest_insights_cte(anchor_date)
    rows = con.execute(f"""
        {cte}
        SELECT
            m.id, m.caption, m.timestamp,
            MAX(CASE WHEN li.metric='reach'                   THEN li.value END) AS reach,
            MAX(CASE WHEN li.metric='shares'                  THEN li.value END) AS shares,
            MAX(CASE WHEN li.metric='saved'                   THEN li.value END) AS saves,
            MAX(CASE WHEN li.metric='likes'                   THEN li.value END) AS likes,
            MAX(CASE WHEN li.metric='comments'                THEN li.value END) AS comments,
            MAX(CASE WHEN li.metric='ig_reels_avg_watch_time' THEN li.value END) AS avg_watch_ms
        FROM ig_media m
        JOIN latest_insights li ON li.media_id = m.id
        WHERE m.media_product_type = 'REELS'
          AND m.timestamp >= datetime({a}, '-90 days')
          AND m.timestamp <  datetime({a}, '+1 day')
        GROUP BY m.id
        HAVING reach IS NOT NULL AND reach > 0
    """).fetchall()
    return [dict(r) for r in rows]

# ─── AI video analysis ───────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are an Instagram Reels growth expert. You are looking at frames from an MMA / fight sports reel.

Frames provided:
- Frame 1: ~0.5s (the hook — what viewers see first)
- Frame 2: ~3s (early content)
- Frame 3: mid-video
- Frame 4: near end

Caption: {caption}
Engagement metrics: {metrics}

Analyze the video for what drives or hurts Instagram Reels performance. Consider:
HOOK (first 1-3s): text overlay, visual impact, scroll-stopping quality, faces/action
CONTENT: pacing, subtitles/captions on screen, clarity of the subject, production quality
ENGAGEMENT TRIGGERS: call to action, emotional reaction, shareability
TECHNICAL: aspect ratio use, text placement, audio cues visible

Reply ONLY with a JSON object — no markdown, no explanation outside the JSON:
{{
  "good": ["bullet 1", "bullet 2", ...],
  "bad": ["bullet 1", "bullet 2", ...],
  "top_tip": "single most impactful change to make"
}}

Each bullet should be specific and actionable (e.g. "Strong hook: fighter's face fills frame immediately" not just "good hook").
good list: 1-4 items. bad list: 1-4 items. If something doesn't apply, omit it."""

def _fresh_media_url(media_id: str) -> str:
    """Fetch a fresh (non-expired) media_url from the Graph API."""
    import urllib.request, urllib.parse
    params = urllib.parse.urlencode({"fields": "media_url,thumbnail_url", "access_token": IG_TOKEN})
    url = f"https://graph.facebook.com/{API_VERSION}/{media_id}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("media_url", ""), data.get("thumbnail_url", "")
    except Exception:
        return "", ""


def extract_frames(media_id: str, fallback_thumbnail: str) -> list[tuple[bytes, str]]:
    """
    Fetch a fresh media URL from the Graph API, then extract up to 4 frames
    via ffmpeg. Returns list of (jpeg_bytes, label) tuples.
    Falls back to thumbnail if video extraction fails.
    """
    media_url, thumb_url = _fresh_media_url(media_id)
    thumbnail_url = thumb_url or fallback_thumbnail

    frames = []
    if media_url:
        with tempfile.TemporaryDirectory() as tmpdir:
            for ts, label in [("0.5", "hook"), ("3", "early"), ("10", "mid"), ("20", "end")]:
                out = os.path.join(tmpdir, f"frame_{label}.jpg")
                try:
                    result = subprocess.run(
                        ["ffmpeg", "-ss", ts, "-i", media_url,
                         "-frames:v", "1", "-q:v", "4", "-y", out],
                        capture_output=True, timeout=20
                    )
                    if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                        frames.append((open(out, "rb").read(), label))
                except Exception:
                    pass

    # Fallback to thumbnail if no frames
    if not frames and thumbnail_url:
        import urllib.request
        try:
            with urllib.request.urlopen(thumbnail_url, timeout=10) as resp:
                frames.append((resp.read(), "thumbnail"))
        except Exception:
            pass

    return frames[:4]


def analyze_video_with_claude(reel: dict, bm: dict) -> dict:
    """
    Send video frames to Claude via CLI and return {good, bad, top_tip}.
    Returns empty lists on failure.
    """
    media_id      = reel["id"]
    thumbnail_url = reel.get("thumbnail_url") or ""
    caption       = (reel.get("caption") or "")[:300]

    er = 0
    reach = reel.get("reach") or 0
    if reach:
        interactions = sum(reel.get(k) or 0 for k in ("likes","comments","shares","saves"))
        er = round(interactions / reach * 100, 1)
    metrics_str = (
        f"views={reel.get('views') or 0}, reach={reach}, "
        f"shares={reel.get('shares') or 0}, saves={reel.get('saves') or 0}, "
        f"engagement_rate={er}%, "
        f"avg_watch={round((reel.get('avg_watch_ms') or 0)/1000, 1)}s"
    )

    frames = extract_frames(media_id, thumbnail_url)
    if not frames:
        return {"good": [], "bad": [], "top_tip": ""}

    prompt = ANALYSIS_PROMPT.format(caption=caption or "(no caption)", metrics=metrics_str)

    # Build stream-json message with all frames + prompt
    content = []
    for jpeg_bytes, label in frames:
        content.append({
            "type": "text",
            "text": f"[Frame: {label}]"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(jpeg_bytes).decode()
            }
        })
    content.append({"type": "text", "text": prompt})

    message = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content}
    })

    # Override Claude Code's interactive coding-agent persona with a strict
    # JSON-only system prompt; otherwise the agent asks clarifying questions
    # instead of returning the requested JSON object.
    system_override = (
        "You are a JSON-only response service for an Instagram Reels analyzer. "
        "You MUST respond with ONLY a valid JSON object matching this schema "
        "exactly: {\"good\": [\"bullet\", ...], \"bad\": [\"bullet\", ...], "
        "\"top_tip\": \"single most impactful change\"}. "
        "Do not ask clarifying questions. Do not explain. Do not refuse. "
        "Do not write any prose outside the JSON. Do not wrap in markdown fences. "
        "If insufficient signal, return empty arrays and an empty top_tip."
    )

    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/claude", "--print",
             "--input-format", "stream-json",
             "--output-format", "stream-json",
             "--verbose",
             "--model", "claude-haiku-4-5-20251001",
             "--append-system-prompt", system_override],
            input=message, capture_output=True, text=True, timeout=120
        )
        raw_text = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("type") == "assistant":
                    for block in d.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            raw_text += block["text"]
                elif d.get("type") == "result" and d.get("result"):
                    raw_text = d["result"]
            except Exception:
                pass

        # Parse JSON from response — strip markdown fences if present
        raw_text = re.sub(r"^\s*```[a-z]*\s*", "", raw_text.strip())
        raw_text = re.sub(r"\s*```\s*$", "", raw_text).strip()
        # Find the outermost JSON object in case there's surrounding text
        m = re.search(r"\{[\s\S]*\}", raw_text)
        if m:
            raw_text = m.group(0)
        parsed = json.loads(raw_text)
        return {
            "good":    parsed.get("good", []),
            "bad":     parsed.get("bad", []),
            "top_tip": parsed.get("top_tip", ""),
        }
    except Exception:
        # Don't surface internal errors as user-facing "what to fix" bullets;
        # leave the AI fields empty and the report renders "No observations".
        return {"good": [], "bad": [], "top_tip": ""}


# ─── analysis ────────────────────────────────────────────────────────────────

def engagement_rate(r):
    reach = r.get("reach") or 0
    if reach == 0:
        return 0.0
    interactions = (r.get("likes") or 0) + (r.get("comments") or 0) + \
                   (r.get("shares") or 0) + (r.get("saves") or 0)
    return round(interactions / reach * 100, 2)

def score_reel(r, bm):
    """Return a 0-100 composite score."""
    def norm(val, avg, p75):
        if not avg or avg == 0:
            return 50
        p75 = p75 or avg * 1.5
        ratio = (val or 0) / avg
        return min(100, int(ratio * 50))  # 50 = avg, 100 = 2x avg

    s_views  = norm(r.get("views"),     bm.get("avg_views"),    bm.get("p75_views"))
    s_reach  = norm(r.get("reach"),     bm.get("avg_reach"),    bm.get("p75_reach"))
    s_shares = norm(r.get("shares"),    bm.get("avg_shares"),   bm.get("p75_shares")) * 2  # shares weighted
    s_saves  = norm(r.get("saves"),     bm.get("avg_saves"),    bm.get("p75_saves")) * 1.5
    s_watch  = norm(r.get("avg_watch_ms"), bm.get("avg_watch_ms"), bm.get("p75_watch_ms"))
    s_eng    = min(100, int(engagement_rate(r) * 10))
    total = (s_views + s_reach + s_shares + s_saves + s_watch + s_eng) / 7
    return round(total)

def tier(score):
    if score >= 65:  return ("Top Performer", "#34d399", "🔥")
    if score >= 40:  return ("Average",       "#f59e0b", "📊")
    return               ("Needs Work",     "#f87171", "⚠️")

def extract_caption_signals(caption):
    if not caption:
        return {}
    text = caption.lower()
    return {
        "has_ufc":         "ufc" in text,
        "has_fight":       any(w in text for w in ["fight", "luta", "combate"]),
        "has_ko":          any(w in text for w in ["ko", "nocaute", "knockout", "finish"]),
        "has_interview":   any(w in text for w in ["fala", "diz", "conta", "revela", "entrevista"]),
        "has_prediction":  any(w in text for w in ["vai", "vai vencer", "vai nocautear", "cravou", "aposta"]),
        "has_question":    "?" in caption,
        "caption_len":     len(caption),
        "hashtag_count":   caption.count("#"),
        "has_local":       any(w in text for w in ["lfa", "brasileiro", "brasil", "nacional"]),
        "is_short":        len(caption) < 150,
        "is_medium":       150 <= len(caption) < 350,
        "is_long":         len(caption) >= 350,
    }

def mine_patterns(all_reels, bm):
    """Compare top-third vs bottom-third reels to surface patterns."""
    if not all_reels:
        return [], []

    scored = [(r, score_reel(r, bm)) for r in all_reels]
    scored.sort(key=lambda x: x[1], reverse=True)

    n = max(3, len(scored) // 3)
    top    = [r for r, _ in scored[:n]]
    bottom = [r for r, _ in scored[-n:]]

    def avg_signal(reels, key):
        vals = [extract_caption_signals(r.get("caption", "")).get(key, False) for r in reels]
        if isinstance(vals[0], bool):
            return sum(vals) / len(vals)
        return sum(v for v in vals if v) / len(vals) if vals else 0

    signal_keys = ["has_ufc", "has_ko", "has_interview", "has_prediction",
                   "has_question", "has_local", "is_short", "is_medium", "is_long"]

    labels = {
        "has_ufc":        "UFC-related content",
        "has_ko":         "KO / knockout / nocaute content",
        "has_interview":  "Interview / fighter quote content",
        "has_prediction": "Prediction / bold claim content",
        "has_question":   "Caption with a question",
        "has_local":      "Local / Brazilian MMA content",
        "is_short":       "Short captions (<150 chars)",
        "is_medium":      "Medium captions (150–350 chars)",
        "is_long":        "Long captions (>350 chars)",
    }

    top_patterns    = []
    avoid_patterns  = []

    for key in signal_keys:
        top_rate    = avg_signal(top,    key)
        bottom_rate = avg_signal(bottom, key)
        diff = top_rate - bottom_rate
        if diff >= 0.25 and top_rate >= 0.4:
            top_patterns.append((labels[key], top_rate, bottom_rate))
        elif diff <= -0.25 and bottom_rate >= 0.4:
            avoid_patterns.append((labels[key], top_rate, bottom_rate))

    # watch time insight
    top_watch    = sum(r.get("avg_watch_ms") or 0 for r in top)    / len(top)
    bottom_watch = sum(r.get("avg_watch_ms") or 0 for r in bottom) / len(bottom)

    # share/save per reach
    def ratio(reels, metric):
        vals = [(r.get(metric) or 0) / max(r.get("reach") or 1, 1) for r in reels]
        return sum(vals) / len(vals)

    top_share_rate    = ratio(top,    "shares")
    bottom_share_rate = ratio(bottom, "shares")

    return top_patterns, avoid_patterns, top_watch, bottom_watch, top_share_rate, bottom_share_rate

def fmt_watch(ms):
    if not ms:
        return "—"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    return f"{s/60:.1f}m"

def fmt_num(n):
    if n is None:
        return "—"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(int(n))

# ─── HTML generation ─────────────────────────────────────────────────────────

CSS = """
<link rel="stylesheet" href="styles.css">
<style>
/* video-analysis daily report tweaks */
body{padding:24px;}
.wrap{max-width:900px;margin:0 auto;}
h1{font-size:28px;color:var(--text);margin-bottom:4px;}
.sub{color:var(--muted);font-size:13px;margin-bottom:32px;}
h2{font-size:18px;color:var(--text);margin:32px 0 14px;border-bottom:1px solid var(--border);padding-bottom:8px;text-transform:none;letter-spacing:0;}
h3{font-size:15px;color:var(--text);margin-bottom:8px;text-transform:none;letter-spacing:0;}
.kpi-row{display:table;width:100%;border-collapse:separate;border-spacing:12px;}
.kpi{display:table-cell;background:var(--card);border:1px solid var(--border);
  border-radius:10px;padding:16px 20px;text-align:center;width:16%;}
.kpi .val{font-size:24px;font-weight:700;color:var(--accent);}
.kpi .lbl{font-size:11px;color:var(--muted);margin-top:2px;}
.badge.top{background:#d1fae5;color:#047857;}
.badge.avg{background:#fef3c7;color:#92400e;}
.badge.low{background:#fee2e2;color:#b91c1c;}
.score{font-size:18px;font-weight:700;}
.bar-wrap{background:#f3f4f6;border-radius:4px;height:6px;margin-top:4px;}
.bar{height:6px;border-radius:4px;}
.cap{color:var(--muted);font-size:12px;max-width:280px;line-height:1.4;}
.cap a{color:var(--accent);text-decoration:none;}
.insight-list{list-style:none;}
.insight-list li{padding:10px 14px;border-left:3px solid var(--accent);
  background:var(--card);margin-bottom:8px;border-radius:0 8px 8px 0;font-size:14px;border-top:1px solid var(--border);border-right:1px solid var(--border);border-bottom:1px solid var(--border);}
.insight-list.avoid li{border-left-color:var(--red);}
.insight-list.do li{border-left-color:var(--green);}
.pct{font-weight:700;color:var(--accent);}
.no-new{background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:32px;text-align:center;color:var(--muted);}
.reel-card{background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:20px;margin-bottom:20px;}
.reel-header{display:flex;align-items:flex-start;gap:16px;margin-bottom:14px;}
.reel-score-block{text-align:center;min-width:60px;}
.reel-meta{flex:1;}
.reel-caption{font-size:13px;color:var(--muted);margin-top:4px;line-height:1.5;}
.reel-stats{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px;font-size:13px;}
.stat{color:var(--muted);}
.stat b{color:var(--text);}
.ai-section{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;}
.ai-col{background:var(--page);border:1px solid var(--border);border-radius:8px;padding:12px 14px;}
.ai-col h4{font-size:12px;font-weight:700;letter-spacing:.05em;margin-bottom:8px;text-transform:uppercase;}
.ai-col.good h4{color:var(--green);}
.ai-col.bad  h4{color:var(--red);}
.ai-col ul{list-style:none;padding:0;}
.ai-col li{font-size:12px;line-height:1.5;padding:3px 0;border-bottom:1px solid var(--border);}
.ai-col li:last-child{border-bottom:none;}
.ai-col.good li::before{content:"✓ ";color:var(--green);font-weight:700;}
.ai-col.bad  li::before{content:"✗ ";color:var(--red);font-weight:700;}
.top-tip{font-size:12px;color:#92400e;margin-top:10px;padding:8px 12px;
  background:#fef3c7;border-radius:6px;border-left:3px solid var(--orange);}
.top-tip b{color:#78350f;}
</style>
"""

def render_bar(value, avg, max_val=None):
    if not value or not avg:
        return ""
    pct = min(100, int((value / (max_val or avg * 2)) * 100))
    color = "#34d399" if value >= avg else "#f87171"
    return f'<div class="bar-wrap"><div class="bar" style="width:{pct}%;background:{color}"></div></div>'

def build_html(new_reels, bm, all_reels, run_time, ai_analyses=None):
    today = run_time.strftime("%Y-%m-%d")
    ai_analyses = ai_analyses or {}

    scored_new = [(r, score_reel(r, bm)) for r in new_reels]
    scored_new.sort(key=lambda x: x[1], reverse=True)

    pattern_result = mine_patterns(all_reels, bm)
    if len(pattern_result) == 6:
        top_pats, avoid_pats, top_watch, bottom_watch, top_share, bottom_share = pattern_result
    else:
        top_pats = avoid_pats = []
        top_watch = bottom_watch = top_share = bottom_share = 0

    # aggregate summary of new reels
    def avg_metric(key):
        vals = [r.get(key) or 0 for r, _ in scored_new if r.get(key)]
        return round(sum(vals) / len(vals)) if vals else 0

    avg_views   = avg_metric("views")
    avg_reach   = avg_metric("reach")
    avg_shares  = avg_metric("shares")
    avg_saves   = avg_metric("saves")
    avg_watch   = avg_metric("avg_watch_ms")
    avg_er      = round(sum(engagement_rate(r) for r, _ in scored_new) / len(scored_new), 2) if scored_new else 0

    bm_views  = int(bm.get("avg_views")    or 0)
    bm_reach  = int(bm.get("avg_reach")    or 0)
    bm_watch  = int(bm.get("avg_watch_ms") or 0)

    def delta_arrow(val, ref):
        if not ref:
            return ""
        pct = round((val - ref) / ref * 100)
        if pct > 0:
            return f' <span style="color:#34d399;font-size:11px">▲{pct}%</span>'
        elif pct < 0:
            return f' <span style="color:#f87171;font-size:11px">▼{abs(pct)}%</span>'
        return ""

    parts = []
    parts.append(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><title>Video Analysis {today}</title>{CSS}</head><body>
<div class="wrap">
<h1>Instagram Video Analysis</h1>
<div class="sub">Reels from the last 7 days with metrics &mdash; Generated {run_time.strftime("%b %d, %Y %I:%M %p")}</div>
""")

    if not new_reels:
        parts.append("""
<div class="no-new">
  <h3>No reels with metrics in the last 7 days</h3>
  <p style="margin-top:8px;font-size:13px;">Check back after the next sync or post new content.</p>
</div>
""")
    else:
        # ── KPI summary bar ──────────────────────────────────────────────────
        parts.append(f"""
<h2>New Content Summary ({len(new_reels)} reel{"s" if len(new_reels)!=1 else ""})</h2>
<table class="kpi-row">
<tr>
<td class="kpi"><div class="val">{fmt_num(avg_views)}{delta_arrow(avg_views, bm_views)}</div><div class="lbl">Avg Views</div></td>
<td class="kpi"><div class="val">{fmt_num(avg_reach)}{delta_arrow(avg_reach, bm_reach)}</div><div class="lbl">Avg Reach</div></td>
<td class="kpi"><div class="val">{fmt_num(avg_shares)}</div><div class="lbl">Avg Shares</div></td>
<td class="kpi"><div class="val">{fmt_num(avg_saves)}</div><div class="lbl">Avg Saves</div></td>
<td class="kpi"><div class="val">{fmt_watch(avg_watch)}{delta_arrow(avg_watch, bm_watch)}</div><div class="lbl">Avg Watch</div></td>
<td class="kpi"><div class="val">{avg_er}%</div><div class="lbl">Eng Rate</div></td>
</tr>
</table>
""")

        # ── Per-reel cards ───────────────────────────────────────────────────
        parts.append(f"<h2>Per-Reel Breakdown ({len(new_reels)} reel{'s' if len(new_reels)!=1 else ''})</h2>")
        for r, sc in scored_new:
            t_label, t_color, t_icon = tier(sc)
            er = engagement_rate(r)
            caption_text = (r.get("caption") or "").replace("<", "&lt;")
            caption_short = caption_text[:120] + ("…" if len(caption_text) > 120 else "")
            permalink = r.get("permalink", "")
            posted = r.get("timestamp", "")[:10]
            watch_val = r.get("avg_watch_ms") or 0
            watch_color = "#34d399" if watch_val >= bm_watch else "#f87171"
            share_color = "#34d399" if (r.get("shares") or 0) >= (bm.get("avg_shares") or 0) else "var(--text)"

            ai = ai_analyses.get(r["id"], {})
            # Filter out internal failure messages from old sidecars so they
            # don't appear as user-facing "what to fix" bullets.
            def _ok(s):
                if not isinstance(s, str): return False
                low = s.lower().strip()
                return not (
                    low.startswith("analysis error")
                    or low.startswith("could not load video frames")
                )
            good_items = [g for g in ai.get("good", []) if _ok(g)]
            bad_items  = [b for b in ai.get("bad", [])  if _ok(b)]
            top_tip    = ai.get("top_tip", "") or ""

            good_html = "".join(f"<li>{item.replace('<','&lt;')}</li>" for item in good_items) or "<li>No observations</li>"
            bad_html  = "".join(f"<li>{item.replace('<','&lt;')}</li>" for item in bad_items)  or "<li>No observations</li>"
            tip_html  = f'<div class="top-tip"><b>Top tip:</b> {top_tip.replace("<","&lt;")}</div>' if top_tip else ""

            parts.append(f"""
<div class="reel-card">
  <div class="reel-header">
    <div class="reel-score-block">
      <span class="score" style="color:{t_color}">{sc}</span><br>
      <span class="badge {'top' if sc>=65 else 'avg' if sc>=40 else 'low'}">{t_icon} {t_label}</span>
    </div>
    <div class="reel-meta">
      <a href="{permalink}" target="_blank" style="color:var(--accent2);font-size:14px;font-weight:600;text-decoration:none">
        {caption_short or "(no caption)"}
      </a>
      <div style="color:var(--muted);font-size:12px;margin-top:3px">{posted}</div>
    </div>
  </div>
  <div class="reel-stats">
    <span class="stat">👁 Views <b>{fmt_num(r.get("views"))}</b>{render_bar(r.get("views"), bm_views)}</span>
    <span class="stat">📡 Reach <b>{fmt_num(r.get("reach"))}</b>{render_bar(r.get("reach"), bm_reach)}</span>
    <span class="stat" style="color:{share_color}">🔁 Shares <b>{fmt_num(r.get("shares"))}</b></span>
    <span class="stat">🔖 Saves <b>{fmt_num(r.get("saves"))}</b></span>
    <span class="stat">❤️ Likes <b>{fmt_num(r.get("likes"))}</b></span>
    <span class="stat">💬 Comments <b>{fmt_num(r.get("comments"))}</b></span>
    <span class="stat" style="color:{watch_color}">⏱ Watch <b>{fmt_watch(r.get("avg_watch_ms"))}</b></span>
    <span class="stat">💥 Eng <b style="color:{'#34d399' if er>=5 else 'var(--muted)'}">{er}%</b></span>
  </div>
  <div class="ai-section">
    <div class="ai-col good">
      <h4>WHAT WORKED</h4>
      <ul>{good_html}</ul>
    </div>
    <div class="ai-col bad">
      <h4>WHAT TO FIX</h4>
      <ul>{bad_html}</ul>
    </div>
  </div>
  {tip_html}
</div>""")

    # ── Pattern insights ─────────────────────────────────────────────────────
    parts.append("<h2>What to Keep Doing</h2>")
    if top_pats:
        parts.append('<ul class="insight-list do">')
        for label, top_r, bot_r in top_pats:
            parts.append(f'<li><b>{label}</b> — present in <span class="pct">{int(top_r*100)}%</span> of top reels vs {int(bot_r*100)}% of weak reels. Keep it up.</li>')
        parts.append("</ul>")
    else:
        parts.append('<p style="color:var(--muted);font-size:13px;">Not enough data yet — patterns will appear after more reels are posted and analyzed.</p>')

    if top_watch and bottom_watch:
        ratio = top_watch / bottom_watch if bottom_watch else 1
        if ratio > 1.3:
            parts.append(f'<ul class="insight-list do" style="margin-top:8px"><li>'
                         f'<b>Watch time in top performers is {ratio:.1f}× higher</b> — the hook and pacing in your best reels work. '
                         f'Top: {fmt_watch(top_watch)} avg vs bottom: {fmt_watch(bottom_watch)} avg.</li></ul>')

    if top_share and bottom_share and top_share > bottom_share * 1.5:
        parts.append(f'<ul class="insight-list do" style="margin-top:8px"><li>'
                     f'<b>Top reels get {(top_share/max(bottom_share,0.001)):.1f}× more shares per viewer</b> — '
                     f'your best content triggers sharing instincts. Keep creating that type.</li></ul>')

    parts.append("<h2>What to Stop / Fix</h2>")
    if avoid_pats:
        parts.append('<ul class="insight-list avoid">')
        for label, top_r, bot_r in avoid_pats:
            parts.append(f'<li><b>{label}</b> — present in <span class="pct">{int(bot_r*100)}%</span> of weak reels vs {int(top_r*100)}% of top reels. Reconsider this approach.</li>')
        parts.append("</ul>")
    else:
        parts.append('<p style="color:var(--muted);font-size:13px;">No strong negative patterns detected yet.</p>')

    # watch-time specific advice
    if new_reels:
        low_watch_reels = [r for r, sc in scored_new if (r.get("avg_watch_ms") or 0) < (bm_watch * 0.6)]
        if low_watch_reels:
            parts.append(f"""
<ul class="insight-list avoid" style="margin-top:8px">
  <li><b>{len(low_watch_reels)} reel{"s" if len(low_watch_reels)!=1 else ""} had watch time well below average (&lt;60% of {fmt_watch(bm_watch)})</b> —
  viewers are dropping off early. The first 2–3 seconds likely aren't grabbing attention.
  Try opening with a bold statement, action shot, or question before anything else.</li>
</ul>""")

        high_reach_low_eng = [r for r, sc in scored_new if (r.get("reach") or 0) > bm_reach and engagement_rate(r) < 3]
        if high_reach_low_eng:
            parts.append(f"""
<ul class="insight-list avoid" style="margin-top:8px">
  <li><b>{len(high_reach_low_eng)} reel{"s" if len(high_reach_low_eng)!=1 else ""} had above-average reach but low engagement (&lt;3%)</b> —
  you're reaching new people but they're not interacting.
  Add a clear call-to-action in the caption: ask a question, ask them to tag someone, or ask for an opinion.</li>
</ul>""")

    # ── Benchmark reference ──────────────────────────────────────────────────
    parts.append(f"""
<h2>90-Day Benchmarks</h2>
<table style="width:auto">
<thead><tr><th>Metric</th><th>90-Day Average</th><th>Top Quartile (P75)</th></tr></thead>
<tbody>
<tr><td>Views</td><td>{fmt_num(bm.get("avg_views"))}</td><td>{fmt_num(bm.get("p75_views"))}</td></tr>
<tr><td>Reach</td><td>{fmt_num(bm.get("avg_reach"))}</td><td>{fmt_num(bm.get("p75_reach"))}</td></tr>
<tr><td>Shares</td><td>{fmt_num(bm.get("avg_shares"))}</td><td>{fmt_num(bm.get("p75_shares"))}</td></tr>
<tr><td>Saves</td><td>{fmt_num(bm.get("avg_saves"))}</td><td>{fmt_num(bm.get("p75_saves"))}</td></tr>
<tr><td>Avg Watch Time</td><td>{fmt_watch(bm.get("avg_watch_ms"))}</td><td>{fmt_watch(bm.get("p75_watch_ms"))}</td></tr>
</tbody>
</table>
""")

    parts.append(f"""
<div class="footer">
  PeaceGrappler Video Analysis &mdash; {run_time.strftime("%Y-%m-%d %H:%M")} &mdash; Data from Instagram Graph API
</div>
</div></body></html>""")

    return "".join(parts)

# ─── email ────────────────────────────────────────────────────────────────────

def send_email(subject, html_content, recipients):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText("See the HTML version of this email.", "plain"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, recipients, msg.as_string())

# ─── main ─────────────────────────────────────────────────────────────────────

def fetch_reels_by_ids(con, reel_ids, anchor_date=None):
    """Fetch as-of metrics from DB for a fixed list of reel IDs.
    Defaults to latest snapshots; pass anchor_date for historical regeneration.
    """
    if not reel_ids:
        return []
    placeholders = ",".join(["?"] * len(reel_ids))
    cte = _latest_insights_cte(anchor_date)
    rows = con.execute(f"""
        {cte}
        SELECT
            m.id, m.caption, m.timestamp, m.permalink, m.media_url, m.thumbnail_url,
            MAX(CASE WHEN li.metric='views'                            THEN li.value END) AS views,
            MAX(CASE WHEN li.metric='reach'                            THEN li.value END) AS reach,
            MAX(CASE WHEN li.metric='shares'                           THEN li.value END) AS shares,
            MAX(CASE WHEN li.metric='saved'                            THEN li.value END) AS saves,
            MAX(CASE WHEN li.metric='likes'                            THEN li.value END) AS likes,
            MAX(CASE WHEN li.metric='comments'                         THEN li.value END) AS comments,
            MAX(CASE WHEN li.metric='ig_reels_avg_watch_time'          THEN li.value END) AS avg_watch_ms,
            MAX(CASE WHEN li.metric='ig_reels_video_view_total_time'   THEN li.value END) AS total_watch_ms
        FROM ig_media m
        JOIN latest_insights li ON li.media_id = m.id
        WHERE m.id IN ({placeholders})
        GROUP BY m.id
        ORDER BY m.timestamp DESC
    """, reel_ids).fetchall()
    return [dict(r) for r in rows]


def regenerate_for_date(target_date: str, rerun_ai: bool = False):
    """Re-render an existing video-analysis-{date} report against current DB metrics.
    Reads the existing JSON sidecar to learn which reel IDs were analyzed that day,
    pulls fresh per-reel metrics from the DB, and rebuilds the HTML. By default
    preserves the AI good/bad/top_tip fields from the sidecar; pass rerun_ai=True
    to re-call Claude and produce fresh AI bullets (costs ~$0.01 per reel).
    Does not send email, does not update the state file.
    """
    sidecar_path = OUTPUT_DIR / f"video-analysis-{target_date}.json"
    if not sidecar_path.exists():
        print(f"[skip] no sidecar at {sidecar_path}")
        return False

    with open(sidecar_path) as f:
        sidecar = json.load(f)

    reel_ids = [r["id"] for r in sidecar.get("reels", [])]
    print(f"[{target_date}] regenerating {len(reel_ids)} reels")

    # Anchor run_time at noon UTC on the target date so the report labels stay correct
    run_time = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=12)

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    new_reels = fetch_reels_by_ids(con, reel_ids)
    all_reels = fetch_all_reels_for_patterns(con)
    bm = fetch_benchmarks(con)
    con.close()

    # Preserve existing AI analyses from the sidecar (don't re-call Claude),
    # but strip internal failure messages so they don't surface in the report.
    def _clean_ai(strs):
        out = []
        for s in strs or []:
            if not isinstance(s, str):
                continue
            low = s.lower().strip()
            if low.startswith("analysis error") or low.startswith("could not load video frames"):
                continue
            out.append(s)
        return out

    if rerun_ai:
        print(f"  re-running AI on {len(new_reels)} reels (one Claude call each)...")
        ai_analyses = {}
        for i, r in enumerate(new_reels):
            print(f"    [{i+1}/{len(new_reels)}] {r['id']}")
            ai_analyses[r["id"]] = analyze_video_with_claude(r, bm)
    else:
        ai_analyses = {
            r["id"]: {
                "good": _clean_ai(r.get("good")),
                "bad": _clean_ai(r.get("bad")),
                "top_tip": r.get("top_tip", "") or "",
            }
            for r in sidecar.get("reels", [])
        }

    html = build_html(new_reels, bm, all_reels, run_time, ai_analyses)
    out_path = OUTPUT_DIR / f"video-analysis-{target_date}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Update the sidecar with refreshed scores + metrics, keeping AI fields
    scored = [(r, score_reel(r, bm)) for r in new_reels]
    scored.sort(key=lambda x: x[1], reverse=True)
    new_sidecar = {
        "date": target_date,
        "reels": [
            {
                "id":        r["id"],
                "permalink": r.get("permalink", ""),
                "caption":   (r.get("caption") or "")[:120],
                "score":     sc,
                "tier":      tier(sc)[0],
                "views":     r.get("views"),
                "reach":     r.get("reach"),
                "timestamp": (r.get("timestamp") or "")[:10],
                "good":      ai_analyses.get(r["id"], {}).get("good", []),
                "bad":       ai_analyses.get(r["id"], {}).get("bad", []),
                "top_tip":   ai_analyses.get(r["id"], {}).get("top_tip", ""),
            }
            for r, sc in scored
        ],
    }
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(new_sidecar, f)
    print(f"  wrote {out_path.name}")
    return True


def backfill_for_date(target_date: str, skip_ai: bool = False):
    """Generate a video-analysis report dated to a past day (no email, no state update).

    Honest historical mode: reel selection AND per-reel metrics are anchored to
    target_date via `ig_media_insights.fetched_at <= target_date`. Requires that
    daily syncs were running on/before the target date; otherwise returns empty
    (the DB has no snapshots to read).

    If you want to refresh an existing sidecar against today's metrics instead
    (e.g., to update view counts as a reel matures), use --regen-date YYYY-MM-DD.
    """
    out_html = OUTPUT_DIR / f"video-analysis-{target_date}.html"
    if out_html.exists():
        print(f"[skip] {out_html.name} already exists")
        return False

    run_time = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=12)
    print(f"[backfill {target_date}] fetching reels (7d window anchored to {target_date})")

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    new_reels = fetch_recent_reels(con, anchor_date=target_date)
    all_reels = fetch_all_reels_for_patterns(con, anchor_date=target_date)
    bm = fetch_benchmarks(con, anchor_date=target_date)
    con.close()

    print(f"  reels with metrics in window: {len(new_reels)}")
    if not new_reels:
        check_con = sqlite3.connect(str(DB_PATH))
        first_snap = check_con.execute("SELECT MIN(fetched_at) FROM ig_media_insights").fetchone()[0]
        check_con.close()
        if first_snap and first_snap > target_date:
            print(f"  [warn] no insight snapshots exist on or before {target_date} "
                  f"(earliest snapshot: {first_snap[:10]}). Daily-snapshot history "
                  f"starts after that date; older backfills will be empty.")

    ai_analyses = {}
    if skip_ai:
        for r in new_reels:
            ai_analyses[r["id"]] = {"good": [], "bad": [], "top_tip": ""}
    else:
        for i, r in enumerate(new_reels):
            print(f"  Analyzing reel {i+1}/{len(new_reels)}: {r['id']}...")
            ai_analyses[r["id"]] = analyze_video_with_claude(r, bm)

    html = build_html(new_reels, bm, all_reels, run_time, ai_analyses)
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {out_html.name}")

    scored = [(r, score_reel(r, bm)) for r in new_reels]
    scored.sort(key=lambda x: x[1], reverse=True)
    sidecar = {
        "date": target_date,
        "reels": [
            {
                "id":        r["id"],
                "permalink": r.get("permalink", ""),
                "caption":   (r.get("caption") or "")[:120],
                "score":     sc,
                "tier":      tier(sc)[0],
                "views":     r.get("views"),
                "reach":     r.get("reach"),
                "timestamp": (r.get("timestamp") or "")[:10],
                "good":      ai_analyses.get(r["id"], {}).get("good", []),
                "bad":       ai_analyses.get(r["id"], {}).get("bad", []),
                "top_tip":   ai_analyses.get(r["id"], {}).get("top_tip", ""),
            }
            for r, sc in scored
        ],
    }
    sidecar_path = OUTPUT_DIR / f"video-analysis-{target_date}.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)
    print(f"  wrote {sidecar_path.name}")
    return True


def main():
    # CLI flags:
    #   --regen-date YYYY-MM-DD       re-render one day from its sidecar
    #   --regen-all                   re-render every sidecar
    #   --rerun-ai                    pair with above to also re-call Claude
    #                                 (costs ~$0.01/reel in subscription credits)
    rerun_ai = "--rerun-ai" in sys.argv
    if "--regen-date" in sys.argv:
        idx = sys.argv.index("--regen-date")
        target = sys.argv[idx + 1]
        regenerate_for_date(target, rerun_ai=rerun_ai)
        return
    if "--regen-all" in sys.argv:
        sidecars = sorted(OUTPUT_DIR.glob("video-analysis-*.json"))
        for sc in sidecars:
            date = sc.stem.replace("video-analysis-", "")
            regenerate_for_date(date, rerun_ai=rerun_ai)
        return

    if "--backfill" in sys.argv:
        idx = sys.argv.index("--backfill")
        target = sys.argv[idx + 1]
        backfill_for_date(target, skip_ai=("--no-ai" in sys.argv))
        return

    run_time = datetime.now(timezone.utc)

    print(f"Video analysis started: {run_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("Fetching reels from the last 7 days with metrics...")

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    new_reels  = fetch_recent_reels(con)
    all_reels  = fetch_all_reels_for_patterns(con)
    bm         = fetch_benchmarks(con)
    con.close()

    print(f"Reels with metrics (last 7 days): {len(new_reels)}")
    print(f"Total reels for pattern analysis: {len(all_reels)}")

    # AI video analysis for each reel
    ai_analyses = {}
    for i, r in enumerate(new_reels):
        print(f"  Analyzing reel {i+1}/{len(new_reels)}: {r['id']}...")
        ai_analyses[r["id"]] = analyze_video_with_claude(r, bm)

    html = build_html(new_reels, bm, all_reels, run_time, ai_analyses)

    # save HTML
    today    = run_time.strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"video-analysis-{today}.html"
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved: {out_path}")

    # save JSON sidecar for index page
    scored_new = [(r, score_reel(r, bm)) for r in new_reels]
    scored_new.sort(key=lambda x: x[1], reverse=True)
    sidecar = {
        "date": today,
        "reels": [
            {
                "id":        r["id"],
                "permalink": r.get("permalink", ""),
                "caption":   (r.get("caption") or "")[:120],
                "score":     sc,
                "tier":      tier(sc)[0],
                "views":     r.get("views"),
                "reach":     r.get("reach"),
                "timestamp": r.get("timestamp", "")[:10],
                "good":      ai_analyses.get(r["id"], {}).get("good", []),
                "bad":       ai_analyses.get(r["id"], {}).get("bad", []),
                "top_tip":   ai_analyses.get(r["id"], {}).get("top_tip", ""),
            }
            for r, sc in scored_new
        ],
    }
    json_path = OUTPUT_DIR / f"video-analysis-{today}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)
    print(f"Sidecar saved: {json_path}")

    # regenerate index.html so new report appears on the dashboard
    idx_script = Path(__file__).parent / "generate-index.js"
    subprocess.run(["node", str(idx_script)], check=False)

    # send email
    count_label = f"{len(new_reels)}" if new_reels else "No"
    subject = f"PeaceGrappler — Video Analysis {today} ({count_label} reel{'s' if len(new_reels)!=1 else ''}, last 7 days)"
    send_email(subject, html, RECIPIENTS)
    print(f"Email sent to: {', '.join(RECIPIENTS)}")

if __name__ == "__main__":
    main()
