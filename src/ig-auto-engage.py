#!/usr/bin/env python3
"""
Auto-Engage for @peacegrappler
- Comments via Meta Graph API (contextual keyword matching)
- Likes + Reposts via AppleScript/Chrome browser automation (API lacks permission)
- Email report via Gmail SMTP
Runs every 30 minutes via cron; only processes posts not yet engaged.
"""

import json
import os
import random
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─── paths ────────────────────────────────────────────────────────────────────

ROOT_DIR   = Path(__file__).parent.parent
ENV_FILE   = ROOT_DIR / ".env"
STATE_FILE = ROOT_DIR / "data" / "ig-auto-engage-state.json"
LOG_FILE   = ROOT_DIR / "logs" / "ig-auto-engage.log"
LOCK_FILE  = ROOT_DIR / "data" / "ig-auto-engage.lock"
# ─── config ───────────────────────────────────────────────────────────────────

API_VERSION = "v25.0"
ACCOUNT_ID  = "17841447891636367"   # @peacegrappler

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

SMTP_USER  = "paxlucta@gmail.com"
SMTP_PASS  = os.environ["GMAIL_PASSWORD_TOKEN"]
RECIPIENTS = ["abghandour@gmail.com", "marcello1spinelli@gmail.com"]

MAX_POSTS_PER_RUN = 5   # cap per run to avoid rate limits

# ─── comment bank ─────────────────────────────────────────────────────────────

COMMENTS = {
    "fight": [
        "This is exactly what I love about MMA – the heart these fighters bring every time out 🔥",
        "MMA truly is the most intense sport on the planet 💪",
        "Respect to both warriors leaving it all in there 👊",
    ],
    "luta": [
        "Luta pesada! Quanto orgulho! 🔥🔥",
        "Que luta incrível! 💪🇧🇷",
        "Brasil mostrando como se faz! 🇧🇷🔥",
    ],
    "ufc": [
        "UFC delivering the best action once again! 🔥",
        "The UFC never disappoints! 👊",
        "The UFC always delivers the best matchups 💯🔥",
    ],
    "mma": [
        "MMA is in a league of its own right now 💯",
        "This is why MMA is the greatest sport 🙏",
    ],
    "win": [
        "Hard work pays off! So happy for this win 🎉",
        "This is what all the sacrifice was for! Congratulations 🎉🔥",
        "Victory tastes so much sweeter after all that training 💪",
    ],
    "victory": [
        "What a moment – this is why champions put in the work 🏆",
        "Celebrate it, you earned every bit of this 🎉",
    ],
    "ganhou": [
        "Parabéns demais! Todo o trabalho valeu a pena! 🇧🇷🔥",
        "Merecido! Parabéns 🎉💪",
    ],
    "training": [
        "This is what champions are made of 💪🔥",
        "Hard work, no excuses – love this mindset 💯",
        "Training this hard shows in the cage every single time 👊",
        "Every champion was once a contender that refused to give up 💪🔥",
    ],
    "treino": [
        "Treino pesado, resultado pesado! 💪🔥",
        "Assim se constrói um campeão! 🇧🇷👊",
    ],
    "knockout": [
        "💥 KNOCKOUT! That is brutal 💯💯💯",
        "And just like that – it's over! 💥🔥",
        "What a way to end it! 💥👊",
    ],
    "nocaute": [
        "NOCAUTE! Isso é surreal! 💥🔥🔥",
        "Que finalização! Nocauteou bonito! 💥💥",
    ],
    "ko": [
        "KO! Flatlined 💥💥",
        "Technical and devastating 💯💥",
    ],
    "submission": [
        "The grappling mastery is real! 🐍🔥",
        "And just like that – it's over! Tap out 💯",
        "The craft speaks for itself 💪🐍",
    ],
    "title": [
        "A true champion's performance 🏆💪",
        "This is what dominance looks like 🏆🔥",
    ],
    "champion": [
        "Champions are built, not born 💪🏆",
        "This is why they call it championship lineage 🏆🔥",
    ],
    "debut": [
        "Welcome to the big stage! Big things coming 🙏🔥",
        "The journey of a thousand miles starts with one step 👊",
    ],
    "comeback": [
        "Never count them out – this is why we watch 💪🔥",
        "The heart to keep going when things get tough is what defines a fighter 💯👊",
    ],
    "grappling": [
        "The ground game was next level today 💯🐍",
        "Jiu-Jitsu really is the gentle art – brutal and precise 🐍🔥",
    ],
    "jiu": [
        "The gentle art showing up once again 🐍💪",
        "Technique beats size every single time – love this 🐍🔥",
    ],
    "boxing": [
        "Hands of stone right there 💪🥊",
        "The sweet science at its finest 🥊🔥",
    ],
    "muay thai": [
        "Thai boxing coming in hot 🔥👊💯",
        "Elbows and knees – the eight limbs of destruction 🥊🔥",
    ],
    "camp": [
        "All those hours in camp lead to moments like this 💯👊",
        "Camp is where champions are forged 💪🔥",
    ],
    "coach": [
        "Coaches make champions – glad to see this work paying off 💪🙏",
        "The best fighters know they can't do it alone 💯🙏",
    ],
    "team": [
        "It takes a village – love seeing the whole team show up 💪🔥",
        "Teamwork making the dream work 💯👊",
    ],
    "sparring": [
        "Sparring partners are unsung heroes – respect to all of them 👊💪",
        "These sessions shape the fighters we watch on fight night 💯🔥",
    ],
    "motivation": [
        "This is why we do it 💪🔥",
        "Raw motivation right here – can't fake this energy 💯👊",
    ],
    "goat": [
        "GOAT behavior 💯🔥",
        "All time greatness talking 🐐💪",
    ],
}

