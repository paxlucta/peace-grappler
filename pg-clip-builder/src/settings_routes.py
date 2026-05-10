"""settings_routes.py — /settings page for AI CLI routing.

Lets the user pick a different provider per task (analysis / wizard /
captions) and configure each provider's binary path + default model.
"""

from flask import Blueprint, jsonify, request

import ai_cli
import app_config

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings/api/app")
def api_app_get():
    cfg = app_config.get_config()
    cfg["profiles"] = app_config.list_profiles()
    cfg["active_profile"] = app_config.get_active_profile_name()
    return jsonify(cfg)


@settings_bp.route("/settings/api/app", methods=["POST"])
def api_app_set():
    """Persist EVERY setting on the page into the active profile.

    Accepts: profile_name, brand_name, content_domain, source_folder,
    output_folder, tag_schema, socials, ai (the dict expected by ai_cli with
    tasks + providers). The deprecated social_handle field is silently
    ignored — primary handle lives at socials.instagram.handle now.
    """
    data = request.get_json(force=True) or {}
    try:
        app_config.set_config(
            profile_name=data.get("profile_name"),
            brand_name=data.get("brand_name"),
            content_domain=data.get("content_domain"),
            source_folder=data.get("source_folder"),
            output_folder=data.get("output_folder"),
            tag_schema=data.get("tag_schema"),
            socials=data.get("socials"),
            analysis_mode=data.get("analysis_mode"),
            whisper_model=data.get("whisper_model"),
            whisper_language=data.get("whisper_language"),
            whisper_translate=data.get("whisper_translate"),
            ai=data.get("ai"),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    out = app_config.get_config()
    out["profiles"] = app_config.list_profiles()
    out["active_profile"] = app_config.get_active_profile_name()
    return jsonify({"ok": True, **out})


@settings_bp.route("/settings/api/app/load", methods=["POST"])
def api_app_load():
    data = request.get_json(force=True) or {}
    name = (data.get("profile") or "").strip()
    try:
        app_config.load_profile(name)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    out = app_config.get_config()
    out["profiles"] = app_config.list_profiles()
    out["active_profile"] = app_config.get_active_profile_name()
    return jsonify({"ok": True, **out})


@settings_bp.route("/settings/api/app/import", methods=["POST"])
def api_app_import():
    """Import a brand profile JSON: validate, copy into data/profiles/, set
    active. Body shape:
      {filename: "MyBrand.json", content: <profile dict>}
    Used by the Load Profile button on /settings — the client reads the
    user's chosen file with FileReader and posts it here.
    """
    import json as _json
    data = request.get_json(force=True) or {}
    raw_name = (data.get("filename") or "").strip()
    content = data.get("content")
    if not isinstance(content, dict):
        return jsonify({"ok": False, "error": "content must be a JSON object"}), 400
    # Pick a profile name: prefer the file's profile_name/brand_name field,
    # fall back to the filename minus .json.
    name = (content.get("profile_name") or content.get("brand_name") or "").strip()
    if not name and raw_name.lower().endswith(".json"):
        name = raw_name[:-5]
    if not name:
        name = raw_name or "imported"
    try:
        # Round-trip through validators by writing then loading.
        app_config._write_profile_raw(name, content)
        app_config.load_profile(name)
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    out = app_config.get_config()
    out["profiles"] = app_config.list_profiles()
    out["active_profile"] = app_config.get_active_profile_name()
    return jsonify({"ok": True, **out})


# ── Settings wizard: scrape URL → AI → form prefill ─────────────────────────

WIZARD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
WIZARD_FETCH_TIMEOUT = 15
WIZARD_MAX_HTML_BYTES = 1_500_000   # cap before extraction
WIZARD_MAX_PROMPT_CHARS = 28_000    # cap fed to the AI


def _wizard_fetch(url):
    """Fetch *url* and return decoded HTML text. Raises ValueError on
    network/HTTP problems with a user-friendly message."""
    import urllib.request
    import urllib.error

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": WIZARD_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=WIZARD_FETCH_TIMEOUT) as resp:
            raw = resp.read(WIZARD_MAX_HTML_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        raise ValueError(f"Could not reach {url}: {e.reason}")
    except Exception as e:
        raise ValueError(f"Fetch failed: {e}")
    if len(raw) > WIZARD_MAX_HTML_BYTES:
        raw = raw[:WIZARD_MAX_HTML_BYTES]
    # Best-effort decode: try UTF-8 first, fall back to latin-1.
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc, errors="replace")
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _wizard_extract_signal(html):
    """Strip out scripts/styles, keep <title>, <meta>, headings, anchor text,
    and visible paragraph text. Returns a string suitable for an LLM prompt."""
    import re
    if not html:
        return ""
    s = html
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<noscript\b[^>]*>.*?</noscript>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)

    keep_chunks = []
    # Meta tags carry og:title, og:description, twitter:..., often the best
    # signal for IG/YouTube pages.
    for m in re.finditer(r"(?is)<meta\b[^>]*>", s):
        keep_chunks.append(m.group(0))

    # Title.
    t = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", s)
    if t:
        keep_chunks.append(f"<title>{t.group(1).strip()}</title>")

    # Headings + paragraphs + list items + anchor text.
    for tag in ("h1", "h2", "h3", "p", "li", "a", "span", "section"):
        for m in re.finditer(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", s):
            inner = m.group(1)
            inner = re.sub(r"(?is)<[^>]+>", " ", inner)
            inner = re.sub(r"\s+", " ", inner).strip()
            if inner and len(inner) > 1:
                keep_chunks.append(f"<{tag}>{inner}</{tag}>")

    out = "\n".join(keep_chunks)
    if len(out) > WIZARD_MAX_PROMPT_CHARS:
        out = out[:WIZARD_MAX_PROMPT_CHARS]
    return out


WIZARD_PROMPT = """\
You are a brand analyst. Below is the cleaned content of a web page (could \
be an Instagram profile, YouTube channel, TikTok, a brand website, etc.).

Your job: extract the brand identity and propose a video-tagging schema \
that would help an AI tag scenes from this brand's videos.

Return ONLY a JSON object with this exact structure:
{{
  "brand_name":     "<channel/brand name as it appears on the page>",
  "social_handle":  "<primary @handle (often the IG handle), or empty if not visible>",
  "content_domain": "<plain-English description of the niche, 2-6 words. Examples: 'sourdough baking', 'urban skateboarding', 'wedding cinematography', 'MMA / combat sports'>",
  "socials": {{
    "instagram": {{"handle": "<@handle or empty>", "url": "<https://instagram.com/... or empty>"}},
    "tiktok":    {{"handle": "<@handle or empty>", "url": "<https://tiktok.com/@... or empty>"}},
    "youtube":   {{"handle": "<@handle or empty>", "url": "<https://youtube.com/@... or full channel URL, or empty>"}}
  }},
  "tag_schema": {{
    "activity": ["<20-50 tags SPECIFIC to this niche — what visual actions, techniques, objects, or events would the AI need to recognize in their videos?>"],
    "setting":  ["<10-20 location/environment tags relevant to this niche>"],
    "camera":   ["close-up", "medium-shot", "wide-shot", "overhead", "pov", "handheld", "steady", "tracking", "slow-pan"],
    "energy":   ["high-energy", "medium-energy", "low-energy"],
    "quality":  ["low-quality"]
  }}
}}

Rules:
- For "socials": fill in only platforms the page actually links to or
  references. Leave handle/url empty for platforms with no signal.
  Inspect og:url, canonical URLs, and any anchor hrefs that point at
  instagram.com / tiktok.com / youtube.com.
- Tag values must be lowercase, hyphenated (e.g. "knife-skills" not "Knife Skills").
- "activity" tags should be highly specific to the niche — generic tags like "talking" or "outro" are fine to include but the bulk should be domain-specific.
- "setting" tags should describe the physical locations/environments where this brand films.
- Keep "camera", "energy", "quality" exactly as shown above (these are universal).
- If you cannot determine a field, use an empty string for strings or a reasonable default for arrays — but try hard before giving up.
- Return ONLY the JSON object. No markdown fences, no commentary.

URL: {url}

Page content:
{content}
"""


@settings_bp.route("/settings/api/app/wizard", methods=["POST"])
def api_app_wizard():
    """Scrape a URL and ask the configured wizard AI to extract brand info
    + a tag schema. Returns the JSON the AI produced (NOT yet saved — the
    user reviews and clicks Save on /settings)."""
    import json as _json
    import re as _re
    import ai_cli

    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url required"}), 400

    try:
        html = _wizard_fetch(url)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    signal = _wizard_extract_signal(html)
    if not signal:
        return jsonify(
            {"ok": False, "error": "Page returned no extractable content."}
        ), 400

    prompt = WIZARD_PROMPT.format(url=url, content=signal)
    raw = ai_cli.call_ai(prompt, task="wizard", timeout=180)
    if not raw:
        return jsonify(
            {"ok": False, "error": "AI returned no response. Check that the "
                                   "wizard provider is installed and authed."}
        ), 502

    # Strip code fences if the model added them despite instructions.
    text = raw.strip()
    text = _re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = _re.sub(r"\s*```\s*$", "", text).strip()
    m = _re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        parsed = _json.loads(text)
    except Exception as e:
        return jsonify(
            {"ok": False, "error": f"Could not parse AI response as JSON: {e}",
             "raw": raw[:2000]}
        ), 502

    return jsonify({"ok": True, "extracted": parsed})


@settings_bp.route("/settings/api/restart", methods=["POST"])
def api_restart():
    """Re-exec the Python process so settings that are read at module load
    (source/output folders, tag schema) take effect.

    The HTTP response is sent first; the actual exec is scheduled on a
    background timer with a 500 ms delay so the client sees a 200 OK and
    can start polling /api/server-id to detect when the new server is up.
    """
    import os
    import sys
    import threading

    def _do_exec():
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:
            print(f"[restart] os.execv failed: {exc}")

    threading.Timer(0.5, _do_exec).start()
    return jsonify({"ok": True})


@settings_bp.route("/settings/api/app/delete", methods=["POST"])
def api_app_delete():
    data = request.get_json(force=True) or {}
    name = (data.get("profile") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "profile required"}), 400
    app_config.delete_profile(name)
    out = app_config.get_config()
    out["profiles"] = app_config.list_profiles()
    out["active_profile"] = app_config.get_active_profile_name()
    return jsonify({"ok": True, **out})


