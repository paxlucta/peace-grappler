"""external_videos.py — List + download videos from YouTube / TikTok /
Instagram using yt-dlp.

The /analyze "Import videos" feature reads channel/profile URLs from the
active brand profile's Social Channels block, queries each platform for
available videos, filters out the ones that have already been imported
into this profile's DB, and presents a thumbnail grid the user picks from.

IG note: yt-dlp can fetch public IG posts, but listing a profile feed
usually requires cookies/auth. Errors there are surfaced cleanly so the
user knows that platform needs extra setup.
"""

import re
from pathlib import Path

# yt-dlp ships drivers for these browsers; anything else is treated as a
# cookies.txt file path.
_BROWSERS = {"chrome", "chromium", "brave", "edge", "firefox", "safari",
             "vivaldi", "opera", "whale"}


def _cookies_opt(cookies_hint):
    """Translate a user-provided cookies setting into a partial yt-dlp opts
    dict. Empty hint → empty dict (no cookies)."""
    s = (cookies_hint or "").strip()
    if not s:
        return {}
    low = s.lower()
    if low in _BROWSERS:
        return {"cookiesfrombrowser": (low,)}
    # Treat anything else as a path to a cookies.txt file.
    p = Path(s).expanduser()
    if p.exists() and p.is_file():
        return {"cookiefile": str(p)}
    # Last resort: pass the raw value to cookiefile so yt-dlp can complain.
    return {"cookiefile": str(p)}


def _platform_cookies_hint(platform):
    """Look up the cookies field for a platform from the active brand profile."""
    try:
        import app_config
        slot = (app_config.get_config().get("socials") or {}).get(platform) or {}
        return slot.get("cookies", "")
    except Exception:
        return ""


# Per-platform yt-dlp options for listing channels/profiles. We use
# extract_flat='in_playlist' so we get a quick list of entries (id, title,
# thumbnail, duration) without touching every video's full page.
_LIST_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
    "playlistend": 50,   # cap so we don't fetch hundreds for a busy channel
}


# Per-platform download options.
def _download_opts(dest_dir):
    return {
        "quiet": True,
        "no_warnings": True,
        # Use the video's actual title as the filename, with the platform
        # ID appended in brackets to guarantee uniqueness even if two
        # videos share the same title. UTF-8 (Russian, Japanese, …) is
        # preserved; OS-unsafe characters (slashes, colons) are stripped
        # by yt-dlp's default sanitizer.
        "outtmpl": str(Path(dest_dir) / "%(title)s [%(id)s].%(ext)s"),
        # Cap title to 180 chars so the filename stays under macOS HFS+'s
        # 255-byte limit even after the suffix is appended.
        "trim_file_name": 180,
        # Format selector tuned for YouTube's late-2025 SABR rollout:
        # DASH (bestvideo+bestaudio) streams now ship with empty URLs on the
        # web/web_safari clients and 403 if forced. Progressive formats with
        # both audio+video in one file (most notably YT format 18, 360p mp4)
        # still download cleanly with cookies. Prefer those, fall back to
        # any HLS/m3u8 stream, then anything yt-dlp picks last.
        "format": "b[ext=mp4][protocol^=http][acodec!=none][vcodec!=none]/b[ext=mp4][acodec!=none][vcodec!=none]/b",
        "merge_output_format": "mp4",
        "noprogress": True,
        "writeinfojson": False,
        "writethumbnail": False,
    }


# ── Platform URL resolution ─────────────────────────────────────────────────

def _normalize_handle_to_url(platform, handle, raw_url):
    """Pick the URL we'll feed to yt-dlp for *platform*.

    Prefers an explicit raw_url; falls back to building one from a handle
    (e.g. '@bakeddaily' on YouTube → https://youtube.com/@bakeddaily).
    Returns None if we have nothing usable.
    """
    raw_url = (raw_url or "").strip()
    handle = (handle or "").strip().lstrip("@")
    if raw_url:
        return raw_url
    if not handle:
        return None
    if platform == "youtube":
        return f"https://www.youtube.com/@{handle}/videos"
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{handle}"
    if platform == "instagram":
        return f"https://www.instagram.com/{handle}/"
    return None