FALLBACK_COMMENTS = [
    "Straight fire 🔥🔥🔥",
    "Love this content 💯👊",
    "This is why we follow 💪🔥",
    "The MMA community is different 🔥",
    "Keep bringing it 👊💪",
    "Incredible 🔥💯",
    "This hits different 💪🔥",
    "More of this please 🙏👊",
    "Can't get enough of this 💯🔥",
]

# ─── helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    # Cron already redirects stderr to ig-auto-engage.log via "2>&1", so
    # writing to BOTH stderr and the file produced duplicate lines for every
    # entry. Print to stderr only and let cron capture it.
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)

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
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"engaged_posts": []}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ─── API ──────────────────────────────────────────────────────────────────────

def api_get(endpoint, token, params=None):
    p = {"access_token": token, **(params or {})}
    url = f"https://graph.facebook.com/{API_VERSION}{endpoint}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())

def api_post(endpoint, token, data):
    payload = urllib.parse.urlencode({"access_token": token, **data}).encode()
    url = f"https://graph.facebook.com/{API_VERSION}{endpoint}"
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def fetch_recent_posts(token):
    data = api_get(
        f"/{ACCOUNT_ID}/media",
        token,
        {"fields": "id,caption,permalink,media_type,timestamp", "limit": "10"},
    )
    return data.get("data", [])

def fetch_post_comments(post_id, token):
    """Fetch up to 10 existing comments to gauge the vibe before posting."""
    try:
        data = api_get(
            f"/{post_id}/comments",
            token,
            {"fields": "text", "limit": "10"},
        )
        return [c.get("text", "") for c in data.get("data", [])]
    except Exception:
        return []

def post_comment(post_id, comment_text, token):
    resp = api_post(f"/{post_id}/comments", token, {"message": comment_text})
    return "id" in resp, resp

# ─── comment generation ───────────────────────────────────────────────────────

# Portuguese signal words used for language detection
PT_SIGNALS = {
    "luta", "nocaute", "treino", "ganhou", "campeão", "vitória", "parabéns",
    "incrível", "demais", "valeu", "muito", "que", "mais", "para", "com",
    "não", "uma", "ele", "ela", "seu", "sua", "por", "foi", "está", "esse",
    "essa", "sobre", "depois", "antes", "cada",
}

# Keywords that have both EN and PT variants in the bank
PT_KEYWORDS = {"luta", "nocaute", "treino", "ganhou", "jiu", "muay thai"}

def _is_portuguese(text):
    words = set(text.lower().split())
    return len(words & PT_SIGNALS) >= 2

