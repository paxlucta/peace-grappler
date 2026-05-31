#!/usr/bin/env python3
"""
Retroactively like and repost all posts in the engaged_posts state.
Safe to re-run: already-liked/reposted posts are handled gracefully.
"""
import json, random, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

ROOT_DIR   = Path(__file__).parent.parent
ENV_FILE   = ROOT_DIR / ".env"
STATE_FILE = ROOT_DIR / "data" / "ig-auto-engage-state.json"
LOG_FILE   = ROOT_DIR / "logs" / "ig-auto-engage.log"
API_VERSION = "v25.0"

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
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

def api_get(endpoint, token, params=None):
    p = {"access_token": token, **(params or {})}
    url = f"https://graph.facebook.com/{API_VERSION}{endpoint}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())

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


def engage_post_browser(permalink):
    try:
        script = _ENGAGE_APPLESCRIPT.replace("{url}", permalink)
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=75)
        output = (r.stdout + r.stderr).strip()

        if "NO_IG_TAB" in output:
            log("  Browser skipped — no Instagram tab open in Chrome")
            return None, None

        liked = False
        if "LIKE=CLICKED" in output:        liked = True
        elif "LIKE=ALREADY_LIKED" in output: log("  Like skipped — already liked"); liked = True
        elif "LIKE=NOT_LOADED" in output:    log("  Like failed — page not loaded")
        else:                                log(f"  Browser output: {output[:120]}")

        reposted = False
        rp = output.split("||REPOST=", 1)[1] if "||REPOST=" in output else ""
        if rp.startswith("S200:"):
            body = rp[5:]
            if '"xdt_create_media_note_v2"' in body and '"id"' in body:
                reposted = True
            elif '"xdt_create_media_note_v2":null' in body:
                log("  Repost skipped — already reposted"); reposted = True
            else:
                log(f"  Repost 200 unexpected: {body[:120]}")
        elif rp and rp != "SKIP":
            log(f"  Repost result: {rp[:120]}")

        return liked, reposted

    except Exception as e:
        log(f"  Engage exception: {e}")
        return False, False


# ── main ──────────────────────────────────────────────────────────────────────

env = load_env()
token = env.get("TOKEN")
if not token:
    log("ERROR: TOKEN not found"); sys.exit(1)

with open(STATE_FILE) as f:
    post_ids = json.load(f).get("engaged_posts", [])

log(f"=== Retro like+repost: {len(post_ids)} posts ===")

liked_count = reposted_count = 0
for post_id in post_ids:
    try:
        data = api_get(f"/{post_id}", token, {"fields": "permalink"})
        permalink = data.get("permalink", "")
    except Exception as e:
        log(f"  {post_id}: API error: {e}"); continue

    if not permalink:
        log(f"  {post_id}: no permalink"); continue

    log(f"Processing {post_id}: {permalink}")

    liked, reposted = engage_post_browser(permalink)
    log(f"  {'✅' if liked    is True else ('—' if liked    is None else '❌')} Like")
    log(f"  {'✅' if reposted is True else ('—' if reposted is None else '❌')} Repost")
    if liked:    liked_count += 1
    if reposted: reposted_count += 1

    time.sleep(random.uniform(2, 4))

log(f"=== Done: {liked_count}/{len(post_ids)} liked, {reposted_count}/{len(post_ids)} reposted ===")