def _fix_youtube_channel_url(url):
    """yt-dlp lists best when pointed at /videos for a channel."""
    if "youtube.com" in url and "/videos" not in url and "/playlist" not in url:
        # Common shapes: /@handle, /channel/UCxxx, /user/x, /c/x
        url = url.rstrip("/")
        if "/@" in url or "/channel/" in url or "/user/" in url or "/c/" in url:
            return url + "/videos"
    return url


# ── Listing ─────────────────────────────────────────────────────────────────

def list_channel_videos(platform, handle, raw_url, limit=50):
    """Return list of metadata dicts for a platform's channel/profile.

    Each entry: {platform, id, title, thumbnail, duration, page_url, uploader}

    Raises ExternalListError on failure with a user-readable message.
    """
    url = _normalize_handle_to_url(platform, handle, raw_url)
    if not url:
        raise ExternalListError(
            f"No handle or URL configured for {platform}."
        )
    if platform == "youtube":
        url = _fix_youtube_channel_url(url)

    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, ExtractorError
    except ImportError as e:
        raise ExternalListError(f"yt-dlp not installed: {e}")

    opts = dict(_LIST_OPTS)
    opts["playlistend"] = max(1, min(limit, 200))
    opts.update(_cookies_opt(_platform_cookies_hint(platform)))

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as e:
        raw = _strip_ansi(str(e))
        raise ExternalListError(_friendly_error(platform, raw), detail=raw)
    except Exception as e:
        raw = _strip_ansi(str(e))
        raise ExternalListError(f"{platform}: {e}", detail=raw)

    entries = (info or {}).get("entries") or []
    out = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        ext_id = ent.get("id") or ent.get("url")
        if not ext_id:
            continue
        thumb = _pick_thumbnail(ent)
        out.append({
            "platform":  platform,
            "id":        str(ext_id),
            "title":     ent.get("title") or "(untitled)",
            "thumbnail": thumb,
            "duration":  ent.get("duration") or 0,
            "page_url":  ent.get("url") or ent.get("webpage_url") or "",
            "uploader":  ent.get("uploader") or info.get("uploader") or "",
        })
    return out


def _pick_thumbnail(entry):
    """yt-dlp returns either `thumbnail` or a `thumbnails` array. Pick the
    largest available URL."""
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    if not thumbs:
        return ""
    # Prefer mid-size: largest height ≤ 720, else first
    sized = [t for t in thumbs if t.get("url")]
    sized.sort(key=lambda t: t.get("height") or 0)
    for t in sized:
        if (t.get("height") or 0) >= 320:
            return t["url"]
    return sized[-1]["url"] if sized else ""


# ── Downloading ─────────────────────────────────────────────────────────────