def _collect_options(text):
    """Return a weighted pool of comment options matched against text."""
    pool = []
    for keyword, options in COMMENTS.items():
        if keyword in text:
            # Longer keyword = more specific match = more weight
            weight = max(1, len(keyword) // 3)
            pool.extend(random.choice(options) for _ in range(weight))
    return pool

def generate_comment(caption, existing_comments=None):
    """
    Pick a positive/neutral comment informed by both the post caption and
    any existing comments, so the reply fits the vibe of the conversation.

    - Combines caption + existing comments into a single context string.
    - Weighs keyword matches by specificity (longer keyword = more weight).
    - Prefers language-matched options (PT vs EN) based on detected language.
    - Always returns something positive or neutral; never negative.
    """
    existing_comments = existing_comments or []

    caption_text  = (caption or "").lower()
    comments_text = " ".join(existing_comments).lower()
    combined      = caption_text + " " + comments_text

    is_pt = _is_portuguese(combined)

    # Build pool from combined context (caption + existing comments)
    pool = _collect_options(combined)

    # Boost language-matching options by adding them again
    lang_keywords = PT_KEYWORDS if is_pt else (set(COMMENTS.keys()) - PT_KEYWORDS)
    for keyword in lang_keywords:
        if keyword in combined:
            pool.extend(random.choice(COMMENTS[keyword]) for _ in range(2))

    if pool:
        return random.choice(pool)

    # Language-aware fallbacks — always positive/neutral
    pt_fallbacks = [
        "Que conteúdo incrível! 🔥💪",
        "Sempre de alto nível! 💯🇧🇷",
        "Isso é demais! 🔥🙏",
        "Conteúdo de primeira! 💪🔥",
        "Direto ao ponto, como sempre! 💯",
    ]
    return random.choice(pt_fallbacks if is_pt else FALLBACK_COMMENTS)

# ─── browser like + repost ────────────────────────────────────────────────────
# Strategy: navigate an existing logged-in Instagram tab to the post, wait for
# React to hydrate (Like button only renders in active/foreground tabs), then in
# a single JS pass:
#   1. Click the Like button (React .click() works here)
#   2. POST to /graphql/query with usePolarisCreateMediaRepostMutation
#      - fb_dtsg + lsd tokens extracted from the page's inline scripts
#      - internal media ID derived from the URL shortcode (base64 decode)
#      - actor_id from ds_user_id cookie
# Then restore the tab URL.

_ENGAGE_APPLESCRIPT = '''\
tell application "Google Chrome"
    set igWinIdx to 0
    set igTabIdx to 0
    set winCount to count of windows
    repeat with wi from 1 to winCount
        set tabCount to count of tabs of window wi
        repeat with ti from 1 to tabCount
            if URL of tab ti of window wi contains "instagram.com" then
                set igWinIdx to wi
                set igTabIdx to ti
                exit repeat
            end if
        end repeat
        if igWinIdx > 0 then exit repeat
    end repeat

    if igWinIdx is 0 then
        log "NO_IG_TAB"
        return
    end if

    set prevURL to URL of tab igTabIdx of window igWinIdx
    set URL of tab igTabIdx of window igWinIdx to "{url}"
    set active tab index of window igWinIdx to igTabIdx
    activate

    -- Wait for page to actually load instead of a fixed delay
    delay 3
    repeat 12 times
        set loadState to execute tab igTabIdx of window igWinIdx javascript "document.readyState"
        if loadState is "complete" then exit repeat
        delay 1
    end repeat
    delay 2

    -- Step 1: Click like + fire async repost (non-blocking)
    execute tab igTabIdx of window igWinIdx javascript "
        (function() {
            // --- Like: multi-strategy button detection ---
            var likeLabels   = ['Like', 'Curtir', 'Me gusta', 'Gostei'];
            var unlikeLabels = ['Unlike', 'Descurtir', 'Ya no me gusta', 'Liked'];
            var commentLabels = ['Comment', 'Comentar', 'Comentario'];
            var likeSvg = null;
            var likeStatus = 'LIKE_NOT_FOUND';
            var svgs = Array.from(document.querySelectorAll('svg[aria-label]'));

            // Strategy 1: Find Comment SVG, take the one before it
            for (var ci = 0; ci < commentLabels.length && !likeSvg; ci++) {
                var cSvg = svgs.find(function(s) {
                    return s.getAttribute('aria-label') === commentLabels[ci];
                });
                if (cSvg) {
                    var idx = svgs.indexOf(cSvg);
                    if (idx > 0) likeSvg = svgs[idx - 1];
                }
            }

            // Strategy 2: Direct search for Like/Unlike SVGs
            if (!likeSvg) {
                likeSvg = svgs.find(function(s) {
                    var lbl = s.getAttribute('aria-label') || '';
                    for (var i = 0; i < likeLabels.length; i++) {
                        if (lbl === likeLabels[i]) return true;
                    }
                    for (var i = 0; i < unlikeLabels.length; i++) {
                        if (lbl === unlikeLabels[i]) return true;
                    }
                    return false;
                });
            }

            // Strategy 3: Heart icon by SVG path shape (IG heart outline)
            if (!likeSvg) {
                var allSvgs = document.querySelectorAll('svg');
                for (var si = 0; si < allSvgs.length; si++) {
                    var paths = allSvgs[si].querySelectorAll('path');
                    for (var pi = 0; pi < paths.length; pi++) {
                        var d = paths[pi].getAttribute('d') || '';
                        if (d.length > 50 && d.length < 500 && d.indexOf('M34.6') >= 0 && d.indexOf('C') >= 0) {
                            likeSvg = allSvgs[si];
                            break;
                        }
                    }
                    if (likeSvg) break;
                }
            }

            // Strategy 4: Section-based - find the action bar with role=button elements
            if (!likeSvg) {
                var sections = document.querySelectorAll('section');
                for (var si = 0; si < sections.length; si++) {
                    var btns = sections[si].querySelectorAll('[role=button]');
                    if (btns.length >= 3 && btns.length <= 5) {
                        var firstSvg = btns[0].querySelector('svg');
                        if (firstSvg) { likeSvg = firstSvg; break; }
                    }
                }
            }

            if (!likeSvg) {
                likeStatus = svgs.length === 0 ? 'NOT_LOADED' : 'NO_BTN:' + svgs.length + 'svgs';
            } else {
                var lbl = likeSvg.getAttribute('aria-label') || '';
                var isUnliked = false;
                for (var i = 0; i < unlikeLabels.length; i++) {
                    if (lbl === unlikeLabels[i]) { isUnliked = true; break; }
                }
                if (isUnliked) {
                    likeStatus = 'ALREADY_LIKED';
                } else {
                    var btn = likeSvg.closest('[role=button]') || likeSvg.parentElement;
                    if (btn) { btn.click(); likeStatus = 'CLICKED'; }
                    else { likeStatus = 'NO_PARENT'; }
                }
            }

            // --- Repost via async fetch (no blocking) ---
            window.__repostResult = 'PENDING';

            function shortcodeToId(code) {
                var alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
                var n = BigInt(0);
                for (var c of code) { n = n * BigInt(64) + BigInt(alpha.indexOf(c)); }
                return n.toString();
            }
            var parts = location.pathname.split('/').filter(Boolean);
            var internalMediaId = shortcodeToId(parts[parts.length - 1]);

            function extractToken(haystack, key) {
                var idx = haystack.indexOf(key);
                if (idx < 0) return null;
                var chunk = haystack.slice(idx + key.length, idx + key.length + 600);
                var ti = chunk.indexOf('token');
                if (ti < 0) return null;
                chunk = chunk.slice(ti + 5);
                var ci = chunk.indexOf(':');
                if (ci < 0) return null;
                chunk = chunk.slice(ci + 1);
                var si = 0;
                while (si < chunk.length && chunk.charCodeAt(si) <= 32) si++;
                var qc = chunk.charCodeAt(si);
                if (qc !== 34 && qc !== 39) return null;
                var ei = chunk.indexOf(chunk[si], si + 1);
                if (ei < 0) return null;
                return chunk.slice(si + 1, ei);
            }
            var allText = '';
            var scripts = document.querySelectorAll('script');
            for (var i = 0; i < scripts.length; i++) allText += scripts[i].textContent;
            var fb_dtsg = extractToken(allText, 'DTSGInitData');
            var lsd     = extractToken(allText, 'LSD');

            if (!fb_dtsg || !lsd) {
                window.__repostResult = 'NO_TOKENS:dtsg=' + !!fb_dtsg + ',lsd=' + !!lsd;
            } else {
                var csrfMatch  = document.cookie.match(/csrftoken=([^;]+)/);
                var actorMatch = document.cookie.match(/ds_user_id=(\\\\d+)/);
                if (!csrfMatch || !actorMatch) {
                    window.__repostResult = 'NO_AUTH';
                } else {
                    var csrf     = csrfMatch[1];
                    var actor_id = actorMatch[1];
                    var variables = JSON.stringify({input: {
                        actor_id: actor_id, client_mutation_id: '1',
                        audience: 7, media_id: internalMediaId, note_style: 13, text: ''
                    }});
                    var body = [
                        'av=' + actor_id, '__d=www', '__user=0', '__a=1',
                        'fb_dtsg=' + encodeURIComponent(fb_dtsg),
                        'lsd=' + encodeURIComponent(lsd),
                        'fb_api_caller_class=RelayModern',
                        'fb_api_req_friendly_name=usePolarisCreateMediaRepostMutation',
                        'server_timestamps=true',
                        'variables=' + encodeURIComponent(variables),
                        'doc_id=24773708208946940'
                    ].join('&');

                    var ctrl = new AbortController();
                    var tid = setTimeout(function() { ctrl.abort(); }, 15000);
                    fetch('/graphql/query', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'x-csrftoken': csrf,
                            'x-ig-app-id': '936619743392459',
                            'x-fb-friendly-name': 'usePolarisCreateMediaRepostMutation',
                            'x-fb-lsd': lsd
                        },
                        body: body,
                        signal: ctrl.signal
                    }).then(function(r) {
                        clearTimeout(tid);
                        return r.text().then(function(t) {
                            window.__repostResult = 'S' + r.status + ':' + t.slice(0, 300);
                        });
                    }).catch(function(e) {
                        clearTimeout(tid);
                        window.__repostResult = 'FETCH_ERR:' + e.message;
                    });
                }
            }

            window.__likeResult = likeStatus;
            return 'LIKE_DONE';
        })()
    "

    -- Wait for async repost fetch to complete
    delay 8

    -- Step 2: Read the results
    set jsResult to execute tab igTabIdx of window igWinIdx javascript "
        'LIKE=' + (window.__likeResult || 'UNKNOWN') + '||REPOST=' + (window.__repostResult || 'TIMEOUT')
    "

    delay 1
    set URL of tab igTabIdx of window igWinIdx to prevURL
    log jsResult
end tell
'''


def engage_post_browser(permalink, media_id=None):
    """
    Navigate an existing logged-in Instagram tab to the post, like it, and
    repost it via the GraphQL usePolarisCreateMediaRepostMutation.
    Returns (liked: bool|None, reposted: bool|None).
    """
    try:
        script = _ENGAGE_APPLESCRIPT.replace("{url}", permalink)
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=75)
        output = (r.stdout + r.stderr).strip()

        if "NO_IG_TAB" in output:
            log("  Browser skipped — no Instagram tab open in Chrome")
            return None, None

        # --- Like ---
        liked = False
        if "LIKE=CLICKED" in output:
            liked = True
        elif "LIKE=ALREADY_LIKED" in output:
            log("  Like skipped — already liked")
            liked = True
        elif "LIKE=NOT_LOADED" in output:
            log("  Like failed — page not loaded")
        else:
            log(f"  Browser output: {output[:120]}")

        # --- Repost ---
        reposted = False
        rp = output.split("||REPOST=", 1)[1] if "||REPOST=" in output else ""
        if rp.startswith("S200:"):
            body = rp[5:]
            if '"xdt_create_media_note_v2"' in body and '"id"' in body:
                reposted = True
            elif '"xdt_create_media_note_v2":null' in body:
                log("  Repost skipped — already reposted (null response)")
                reposted = True
            else:
                log(f"  Repost 200 unexpected: {body[:120]}")
        elif rp and rp not in ("SKIP",):
            log(f"  Repost result: {rp[:120]}")

        return liked, reposted

    except Exception as e:
        log(f"  Browser engage exception: {e}")
        return False, False