@settings_bp.route("/settings/api/ai")
def api_ai_get():
    return jsonify(ai_cli.get_config())


@settings_bp.route("/settings/api/ai", methods=["POST"])
def api_ai_set():
    data = request.get_json(force=True) or {}
    # Per-task provider routing.
    for task, provider in (data.get("tasks") or {}).items():
        try:
            ai_cli.set_task_provider(task, provider)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    # Per-provider settings (binary path, default model).
    for name, vals in (data.get("providers") or {}).items():
        try:
            ai_cli.set_provider_settings(
                name,
                bin_path=vals.get("bin"),
                model=vals.get("model"),
            )
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **ai_cli.get_config()})


SETTINGS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Settings — ClipBuilder</title>
<style>
  body{font-family:system-ui,-apple-system,sans-serif;background:#08080c;color:#eeeef2;margin:0}
  /* Shared chrome — keep these rules in sync with the other pages so the
     header is the same height everywhere. */
  header{
    background:#141414;border-bottom:1px solid #2a2a2a;
    padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;
  }
  header h1{font-size:18px;font-weight:600;color:#fff;white-space:nowrap;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    letter-spacing:0;margin:0;}
  header h1 span{color:#e53935}
  header nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
  header nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
    border:1px solid #444;border-radius:6px}
  header nav a:hover{color:#fff;border-color:#888}
  header nav a.active{color:#e53935;border-color:#e53935}
  main{max-width:880px;margin:30px auto;padding:0 20px 80px}
  h2{font-family:'Bebas Neue',Impact,sans-serif;letter-spacing:2px;margin-top:32px;color:#fff}
  h3{margin:0 0 6px 0;font-size:14px;letter-spacing:0.5px;display:flex;align-items:center;gap:8px}
  .section-title{
    font-size:11px;color:#888;font-weight:800;letter-spacing:1.5px;
    text-transform:uppercase;margin:24px 0 10px;
  }
  .card{background:#111118;border:1px solid #1e1e2a;border-radius:10px;padding:18px 20px;margin-bottom:14px}

  /* Task picker rows */
  .task-row{
    display:grid;grid-template-columns:160px 1fr;gap:18px;align-items:start;
    padding:14px 0;border-bottom:1px solid #1a1a24;
  }
  .task-row:last-child{border-bottom:none}
  .task-name{font-size:14px;font-weight:700;color:#fff}
  .task-desc{font-size:11px;color:#777;margin-top:4px;line-height:1.5}
  .prov-pills{display:flex;gap:6px;flex-wrap:wrap}
  .prov-pill{
    display:inline-flex;align-items:center;gap:6px;
    padding:6px 12px;border-radius:6px;border:1px solid #2e2e3e;
    background:#181820;color:#bbb;cursor:pointer;font-size:12px;
    font-weight:600;transition:all .15s;
  }
  .prov-pill:hover{border-color:#444;color:#fff}
  .prov-pill.sel{background:linear-gradient(135deg,#e53935,#c62828);
    border-color:#e53935;color:#fff}
  .prov-pill.warn{border-color:#5a4a1a;background:#1a1610;color:#d4a017}
  .prov-pill.warn.sel{background:linear-gradient(135deg,#d4a017,#9a7611);
    border-color:#d4a017;color:#fff}
  .prov-pill .miss{font-size:9px;opacity:.7}
  .pill-warning{
    margin-top:8px;font-size:11px;color:#d4a017;
    display:flex;gap:6px;align-items:center;
  }
  .pill-warning::before{content:"⚠";font-size:13px}

  /* Per-provider settings */
  .prov-card{background:#111118;border:1px solid #1e1e2a;border-radius:10px;
    padding:16px 18px;margin-bottom:12px}
  .prov-card h3{margin-bottom:8px}
  .pill{display:inline-block;padding:2px 9px;border-radius:99px;
    font-size:10px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase}
  .pill.found{background:rgba(34,197,94,0.15);color:#22c55e}
  .pill.missing{background:rgba(239,68,68,0.15);color:#ef4444}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .grid2 > div{min-width:0}
  label{display:block;font-size:10px;color:#8888a0;text-transform:uppercase;
    letter-spacing:1px;margin-bottom:5px;font-weight:700}
  input[type=text]{
    width:100%;padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;
    color:#eee;border-radius:5px;font-family:'JetBrains Mono',monospace;
    font-size:12px;box-sizing:border-box;
  }
  input[type=text]:focus{outline:none;border-color:#e53935}
  textarea{
    width:100%;padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;
    color:#eee;border-radius:5px;font-family:'JetBrains Mono',monospace;
    font-size:11px;line-height:1.5;box-sizing:border-box;resize:vertical;
  }
  textarea:focus{outline:none;border-color:#e53935}
  .prof-btn{
    padding:8px 12px;background:#1a1a24;border:1px solid #2e2e3e;color:#ccc;
    border-radius:5px;cursor:pointer;font-weight:600;font-size:12px;
  }
  .prof-btn:hover{background:#22222e;border-color:#444;color:#fff}
  select:focus{outline:none;border-color:#e53935}

  /* Social channels */
  .social-row{
    display:grid;grid-template-columns:110px 1fr 1.4fr 1fr;gap:8px;
    align-items:center;padding:10px 0;
    border-bottom:1px solid #1a1a24;
  }
  .social-row:last-child{border-bottom:none}
  .social-row .plat{
    display:flex;align-items:center;gap:8px;
    font-size:13px;font-weight:600;color:#fff;
  }
  .social-row .plat svg{width:18px;height:18px;flex-shrink:0}
  .social-row input{font-size:11px}
  .socials-help{
    font-size:11px;color:#888;line-height:1.5;margin-top:14px;
    padding:10px 12px;background:rgba(124,58,237,0.06);
    border:1px solid rgba(124,58,237,0.2);border-radius:6px;
  }
  .socials-help b{color:#c7a8ff}
  .socials-help code{background:#0a0a10;padding:1px 5px;border-radius:3px;
    font-size:10px;color:#ff8a8a}

  /* Settings Wizard */
  .wiz-btn{
    padding:9px 14px;background:linear-gradient(135deg,#7c3aed 0%,#4338ca 100%);
    border:none;color:#fff;border-radius:6px;cursor:pointer;
    font-weight:700;font-size:12px;letter-spacing:.3px;
    display:inline-flex;align-items:center;gap:7px;
    box-shadow:0 0 16px rgba(124,58,237,0.25);
  }
  .wiz-btn:hover{filter:brightness(1.15)}
  .wiz-overlay{
    display:none;position:fixed;inset:0;z-index:9000;
    background:rgba(0,0,0,.7);align-items:center;justify-content:center;
    padding:20px;
  }
  .wiz-overlay.active{display:flex}
  .wiz-modal{
    background:#111118;border:1px solid #2e2e3e;border-radius:12px;
    padding:22px 24px;width:520px;max-width:100%;
    box-shadow:0 20px 60px rgba(0,0,0,.6);
  }
  .wiz-modal h3{font-size:16px;font-weight:700}
  .wiz-modal label{margin-bottom:6px}
  .wiz-modal input{font-size:13px}
  #wiz-status.working{color:#7c3aed}
  #wiz-status.err{color:#ef4444}
  #wiz-status.ok{color:#22c55e}
  button.save:disabled{opacity:0.5;cursor:not-allowed}
  .muted{color:#8888a0;font-size:11px;line-height:1.5}
  .muted code{background:#0a0a10;padding:1px 5px;border-radius:3px;font-size:10px}

  button.save{
    padding:10px 22px;background:linear-gradient(135deg,#e53935,#c62828);
    border:none;color:#fff;border-radius:6px;cursor:pointer;
    font-weight:700;font-size:13px;letter-spacing:.3px;
  }
  button.save:hover{background:linear-gradient(135deg,#ff5252,#e53935)}
  a{color:#ff5252;font-size:11px}
</style></head>
<body>
<!-- pg-chrome -->

<main>
  <h2>Settings</h2>
  <p class="muted">
    Each <b>brand profile</b> is a single JSON file under
    <code>data/profiles/</code> that captures every setting on this page.
    Switch brands with <b>Load</b>; rename the active profile by editing the
    filename and clicking Save.
  </p>

  <div class="section-title">Profile</div>
  <div class="card">
    <div>
      <label>Settings file name</label>
      <input type="text" id="profile-name" placeholder="defaults to brand name">
      <div class="muted" style="margin-top:4px">
        Saved as <code>data/profiles/&lt;name&gt;.json</code>. Saving with a
        new name <b>creates a new profile</b> (the previous one is kept).
        Use the <b>Load Profile</b> button at the bottom to switch to a
        different brand.
      </div>
    </div>
    <div style="margin-top:14px;padding-top:14px;border-top:1px solid #1e1e2a">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <button onclick="openWizard()" class="wiz-btn">
          <span style="font-size:14px">&#10024;</span>
          Settings Wizard
        </button>
        <span class="muted">
          Paste an Instagram, YouTube, TikTok, or website URL and the AI will
          fill in brand name, handle, content domain, and a starter tag schema.
        </span>
      </div>
    </div>
  </div>

  <div id="wiz-overlay" class="wiz-overlay">
    <div class="wiz-modal">
      <h3 style="margin:0 0 8px 0;color:#fff">Settings Wizard</h3>
      <p class="muted" style="margin:0 0 14px 0">
        Paste a URL the AI can read — an Instagram profile, YouTube channel,
        TikTok page, or any brand website. The AI extracts brand details and
        proposes a tag schema; you review &amp; click Save.
      </p>
      <label>URL</label>
      <input type="text" id="wiz-url" placeholder="https://www.instagram.com/your-brand">
      <div id="wiz-status" class="muted" style="min-height:18px;margin-top:10px"></div>
      <div style="margin-top:14px;display:flex;justify-content:flex-end;gap:8px">
        <button onclick="closeWizard()" class="prof-btn">Cancel</button>
        <button id="wiz-go" onclick="runWizard()" class="save">Run</button>
      </div>
    </div>
  </div>

  <div class="section-title">Brand &amp; Content</div>
  <div class="card">
    <div>
      <label>Brand name</label>
      <input type="text" id="brand-name" placeholder="e.g. ClipBuilder, MyChannel">
      <div class="muted" style="margin-top:4px">
        Appears in AI prompts and email defaults. (Social handles live in
        the Social Channels section below.)
      </div>
    </div>
    <div style="margin-top:12px">
      <label>Content domain</label>
      <input type="text" id="content-domain" placeholder="e.g. cooking, MMA, skateboarding, weddings">
      <div class="muted" style="margin-top:4px">
        Plain-English description of the niche. Drops directly into prompts:
        “You are a social media expert for a <b>{content_domain}</b> channel…”
      </div>
    </div>
    <div style="margin-top:14px">
      <label>Tag schema (JSON)</label>
      <textarea id="tag-schema" rows="14" spellcheck="false"></textarea>
      <div class="muted" style="margin-top:4px">
        Categories → list of tag strings. The AI must pick from this list when
        analyzing videos. Schema changes take effect on the next analysis run.
      </div>
    </div>
  </div>

  <div class="section-title">Social Channels</div>
  <div class="card">
    <p class="muted" style="margin:0 0 12px 0">
      Per-platform handles, URLs, and (optionally) cookies for downloads.
      Used by the AI Wizard for caption generation and by <b>Import Videos</b>
      on /analyze.
    </p>
    <div id="socials-rows"></div>
    <div class="socials-help">
      <b>Cookies</b> field — only needed when a platform blocks anonymous
      downloads (common on YouTube and Instagram). Two formats:
      <br>• Browser name: <code>chrome</code>, <code>firefox</code>, <code>brave</code>, <code>edge</code> — yt-dlp pulls cookies directly from your logged-in browser. <b>Recommended.</b>
      <br>• File path: absolute path to a <code>cookies.txt</code> file
      (e.g. exported with the <i>Get cookies.txt LOCALLY</i> browser extension).
      <br>• <b>macOS gotcha:</b> <code>safari</code> usually fails because Safari's
      cookies are sandboxed — use a different browser, or grant Terminal
      Full Disk Access in System Settings → Privacy &amp; Security.
    </div>
  </div>

  <div class="section-title">Folders</div>
  <div class="card">
    <div class="grid2">
      <div>
        <label>Source folder (Analyze page)</label>
        <input type="text" id="source-folder" placeholder="videos">
        <div class="muted" style="margin-top:4px">
          Where raw videos live. Relative paths resolve against the project root.
          Restart the app after changing.
        </div>
      </div>
      <div>
        <label>Output folder (Library page)</label>
        <input type="text" id="output-folder" placeholder="output">
        <div class="muted" style="margin-top:4px">
          Where generated reels are written. Restart the app after changing.
        </div>
      </div>
    </div>
  </div>

  <div class="section-title">Analysis</div>
  <div class="card">
    <p class="muted" style="margin:0 0 12px 0">
      How videos get broken into scenes during analysis. Pick the mode that
      fits this brand's content type.
    </p>
    <div class="grid2">
      <div>
        <label>Mode</label>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">
          <button class="prov-pill" id="amode-visual" data-mode="visual">
            <b>Visual</b> · action / sports
          </button>
          <button class="prov-pill" id="amode-speech" data-mode="speech">
            <b>Speech</b> · tutorials / interviews
          </button>
        </div>
        <div class="muted" style="margin-top:6px;line-height:1.55">
          <b>Visual</b> samples frames + asks the AI to tag each timeframe.
          Best for fights, sports, motion content.<br>
          <b>Speech</b> runs a local Whisper transcription, uses transcript
          segments as scene boundaries, then tags each spoken chunk. Best
          for talking-head content where the spoken topic — not the camera
          cuts — defines a scene.
        </div>
      </div>
      <div id="speech-opts" style="display:none">
        <label>Whisper model</label>
        <select id="whisper-model"
                style="width:100%;padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:5px;font-size:12px">
          <option value="tiny">tiny — 39 MB, fastest, English-leaning</option>
          <option value="base">base — 74 MB, balanced (default)</option>
          <option value="small">small — 244 MB, better accuracy</option>
          <option value="medium">medium — 769 MB, very accurate</option>
          <option value="large-v3">large-v3 — 1.5 GB, best quality (recommended for multilingual)</option>
        </select>
        <div style="height:10px"></div>
        <label>Language (ISO code, optional)</label>
        <input type="text" id="whisper-language"
               placeholder="auto-detect (e.g. en, ru, es, pt)">
        <div class="muted" style="margin-top:6px">
          Lock the language for faster + more accurate transcription. Leave
          blank to auto-detect each video. <b>Multilingual content:</b>
          leave blank and use the <code>large-v3</code> model — it handles
          code-switching far better than smaller models.
        </div>
        <div style="height:12px"></div>
        <label class="tl-check" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="whisper-translate">
          <span>Translate transcript to English</span>
        </label>
        <div class="muted" style="margin-top:4px">
          Whisper transcribes the source audio AND translates to English in
          one pass. Useful for multilingual videos so downstream AI tagging
          gets a uniform English transcript regardless of source language.
          Leaves audio untouched — only affects the cached transcript text.
          Models download on first use to
          <code>~/.cache/huggingface/hub/</code>.
        </div>
      </div>
    </div>
  </div>

  <div class="section-title">Strategy Research</div>
  <div class="card">
    <p class="muted" style="margin:0 0 10px 0">
      The wizard caches a JSON playbook describing how to make a viral Reel
      for this brand's domain — duration, hook strategy, pacing, music style,
      etc. This is fed into every reel-generation prompt. Edit it directly to
      shape what the AI optimizes for, or re-generate with the wizard. Edits
      are persisted when you click <b>Save settings</b>.
    </p>
    <div class="muted" style="margin-bottom:10px">
      Last refreshed: <span id="research-status">(unknown)</span>
    </div>
    <textarea id="research-json" rows="14" spellcheck="false"
              placeholder="(no research cached yet — pick a model and click Re-generate)"></textarea>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="research-model"
              style="padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:5px;font-size:12px;font-family:'JetBrains Mono',monospace">
      </select>
      <button class="prof-btn" onclick="refreshResearch()">Re-generate with AI</button>
      <span id="research-msg" class="muted"></span>
    </div>
  </div>

  <div class="section-title">Task → Provider</div>
  <div class="card" id="tasks-card"></div>

  <div class="section-title">Provider Settings</div>
  <div id="providers-cards"></div>

  <div style="margin-top:18px;display:flex;align-items:center;gap:14px">
    <button class="save" onclick="saveAll()">Save settings</button>
    <button class="prof-btn" onclick="document.getElementById('profile-file-input').click()"
            title="Pick a brand profile JSON from disk; the app will load it and restart.">
      Load Profile
    </button>
    <input type="file" id="profile-file-input" accept=".json,application/json"
           style="display:none" onchange="onProfileFilePicked(this)">
    <span id="msg" class="muted"></span>
  </div>
</main>

<script>
const PROVIDERS = ['claude','codex','gemini','minimax'];
const TASKS = ['analysis','wizard','captions'];
let cfg = null;
let appCfg = null;
// Snapshot of restart-sensitive fields the last time we synced with the
// server. Used after Save to decide whether to prompt for a restart.
let restartSnapshot = null;

function _captureRestartSnapshot(c){
  return {
    profile: c.active_profile || c.profile_name,
    source_folder: c.source_folder || '',
    output_folder: c.output_folder || '',
    tag_schema_json: JSON.stringify(c.tag_schema || {}),
  };
}

function _restartReasons(prev, curr){
  if (!prev) return [];
  const out = [];
  if (prev.source_folder !== curr.source_folder) out.push('source folder');
  if (prev.output_folder !== curr.output_folder) out.push('output folder');
  if (prev.tag_schema_json !== curr.tag_schema_json) out.push('tag schema');
  return out;
}

async function load(){
  const [aiRes, appRes] = await Promise.all([
    fetch('/settings/api/ai'),
    fetch('/settings/api/app'),
  ]);
  cfg = await aiRes.json();
  appCfg = await appRes.json();
  restartSnapshot = _captureRestartSnapshot(appCfg);
  fillFromAppCfg();
  render();
  loadResearch();
  // Wire analysis-mode pill clicks once.
  for (const m of ['visual', 'speech']) {
    const el = document.getElementById('amode-' + m);
    if (el) el.onclick = () => selectAnalysisMode(m);
  }
}

function _currentAnalysisMode(){
  for (const m of ['visual', 'speech']) {
    const el = document.getElementById('amode-' + m);
    if (el && el.classList.contains('sel')) return m;
  }
  return 'visual';
}

function fillFromAppCfg(){
  document.getElementById('brand-name').value     = appCfg.brand_name || '';
  document.getElementById('content-domain').value = appCfg.content_domain || '';
  document.getElementById('source-folder').value  = appCfg.source_folder || '';
  document.getElementById('output-folder').value  = appCfg.output_folder || '';
  document.getElementById('profile-name').value   = appCfg.profile_name || '';
  document.getElementById('tag-schema').value     =
    JSON.stringify(appCfg.tag_schema || {}, null, 2);
  renderSocials(appCfg.socials || {});
  // Analysis mode
  const mode = (appCfg.analysis_mode || 'visual');
  selectAnalysisMode(mode);
  document.getElementById('whisper-model').value =
    appCfg.whisper_model || 'base';
  document.getElementById('whisper-language').value =
    appCfg.whisper_language || '';
  document.getElementById('whisper-translate').checked =
    !!appCfg.whisper_translate;
}

function selectAnalysisMode(mode){
  for (const m of ['visual', 'speech']) {
    const el = document.getElementById('amode-' + m);
    if (el) el.classList.toggle('sel', m === mode);
  }
  // Speech-only options visible only in speech mode.
  const opts = document.getElementById('speech-opts');
  if (opts) opts.style.display = (mode === 'speech') ? '' : 'none';
}

const SOCIAL_PLATFORMS = [
  {key:'instagram', label:'Instagram',
   icon: '<svg viewBox="0 0 24 24" fill="#E4405F"><path d="M7.8 2h8.4C19.4 2 22 4.6 22 7.8v8.4a5.8 5.8 0 0 1-5.8 5.8H7.8C4.6 22 2 19.4 2 16.2V7.8A5.8 5.8 0 0 1 7.8 2m-.2 2A3.6 3.6 0 0 0 4 7.6v8.8C4 18.39 5.61 20 7.6 20h8.8a3.6 3.6 0 0 0 3.6-3.6V7.6C20 5.61 18.39 4 16.4 4H7.6m9.65 1.5a1.25 1.25 0 1 1 0 2.5a1.25 1.25 0 0 1 0-2.5M12 7a5 5 0 1 1 0 10a5 5 0 0 1 0-10m0 2a3 3 0 1 0 0 6a3 3 0 0 0 0-6z"/></svg>'},
  {key:'tiktok', label:'TikTok',
   icon: '<svg viewBox="0 0 24 24"><path fill="#25F4EE" d="M19.6 6.3a4.8 4.8 0 0 1-2.5-2.3h-2.4v12.5a2.6 2.6 0 1 1-2.5-2.7v-2.5a5 5 0 1 0 5 5V8.1a7 7 0 0 0 4 1.3V7a4.8 4.8 0 0 1-1.6-.7z"/><path fill="#FE2C55" d="M21.6 5.6a4.8 4.8 0 0 1-2.5-2.3h-2.4v12.5a2.6 2.6 0 1 1-2.5-2.7v-2.5a5 5 0 1 0 5 5V7.4a7 7 0 0 0 4 1.3V6.3a4.8 4.8 0 0 1-1.6-.7z" opacity=".6"/></svg>'},
  {key:'youtube', label:'YouTube',
   icon: '<svg viewBox="0 0 24 24" fill="#FF0000"><path d="M23 7.2a2.8 2.8 0 0 0-2-2C19.3 4.8 12 4.8 12 4.8s-7.3 0-9 .4a2.8 2.8 0 0 0-2 2C0.7 8.8 0.7 12 0.7 12s0 3.2.4 4.8c.2.9 1 1.7 2 2C2.7 19.2 12 19.2 12 19.2s7.3 0 9-.4a2.8 2.8 0 0 0 2-2c.4-1.6.4-4.8.4-4.8s0-3.2-.4-4.8zM9.6 15.4V8.6L15.6 12l-6 3.4z"/></svg>'},
];

function renderSocials(socials){
  const root = document.getElementById('socials-rows');
  if (!root) return;
  root.innerHTML = '';
  for (const p of SOCIAL_PLATFORMS) {
    const slot = (socials || {})[p.key] || {handle:'', url:'', cookies:''};
    const row = document.createElement('div');
    row.className = 'social-row';
    row.innerHTML = `
      <div class="plat">${p.icon}<span>${p.label}</span></div>
      <input type="text" id="social-${p.key}-handle"
             placeholder="@handle" value="${escapeHtml(slot.handle || '')}">
      <input type="text" id="social-${p.key}-url"
             placeholder="https://..." value="${escapeHtml(slot.url || '')}">
      <input type="text" id="social-${p.key}-cookies"
             placeholder="cookies (browser or path)"
             title="Browser name (safari/chrome/firefox/edge/brave) or absolute path to cookies.txt"
             value="${escapeHtml(slot.cookies || '')}">
    `;
    root.appendChild(row);
  }
}

function _collectSocials(){
  const out = {};
  for (const p of SOCIAL_PLATFORMS) {
    const h = document.getElementById('social-' + p.key + '-handle');
    const u = document.getElementById('social-' + p.key + '-url');
    const c = document.getElementById('social-' + p.key + '-cookies');
    out[p.key] = {
      handle:  h ? h.value.trim() : '',
      url:     u ? u.value.trim() : '',
      cookies: c ? c.value.trim() : '',
    };
  }
  return out;
}

async function onProfileFilePicked(input){
  const file = input && input.files && input.files[0];
  input.value = '';   // reset so picking the same file twice still fires onchange
  if (!file) return;
  let parsed;
  try {
    const text = await file.text();
    parsed = JSON.parse(text);
  } catch (e) {
    setMsg('Not a valid JSON file: ' + e.message, false);
    return;
  }
  setMsg('Loading "' + file.name + '"…', true);
  const r = await fetch('/settings/api/app/import', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({filename: file.name, content: parsed}),
  });
  const d = await r.json();
  if (!d.ok) { setMsg('Load failed: ' + (d.error || 'unknown'), false); return; }
  // Restart so per-profile DB / source folder / tag schema all flip,
  // then reload the page once the new server is up.
  setMsg('Loaded "' + (d.active_profile || file.name) + '". Restarting server…', true);
  await restartAndReload();
}

function setMsg(text, ok){
  const msg = document.getElementById('msg');
  msg.textContent = text;
  msg.style.color = ok ? '#22c55e' : '#ef4444';
}

/* ── Strategy research ───────────────────────────────────────────────── */

async function loadResearch(){
  try {
    const r = await fetch('/wizard/api/research');
    const d = await r.json();
    const ta = document.getElementById('research-json');
    const status = document.getElementById('research-status');
    const badge = (window.pgAiBadge && d.provider)
      ? '  ' + window.pgAiBadge(d.provider, {size:13, title:'Research generated by ' + d.provider})
      : '';
    if (d.research) {
      ta.value = JSON.stringify(d.research, null, 2);
      status.innerHTML = '(cached' + (d.researched_at ? ' ' + d.researched_at : '') + ')' + badge;
    } else {
      ta.value = '';
      status.innerHTML = '(no research yet for this profile)';
    }
    populateResearchModelPicker();
  } catch (e) {
    document.getElementById('research-msg').textContent = 'Load failed: ' + e.message;
  }
}

function populateResearchModelPicker(){
  // Flatten provider × models into one option per (provider, model) pair.
  // The model list is seeded by ai_cli.PROVIDER_DEFAULTS (cheapest-first
  // per provider) and merged with the user's live "Default model" field
  // so a typed custom model shows up too.
  const sel = document.getElementById('research-model');
  if (!sel || !cfg) return;
  const prev = sel.value;
  sel.innerHTML = '';

  // Compute the cheapest globally-available (provider, model) pair using
  // the cross-provider rank from the API. We only count providers whose
  // binary actually resolved on PATH.
  const rank = cfg.cheapest_rank || [];
  let cheapest = null;
  for (const r of rank) {
    const pr = cfg.providers[r.provider];
    if (pr && pr.bin_found) { cheapest = r; break; }
  }
  // Fallback if nothing in rank is installed: pick the configured default
  // of any installed provider, else the first installed provider's first model.
  if (!cheapest) {
    for (const k of PROVIDERS) {
      const p = cfg.providers[k];
      if (p && p.bin_found && (p.model || (p.models || []).length)) {
        cheapest = {provider: k, model: p.model || p.models[0]};
        break;
      }
    }
  }

  for (const key of PROVIDERS) {
    const p = cfg.providers[key];
    if (!p) continue;
    const live = (document.getElementById('model-' + key) || {}).value
                 || p.model || '';
    // Build a deduped list: live model first (if set), then catalog entries.
    const seen = new Set();
    const ordered = [];
    if (live) { ordered.push(live); seen.add(live); }
    for (const m of (p.models || [])) {
      if (m && !seen.has(m)) { ordered.push(m); seen.add(m); }
    }
    if (!ordered.length) continue;
    const grp = document.createElement('optgroup');
    grp.label = (p.label || key) + (p.bin_found ? '' : '  (binary missing)');
    for (const m of ordered) {
      const opt = document.createElement('option');
      opt.value = key + '::' + m;
      let label = m;
      if (m === live) label += '   (default)';
      if (cheapest && key === cheapest.provider && m === cheapest.model) {
        label += '   💸 cheapest';
      }
      opt.textContent = label;
      grp.appendChild(opt);
    }
    sel.appendChild(grp);
  }

  // Default to the cheapest option globally. User's prior selection (if
  // they reopened the picker after switching) wins.
  const cheapestVal = cheapest
    ? cheapest.provider + '::' + cheapest.model : '';
  if (prev && [...sel.options].some(o => o.value === prev)) {
    sel.value = prev;
  } else if (cheapestVal && [...sel.options].some(o => o.value === cheapestVal)) {
    sel.value = cheapestVal;
  } else if (sel.options.length) {
    sel.selectedIndex = 0;
  }
}

async function refreshResearch(){
  const msg = document.getElementById('research-msg');
  const sel = document.getElementById('research-model');
  if (!sel.value) {
    msg.textContent = 'Pick a model first.';
    msg.style.color = '#ef4444';
    return;
  }
  const sep = sel.value.indexOf('::');
  const provider = sep > 0 ? sel.value.slice(0, sep) : sel.value;
  const model    = sep > 0 ? sel.value.slice(sep + 2) : '';
  msg.textContent = 'Asking ' + ((cfg.providers[provider] || {}).label || provider)
                    + ' (' + (model || 'default model') + ') '
                    + 'to research best practices…';
  msg.style.color = '#888';
  try {
    const r = await fetch('/wizard/api/refresh-research', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({provider: provider, model: model}),
    });
    const d = await r.json();
    if (d.status === 'ok' && d.research) {
      document.getElementById('research-json').value =
        JSON.stringify(d.research, null, 2);
      const badge = (window.pgAiBadge && d.provider)
        ? '  ' + window.pgAiBadge(d.provider, {size:13, title:'Research generated by ' + d.provider})
        : '';
      document.getElementById('research-status').innerHTML = '(just refreshed)' + badge;
      msg.textContent = 'Updated.';
      msg.style.color = '#22c55e';
    } else {
      msg.textContent = 'Failed: ' + (d.message || 'unknown');
      msg.style.color = '#ef4444';
    }
  } catch (e) {
    msg.textContent = 'Network error: ' + e.message;
    msg.style.color = '#ef4444';
  }
  setTimeout(() => { msg.textContent = ''; }, 6000);
}

/* ── Settings Wizard ─────────────────────────────────────────────────── */

function openWizard(){
  document.getElementById('wiz-url').value = '';
  setWizStatus('', '');
  document.getElementById('wiz-overlay').classList.add('active');
  setTimeout(() => document.getElementById('wiz-url').focus(), 50);
}
function closeWizard(){
  document.getElementById('wiz-overlay').classList.remove('active');
}
function setWizStatus(text, cls){
  const el = document.getElementById('wiz-status');
  el.textContent = text || '';
  el.className = 'muted' + (cls ? ' ' + cls : '');
  // Mirror to the global log footer so progress/errors are triage-able
  // even after the modal closes.
  if (text && window.pgLog) {
    const lg = (cls === 'err') ? 'error' : (cls === 'ok' ? 'ok' : '');
    window.pgLog('[wizard] ' + text, lg);
  }
}
async function runWizard(){
  const url = document.getElementById('wiz-url').value.trim();
  if (!url) { setWizStatus('Enter a URL first.', 'err'); return; }
  const btn = document.getElementById('wiz-go');
  btn.disabled = true;
  setWizStatus('Fetching page and asking the AI…', 'working');
  try {
    const r = await fetch('/settings/api/app/wizard', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url}),
    });
    const d = await r.json();
    if (!d.ok) {
      setWizStatus('Failed: ' + (d.error || 'unknown'), 'err');
      // If the AI returned something we couldn't parse, the endpoint
      // includes the raw response — dump it to the footer for triage.
      if (d.raw && window.pgLog) {
        const lines = String(d.raw).split(/\\r?\\n/);
        for (const ln of lines) {
          if (ln.trim()) window.pgLog('[wizard:raw] ' + ln, 'error');
        }
      }
      btn.disabled = false;
      return;
    }
    const ext = d.extracted || {};
    if (ext.brand_name)     document.getElementById('brand-name').value     = ext.brand_name;
    if (ext.content_domain) document.getElementById('content-domain').value = ext.content_domain;
    if (ext.tag_schema && typeof ext.tag_schema === 'object') {
      document.getElementById('tag-schema').value =
        JSON.stringify(ext.tag_schema, null, 2);
    }
    if ((ext.socials && typeof ext.socials === 'object') || ext.social_handle) {
      // Merge into the current rows so the user keeps anything they'd
      // already typed in for platforms the AI couldn't detect.
      const merged = Object.assign({}, _collectSocials(), {});
      // Legacy field → IG slot if not otherwise populated
      if (ext.social_handle && !(ext.socials && ext.socials.instagram
                                  && ext.socials.instagram.handle)) {
        merged.instagram = {
          handle: ext.social_handle,
          url:    (merged.instagram && merged.instagram.url) || '',
        };
      }
      if (ext.socials && typeof ext.socials === 'object') {
        for (const k of Object.keys(ext.socials)) {
          const val = ext.socials[k] || {};
          merged[k] = {
            handle:  (val.handle  || (merged[k] && merged[k].handle)  || '').trim(),
            url:     (val.url     || (merged[k] && merged[k].url)     || '').trim(),
            // Wizard scrape never produces cookies — preserve whatever the
            // user already typed in.
            cookies: (merged[k] && merged[k].cookies) || '',
          };
        }
      }
      renderSocials(merged);
    }
    // If profile-name is empty (or matches the previous brand), follow brand.
    if (ext.brand_name) {
      const pn = document.getElementById('profile-name');
      if (!pn.value || pn.value === (appCfg && appCfg.brand_name)) {
        pn.value = ext.brand_name;
      }
    }
    setWizStatus('Done! Form prefilled — review and click Save.', 'ok');
    btn.disabled = false;
    setTimeout(closeWizard, 700);
  } catch (e) {
    setWizStatus('Network error: ' + e.message, 'err');
    btn.disabled = false;
  }
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeWizard();
});

function escapeHtml(s){
  return (s == null ? '' : String(s))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function render(){
  renderTasks();
  renderProviders();
  populateResearchModelPicker();
}

function renderTasks(){
  const root = document.getElementById('tasks-card');
  root.innerHTML = '';
  for (const t of TASKS) {
    const meta = cfg.task_meta[t];
    const chosen = cfg.tasks[t];
    const wantsImages = (t === 'analysis');
    const row = document.createElement('div');
    row.className = 'task-row';
    let pills = '';
    for (const p of PROVIDERS) {
      const settings = cfg.providers[p];
      const sel = (chosen === p) ? ' sel' : '';
      const warn = (wantsImages && !settings.supports_images) ? ' warn' : '';
      const missingLabel = settings.bin_found ? '' : '<span class="miss">(missing)</span>';
      pills += `<button class="prov-pill${sel}${warn}" data-task="${t}" data-prov="${p}">
        ${escapeHtml(settings.label)} ${missingLabel}
      </button>`;
    }
    let warning = '';
    const chosenSettings = cfg.providers[chosen];
    if (wantsImages && !chosenSettings.supports_images) {
      warning = '<div class="pill-warning">'
        + escapeHtml(chosenSettings.label)
        + ' is text-only — video frames can\\'t be analyzed. Pick a provider with image support.</div>';
    }
    if (!chosenSettings.bin_found) {
      warning += '<div class="pill-warning" style="color:#ef4444">'
        + 'Binary <code>' + escapeHtml(chosenSettings.bin || '?') + '</code> not on PATH. '
        + 'Install it or update the path below.</div>';
    }
    row.innerHTML = `
      <div>
        <div class="task-name">${escapeHtml(meta.label)}</div>
        <div class="task-desc">${escapeHtml(meta.description)}</div>
      </div>
      <div>
        <div class="prov-pills">${pills}</div>
        ${warning}
      </div>
    `;
    root.appendChild(row);
  }
  root.querySelectorAll('.prov-pill').forEach(b => {
    b.addEventListener('click', () => {
      cfg.tasks[b.dataset.task] = b.dataset.prov;
      renderTasks();
    });
  });
}

function renderProviders(){
  const root = document.getElementById('providers-cards');
  root.innerHTML = '';
  for (const key of PROVIDERS) {
    const p = cfg.providers[key];
    const usedBy = TASKS.filter(t => cfg.tasks[t] === key)
      .map(t => cfg.task_meta[t].label);
    const usedByText = usedBy.length
      ? 'Used for: ' + usedBy.join(', ')
      : 'Not currently used';
    const card = document.createElement('div');
    card.className = 'prov-card';
    // API-only providers (e.g. MiniMax) authenticate via an env var
    // instead of a CLI binary; render a different "found / missing" pill
    // and an env-var hint instead of a binary path.
    const apiOnly = !!p.auth_env;
    const foundLabel = apiOnly
      ? (p.bin_found ? p.auth_env + ' set' : p.auth_env + ' missing')
      : (p.bin_found ? 'binary found' : 'binary missing');
    const binSection = apiOnly
      ? `<div>
          <label>Auth</label>
          <div class="muted" style="font-size:11px;line-height:1.5;padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;border-radius:4px">
            Set <code>${escapeHtml(p.auth_env)}</code> in your <code>.env</code> file
            ${p.bin_found ? '— ✓ key detected.' : '— key not detected.'}
            <a href="${p.homepage}" target="_blank" rel="noopener" style="color:#1976d2">Get API key</a>
          </div>
        </div>`
      : `<div>
          <label>Binary</label>
          <input type="text" id="bin-${key}" value="${escapeHtml(p.bin || '')}" placeholder="${key}">
          <div class="muted" style="margin-top:4px">
            ${p.bin_resolved
              ? '→ <code>' + escapeHtml(p.bin_resolved) + '</code>'
              : 'Not on PATH. <a href="' + p.homepage + '" target="_blank" rel="noopener">install instructions</a>'}
          </div>
        </div>`;
    card.innerHTML = `
      <h3>
        <span style="color:#fff;font-weight:700;font-size:14px">${escapeHtml(p.label)}</span>
        <span class="pill ${p.bin_found ? 'found' : 'missing'}">
          ${escapeHtml(foundLabel)}
        </span>
        <span class="muted" style="margin-left:auto;font-size:10px">
          ${p.supports_images ? '✓ image input' : '⚠ text-only'}
        </span>
      </h3>
      <div class="muted" style="margin:0 0 12px 0">${escapeHtml(usedByText)}</div>
      <div class="grid2">
        ${binSection}
        <div>
          <label>Default model</label>
          <input type="text" id="model-${key}" value="${escapeHtml(p.model || '')}">
        </div>
      </div>
    `;
    root.appendChild(card);
  }
}

async function saveAll(){
  // Validate JSON tag schema first.
  let tagSchema;
  try {
    const raw = document.getElementById('tag-schema').value.trim();
    tagSchema = raw ? JSON.parse(raw) : {};
  } catch (e) {
    setMsg('Tag schema is not valid JSON: ' + e.message, false);
    return;
  }
  // Validate research JSON (saved as part of the unified Save flow).
  let researchObj = null;
  const researchRaw = document.getElementById('research-json').value.trim();
  if (researchRaw) {
    try {
      researchObj = JSON.parse(researchRaw);
    } catch (e) {
      setMsg('Strategy Research is not valid JSON: ' + e.message, false);
      return;
    }
  }

  // Build the AI sub-block from current form state. API-only providers
  // (auth via env var) skip the binary input — only model is editable.
  const aiProviders = {};
  for (const key of PROVIDERS) {
    const binEl = document.getElementById('bin-' + key);
    const modelEl = document.getElementById('model-' + key);
    aiProviders[key] = {
      bin:   binEl ? binEl.value : '',
      model: modelEl ? modelEl.value : '',
    };
  }

  // Single request — every setting on the page is persisted into the active
  // brand profile (with rename if profile-name changed).
  const body = {
    profile_name:   document.getElementById('profile-name').value,
    brand_name:     document.getElementById('brand-name').value,
    content_domain: document.getElementById('content-domain').value,
    source_folder:  document.getElementById('source-folder').value,
    output_folder:  document.getElementById('output-folder').value,
    tag_schema:     tagSchema,
    socials:        _collectSocials(),
    analysis_mode:     _currentAnalysisMode(),
    whisper_model:     document.getElementById('whisper-model').value,
    whisper_language:  document.getElementById('whisper-language').value,
    whisper_translate: document.getElementById('whisper-translate').checked,
    ai: { tasks: cfg.tasks, providers: aiProviders },
  };
  const r = await fetch('/settings/api/app', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (d.ok === false) {
    setMsg('Save failed: ' + (d.error || 'unknown'), false);
    return;
  }
  // Persist Strategy Research alongside the brand profile. Skipped when the
  // textarea is empty so we don't blow away an existing cache by accident.
  if (researchObj && Object.keys(researchObj).length > 0) {
    try {
      const rr = await fetch('/wizard/api/research', {
        method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({research: researchObj}),
      });
      const rd = await rr.json();
      if (rd.status !== 'saved') {
        setMsg('Settings saved, but research save failed: '
               + (rd.error || 'unknown'), false);
        // Continue — partial success is still useful.
      }
    } catch (e) {
      setMsg('Settings saved, but research save errored: ' + e.message, false);
    }
  }
  const prevSnap = restartSnapshot;
  appCfg = d;
  cfg = await (await fetch('/settings/api/ai')).json();
  fillFromAppCfg();
  render();

  const newSnap = _captureRestartSnapshot(appCfg);
  const reasons = _restartReasons(prevSnap, newSnap);
  restartSnapshot = newSnap;

  if (reasons.length) {
    setMsg('Saved.', true);
    const ok = window.confirm(
      'These changes need a server restart to take effect:\\n  • '
      + reasons.join('\\n  • ')
      + '\\n\\nRestart now? Your browser will refresh automatically.'
    );
    if (ok) {
      await restartAndReload();
      return;  // page will be replaced
    } else {
      setMsg('Saved — restart later for ' + reasons.join(', ')
             + ' to take effect.', true);
    }
  } else {
    setMsg('Saved.', true);
  }
  setTimeout(() => { document.getElementById('msg').textContent = ''; }, 6000);
}

async function restartAndReload(){
  setMsg('Restarting server…', true);
  // Capture the current server-id so we can detect when the new process
  // is actually up. /api/server-id changes on every process start.
  let oldId = null;
  try {
    oldId = (await (await fetch('/api/server-id', {cache:'no-store'})).json()).id;
  } catch (e) {/* don't block restart on this */}

  try {
    await fetch('/settings/api/restart', {method:'POST'});
  } catch (e) {/* connection may close mid-restart, that's fine */}

  // Poll until a new server-id arrives, then reload.
  const start = Date.now();
  const TIMEOUT_MS = 30000;
  const POLL_MS = 500;
  function tick(){
    fetch('/api/server-id', {cache:'no-store'})
      .then(r => r.json())
      .then(d => {
        if (d && d.id && d.id !== oldId) {
          window.location.reload();
        } else {
          schedule();
        }
      })
      .catch(() => schedule());
  }
  function schedule(){
    if (Date.now() - start > TIMEOUT_MS) {
      setMsg('Server did not come back after 30s — refresh manually.', false);
      return;
    }
    setTimeout(tick, POLL_MS);
  }
  schedule();
}

load();
</script>
</body></html>
"""


@settings_bp.route("/settings")
def settings_page():
    from chrome import inject_chrome
    return inject_chrome(SETTINGS_PAGE, active="settings")