def download_video(platform, page_url_or_id, dest_dir, on_log=None):
    """Download a single video. Returns the local file Path.

    *page_url_or_id*: typically the page URL from list_channel_videos
    (each platform's extractor knows what to do with it). For YouTube/TikTok
    a bare video ID also works.

    YouTube fallback strategy: YouTube has been blocking yt-dlp's default
    `web` player client with HTTP 403. We try a chain of alternative player
    clients before giving up; cookies (from the user's Settings) are also
    applied to every attempt.
    """
    log = on_log or (lambda m: None)
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as e:
        raise ExternalDownloadError(f"yt-dlp not installed: {e}")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # If we just got a bare ID and platform info, build a canonical URL.
    url = _ensure_video_url(platform, page_url_or_id)

    base_opts = _download_opts(dest)
    base_opts.update(_cookies_opt(_platform_cookies_hint(platform)))

    def hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            if pct:
                log(f"Downloading {pct}")
        elif d.get("status") == "finished":
            log("Merging streams...")

    base_opts["progress_hooks"] = [hook]

    # Build the list of attempts. For YouTube we cycle through alternative
    # player clients that often dodge 403s. Other platforms get a single
    # default attempt.
    attempts = []
    if platform == "youtube":
        # As of yt-dlp 2025.10, YouTube's anti-bot has neutered most player
        # clients we used to fall back through:
        #   - `ios` and `android` now require a GVS PO Token (all formats skipped).
        #   - `tv_embedded` returns "no longer supported in this device".
        #   - `web` / `web_safari` are forced into SABR streaming, so DASH
        #     formats arrive without URLs.
        # yt-dlp's own default chain (tv, web_safari, web) plus our progressive
        # format selector still reliably grabs format 18 (360p mp4 with
        # baked-in audio), which is fine for analysis + clip building. We
        # used to retry with explicit player_clients (ios/tv_embedded/etc),
        # but every one of them now fails worse than the default ("page needs
        # to be reloaded", "GVS PO Token required") and overwrites the more
        # actionable error from the first attempt. So: one attempt only.
        attempts.append(base_opts)
    else:
        attempts.append(base_opts)

    cookies_set = bool(_platform_cookies_hint(platform))
    last_err = None
    last_raw = None     # full unredacted yt-dlp error (for triage in log footer)
    info = None
    fname = None
    for i, opts in enumerate(attempts):
        client_label = ""
        if platform == "youtube":
            try:
                client_label = " (player_client=" + \
                    ",".join(opts["extractor_args"]["youtube"]["player_client"]) + ")"
            except Exception:
                pass
        if i > 0:
            log(f"Retrying{client_label}...")
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fname = ydl.prepare_filename(info)
            last_err = None
            break
        except DownloadError as e:
            last_raw = _strip_ansi(str(e))
            # Tag each attempt with which player_client failed so the
            # triage log in the footer can correlate.
            if client_label:
                last_raw = f"[attempt {i+1}{client_label}]\n{last_raw}"
            last_err = _friendly_error(platform, last_raw)
            if "auth" in last_err.lower() or "cookies" in last_err.lower():
                break
        except Exception as e:
            last_raw = _strip_ansi(str(e))
            last_err = f"{platform}: {e}"
    if last_err:
        # YouTube 403 — give cookie-state-aware guidance so the user knows
        # exactly what to change (instead of generic "blocked"). yt-dlp
        # surfaces three different shapes for the same underlying anti-bot:
        #   - "HTTP Error 403" on direct progressive download
        #   - "fragment ... 403" + "downloaded file is empty" on HLS (every
        #     fragment 403s, then yt-dlp gives up with an empty-file error)
        #   - generic "auth" / "cookies" wording from our friendly_error
        # Match all three so the user always lands on the actionable message.
        low_err = last_err.lower()
        low_raw = (last_raw or "").lower()
        is_403 = (
            "403" in last_err
            or "auth" in low_err
            or "downloaded file is empty" in low_err
            or "403" in low_raw
            or "fragment" in low_raw and "empty" in low_raw
        )
        if platform == "youtube" and is_403:
            cookies_hint = (_platform_cookies_hint(platform) or "").strip()
            raise ExternalDownloadError(
                _youtube_403_message(cookies_hint, url),
                detail=last_raw,
            )
        raise ExternalDownloadError(last_err, detail=last_raw)
    if not fname:
        raise ExternalDownloadError("Download produced no file.",
                                    detail=last_raw)

    # After merge, the file may have a different extension than the template.
    final = Path(fname)
    if not final.exists():
        # Search for it by stem under dest dir.
        stem = final.stem
        for cand in dest.glob(stem + ".*"):
            if cand.is_file():
                final = cand
                break
    if not final.exists():
        raise ExternalDownloadError("Download finished but file not found.")
    return final


def _ensure_video_url(platform, page_url_or_id):
    s = (page_url_or_id or "").strip()
    if not s:
        raise ExternalDownloadError("No video URL or id supplied.")
    if s.startswith(("http://", "https://")):
        return s
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={s}"
    if platform == "tiktok":
        # TikTok needs the username too; bare ids rarely work — surface a
        # clearer error in that case.
        raise ExternalDownloadError(
            "TikTok requires the full video URL (need /@user/video/<id>)."
        )
    if platform == "instagram":
        return f"https://www.instagram.com/p/{s}/"
    return s


# ── Errors + helpers ────────────────────────────────────────────────────────

class _DetailedError(Exception):
    """Carries both a user-readable summary and the raw triage detail.
    str(err) returns the friendly summary; err.detail is the unedited
    yt-dlp output (multi-line, with HTTP codes / player_client info)
    that the global log footer can surface for debugging."""
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