# ─── email ────────────────────────────────────────────────────────────────────

def send_email_report(engagements):
    if not engagements:
        return

    rows = ""
    for i, e in enumerate(engagements):
        comment_html = e.get("comment", "—").replace("<", "&lt;")
        liked    = "✅" if e.get("liked")    is True else ("—" if e.get("liked")    is None else "❌")
        reposted = "✅" if e.get("reposted") is True else ("—" if e.get("reposted") is None else "❌")
        caption = (e.get("caption") or "")[:100].replace("<", "&lt;")
        permalink = e.get("permalink", "#")
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #2a2d3a;color:#8b8fa3;font-size:12px">{i+1}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #2a2d3a;font-size:13px;max-width:260px">
            <a href="{permalink}" style="color:#818cf8;text-decoration:none">{caption}{'…' if len(e.get('caption') or '') > 100 else ''}</a>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #2a2d3a;font-size:13px">{comment_html}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #2a2d3a;text-align:center">{liked}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #2a2d3a;text-align:center">{reposted}</td>
        </tr>"""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#0f1117;color:#e1e4ed;padding:24px;margin:0">
<div style="max-width:800px;margin:0 auto">
  <h2 style="color:#818cf8;margin:0 0 4px">Auto-Engage Report</h2>
  <p style="color:#8b8fa3;font-size:13px;margin:0 0 24px">{now} &mdash; {len(engagements)} post{"s" if len(engagements)!=1 else ""} engaged</p>
  <table style="width:100%;border-collapse:collapse">
    <thead>
      <tr style="background:#1a1d27">
        <th style="padding:10px 12px;text-align:left;color:#8b8fa3;font-size:12px;border-bottom:2px solid #2a2d3a">#</th>
        <th style="padding:10px 12px;text-align:left;color:#8b8fa3;font-size:12px;border-bottom:2px solid #2a2d3a">Post</th>
        <th style="padding:10px 12px;text-align:left;color:#8b8fa3;font-size:12px;border-bottom:2px solid #2a2d3a">Comment Posted</th>
        <th style="padding:10px 12px;text-align:left;color:#8b8fa3;font-size:12px;border-bottom:2px solid #2a2d3a">Liked</th>
        <th style="padding:10px 12px;text-align:left;color:#8b8fa3;font-size:12px;border-bottom:2px solid #2a2d3a">Reposted</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#8b8fa3;font-size:11px;margin-top:32px;border-top:1px solid #2a2d3a;padding-top:16px">
    PeaceGrappler Auto-Engage &mdash; Comments via Meta Graph API &middot; Likes &amp; Reposts via browser
  </p>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Auto-Engage — {len(engagements)} post{'s' if len(engagements)!=1 else ''} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText("See HTML version.", "plain"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, RECIPIENTS, msg.as_string())
    log("Email report sent.")

# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    # Prevent concurrent runs (two schedulers firing at the same time)
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
    except FileExistsError:
        # Another instance is already running — exit silently
        return
    try:
        _main()
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def _main():
    log("=== Auto-Engage started ===")

    env = load_env()
    token = env.get("TOKEN")
    if not token:
        log("ERROR: TOKEN not found in .env")
        sys.exit(1)

    state          = load_state()
    engaged_list   = list(state.get("engaged_posts", []))   # preserved order
    engaged        = set(engaged_list)                      # O(1) membership

    # Fetch recent posts
    try:
        posts = fetch_recent_posts(token)
    except Exception as e:
        log(f"ERROR fetching posts: {e}")
        sys.exit(1)

    log(f"Fetched {len(posts)} recent posts")

    new_posts = [p for p in posts if p["id"] not in engaged][:MAX_POSTS_PER_RUN]
    if not new_posts:
        log("No new posts to engage with — exiting")
        return

    log(f"{len(new_posts)} new post(s) to process")

    engagements = []
    for post in new_posts:
        post_id   = post["id"]
        caption   = post.get("caption", "")
        permalink = post.get("permalink", "")
        log(f"Processing {post_id}: {(caption or 'no caption')[:60]}")

        # Read existing comments first to get a feel of the vibe
        existing_comments = fetch_post_comments(post_id, token)
        if existing_comments:
            log(f"  Read {len(existing_comments)} existing comment(s) for context")

        # Comment via API
        comment_text = generate_comment(caption, existing_comments)
        log(f"  Comment: {comment_text}")
        ok, resp = post_comment(post_id, comment_text, token)
        if ok:
            log(f"  ✅ Comment posted (id={resp.get('id')})")
        else:
            log(f"  ❌ Comment failed: {resp}")

        # Like + Repost via browser — single tab visit for both actions
        liked, reposted = engage_post_browser(permalink, media_id=post_id)
        log(f"  {'✅' if liked    is True else ('—' if liked    is None else '❌')} Like")
        log(f"  {'✅' if reposted is True else ('—' if reposted is None else '❌')} Repost")

        # Only mark as fully engaged if the browser action didn't hard-fail.
        # liked=None  → no Chrome/IG tab open; comment already posted, don't re-comment.
        # liked=False → AppleScript/runtime error; retry browser action next run.
        browser_done = liked is not False

        engagements.append({
            "post_id":   post_id,
            "caption":   caption[:120],
            "comment":   comment_text,
            "liked":     liked,
            "reposted":  reposted,
            "permalink": permalink,
            "browser_done": browser_done,
        })

        if browser_done and post_id not in engaged:
            engaged.add(post_id)
            engaged_list.append(post_id)   # append in chronological order

        time.sleep(random.uniform(1.5, 3.0))   # natural pacing

    # Persist last 50 engaged post IDs in chronological order — newest at the
    # end, oldest dropped off the front. Previous code set-trim was random.
    save_state({"engaged_posts": engaged_list[-50:]})

    log(f"=== Done — {len(engagements)} post(s) engaged ===")

    send_email_report(engagements)

if __name__ == "__main__":
    main()