class ExternalListError(_DetailedError):
    pass


class ExternalDownloadError(_DetailedError):
    pass


_AUTH_RE = re.compile(
    r"login required|please log in|http error 401|http error 403|"
    r"rate-limit|sign in",
    re.IGNORECASE,
)
_SAFARI_BLOCKED_RE = re.compile(
    r"Operation not permitted.*Cookies\.binarycookies", re.IGNORECASE,
)
_BROWSER_LOCKED_RE = re.compile(
    r"could not (find|load|read|extract).*cookie|cookies database is locked",
    re.IGNORECASE,
)


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s or "")


def _youtube_403_message(cookies_hint, url):
    """Produce a cookie-state-aware error message for YouTube 403s. The
    user gets a different actionable hint depending on what they currently
    have configured."""
    low = (cookies_hint or "").lower()
    base = "YouTube blocked this download (HTTP 403)."
    if not cookies_hint:
        body = (
            "No cookies configured. In /settings → YouTube, set the "
            "Cookies field to a logged-in browser name: `chrome`, "
            "`firefox`, `brave`, or `edge`. yt-dlp will pull cookies "
            "directly from there — no manual export needed."
        )
    elif low == "safari":
        body = (
            "Safari cookies are blocked by macOS sandbox. Switch the "
            "Cookies field in /settings → YouTube to `chrome`, `firefox`, "
            "or `brave` instead — those store cookies in a location "
            "yt-dlp can read."
        )
    elif low in _BROWSERS:
        body = (
            f"Cookies from `{cookies_hint}` got us a session, but YouTube's "
            f"late-2025 anti-bot (SABR + GVS PO Token requirement) is "
            f"rejecting the actual video fetch. This affects most public "
            f"videos right now — cookies alone are no longer enough. "
            f"Workarounds: "
            f"(1) install the bgutil PO Token provider plugin "
            f"(https://github.com/Brainicism/bgutil-ytdlp-pot-provider) — "
            f"this is the cleanest fix; "
            f"(2) download the video manually in your browser (use the "
            f"\"Open in browser ↗\" link below) and drop the file into the "
            f"source folder; "
            f"(3) confirm you're signed in to {cookies_hint} and the video "
            f"isn't age-restricted, members-only, or region-blocked."
        )
    else:
        body = (
            f"Cookies file `{cookies_hint}` didn't unlock the download. "
            f"The cookies may be stale (export a fresh `cookies.txt` from "
            f"a logged-in browser session) or the video itself is "
            f"restricted (age-gated, members-only, region-locked)."
        )
    if url:
        body += f"  Source URL: {url}"
    return f"{base} {body}"


def _friendly_error(platform, raw):
    """Trim yt-dlp's verbose error output and surface auth issues clearly."""
    raw = _strip_ansi(raw)
    msg = raw.strip().splitlines()[-1] if raw else ""
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = msg[:280]

    # macOS Safari sandbox — extra-clear message + actionable workarounds.
    if _SAFARI_BLOCKED_RE.search(raw) or _SAFARI_BLOCKED_RE.search(msg):
        return (
            f"{platform}: macOS won't let this app read Safari's cookies "
            f"(sandbox restriction). Workarounds, in order of effort: "
            f"(1) put `chrome`, `firefox`, or `brave` in the Cookies field "
            f"instead; (2) export a cookies.txt with the 'Get cookies.txt "
            f"LOCALLY' extension and paste its full path; (3) grant Terminal "
            f"Full Disk Access in System Settings → Privacy & Security."
        )

    # Other browser cookie-store problems — usually means the browser is
    # running and holding a SQLite lock.
    if _BROWSER_LOCKED_RE.search(msg):
        return (
            f"{platform}: couldn't read cookies — the browser may be open. "
            f"Quit it and retry, or switch to a different browser / "
            f"cookies.txt path. ({msg})"
        )

    if _AUTH_RE.search(msg) or "cookies" in msg.lower():
        return (f"{platform} requires authentication for this operation. "
                f"Public scraping is rate-limited or blocked. "
                f"({msg})")
    return f"{platform}: {msg}" if msg else f"{platform} request failed."
