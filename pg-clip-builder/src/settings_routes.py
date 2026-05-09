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

    Accepts: profile_name, brand_name, social_handle, content_domain,
    source_folder, output_folder, tag_schema, ai (the dict expected by
    ai_cli with tasks + providers).
    """
    data = request.get_json(force=True) or {}
    try:
        app_config.set_config(
            profile_name=data.get("profile_name"),
            brand_name=data.get("brand_name"),
            social_handle=data.get("social_handle"),
            content_domain=data.get("content_domain"),
            source_folder=data.get("source_folder"),
            output_folder=data.get("output_folder"),
            tag_schema=data.get("tag_schema"),
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
  "social_handle":  "<@handle for Instagram/TikTok/Twitter/YouTube, or empty if not visible>",
  "content_domain": "<plain-English description of the niche, 2-6 words. Examples: 'sourdough baking', 'urban skateboarding', 'wedding cinematography', 'MMA / combat sports'>",
  "tag_schema": {{
    "activity": ["<20-50 tags SPECIFIC to this niche — what visual actions, techniques, objects, or events would the AI need to recognize in their videos?>"],
    "setting":  ["<10-20 location/environment tags relevant to this niche>"],
    "camera":   ["close-up", "medium-shot", "wide-shot", "overhead", "pov", "handheld", "steady", "tracking", "slow-pan"],
    "energy":   ["high-energy", "medium-energy", "low-energy"],
    "quality":  ["low-quality"]
  }}
}}

Rules:
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
<header><h1>Clip<span>Builder</span></h1>
<nav>
  <a href="/wizard">AI Wizard</a>
  <a href="/builder">Builder</a>
  <a href="/library">Library</a>
  <a href="/rate">Scenes</a>
  <a href="/analyze">Analyze</a>
</nav>
</header>

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
    <div class="grid2">
      <div>
        <label>Active profile</label>
        <div style="display:flex;gap:6px;align-items:center">
          <select id="profile-picker" style="flex:1;padding:8px 10px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:5px;font-size:12px"></select>
          <button onclick="loadProfile()" class="prof-btn">Load</button>
          <button onclick="deleteActiveProfile()" class="prof-btn" title="Delete this profile">&#x1F5D1;</button>
        </div>
      </div>
      <div>
        <label>Settings file name</label>
        <input type="text" id="profile-name" placeholder="defaults to brand name">
        <div class="muted" style="margin-top:4px">
          Saved as <code>data/profiles/&lt;name&gt;.json</code>. Saving with a
          new name <b>creates a new profile</b> (the previous one is kept).
          Use the trash button to delete a profile.
        </div>
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
    <div class="grid2">
      <div>
        <label>Brand name</label>
        <input type="text" id="brand-name" placeholder="e.g. ClipBuilder, MyChannel">
        <div class="muted" style="margin-top:4px">
          Appears in AI prompts and email defaults.
        </div>
      </div>
      <div>
        <label>Social handle</label>
        <input type="text" id="social-handle" placeholder="e.g. @mychannel">
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
    <span id="msg" class="muted"></span>
  </div>
</main>

<script>
const PROVIDERS = ['claude','codex','gemini'];
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
  refreshProfilePicker();
  render();
  loadResearch();
}

function fillFromAppCfg(){
  document.getElementById('brand-name').value     = appCfg.brand_name || '';
  document.getElementById('social-handle').value  = appCfg.social_handle || '';
  document.getElementById('content-domain').value = appCfg.content_domain || '';
  document.getElementById('source-folder').value  = appCfg.source_folder || '';
  document.getElementById('output-folder').value  = appCfg.output_folder || '';
  document.getElementById('profile-name').value   = appCfg.profile_name || '';
  document.getElementById('tag-schema').value     =
    JSON.stringify(appCfg.tag_schema || {}, null, 2);
}

function refreshProfilePicker(){
  const sel = document.getElementById('profile-picker');
  const profiles = appCfg.profiles || [];
  const active   = appCfg.active_profile || appCfg.profile_name || '';
  sel.innerHTML = '';
  if (!profiles.length) {
    sel.innerHTML = '<option>(no profiles)</option>';
    return;
  }
  for (const p of profiles) {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p + (p === active ? '  •  active' : '');
    if (p === active) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function loadProfile(){
  const name = document.getElementById('profile-picker').value;
  if (!name) return;
  if (name === appCfg.active_profile) return;
  const r = await fetch('/settings/api/app/load', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({profile: name}),
  });
  const d = await r.json();
  if (!d.ok) {
    setMsg('Load failed: ' + (d.error || 'unknown'), false);
    return;
  }
  // Force a full reload so every page (Analyze, Library, Builder, Rate)
  // re-reads from the new profile's database.
  setMsg('Loaded "' + name + '". Reloading…', true);
  setTimeout(() => { window.location.reload(); }, 400);
}

async function deleteActiveProfile(){
  const name = appCfg.active_profile;
  if (!name) return;
  if (!confirm('Delete profile "' + name + '"? This cannot be undone.')) return;
  const r = await fetch('/settings/api/app/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({profile: name}),
  });
  const d = await r.json();
  if (!d.ok) { setMsg('Delete failed: ' + (d.error||'unknown'), false); return; }
  appCfg = d;
  cfg = await (await fetch('/settings/api/ai')).json();
  fillFromAppCfg();
  refreshProfilePicker();
  render();
  setMsg('Deleted. Active is now "' + (appCfg.active_profile || '(none)') + '"', true);
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
    if (d.research) {
      ta.value = JSON.stringify(d.research, null, 2);
      status.textContent = '(cached)';
    } else {
      ta.value = '';
      status.textContent = '(no research yet for this profile)';
    }
    populateResearchModelPicker();
  } catch (e) {
    document.getElementById('research-msg').textContent = 'Load failed: ' + e.message;
  }
}

function populateResearchModelPicker(){
  // Flatten provider × models into one option per (provider, model) pair.
  // The model list is seeded by ai_cli.PROVIDER_DEFAULTS and merged with the
  // current "Default model" field so any custom model the user typed shows
  // up too.
  const sel = document.getElementById('research-model');
  if (!sel || !cfg) return;
  const prev = sel.value;
  sel.innerHTML = '';
  for (const key of PROVIDERS) {
    const p = cfg.providers[key];
    if (!p) continue;
    const live = (document.getElementById('model-' + key) || {}).value
                 || p.model || '';
    // Build a deduped, ordered list: live model first, then catalog entries.
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
      opt.value = key + '::' + m;  // "<provider>::<model>"
      opt.textContent = m + (m === live ? '   (default)' : '');
      grp.appendChild(opt);
    }
    sel.appendChild(grp);
  }
  // Default selection: the wizard task's provider × its current default model.
  const wizardProv = cfg.tasks && cfg.tasks.wizard;
  const defaultModel = wizardProv
    ? ((document.getElementById('model-' + wizardProv) || {}).value
       || (cfg.providers[wizardProv] || {}).model || '')
    : '';
  const desired = wizardProv && defaultModel
    ? wizardProv + '::' + defaultModel : '';
  if (prev && [...sel.options].some(o => o.value === prev)) {
    sel.value = prev;
  } else if (desired && [...sel.options].some(o => o.value === desired)) {
    sel.value = desired;
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
      document.getElementById('research-status').textContent = '(just refreshed)';
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
      btn.disabled = false;
      return;
    }
    const ext = d.extracted || {};
    if (ext.brand_name)     document.getElementById('brand-name').value     = ext.brand_name;
    if (ext.social_handle)  document.getElementById('social-handle').value  = ext.social_handle;
    if (ext.content_domain) document.getElementById('content-domain').value = ext.content_domain;
    if (ext.tag_schema && typeof ext.tag_schema === 'object') {
      document.getElementById('tag-schema').value =
        JSON.stringify(ext.tag_schema, null, 2);
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
    card.innerHTML = `
      <h3>
        <span style="color:#fff;font-weight:700;font-size:14px">${escapeHtml(p.label)}</span>
        <span class="pill ${p.bin_found ? 'found' : 'missing'}">
          ${p.bin_found ? 'binary found' : 'binary missing'}
        </span>
        <span class="muted" style="margin-left:auto;font-size:10px">
          ${p.supports_images ? '✓ image input' : '⚠ text-only'}
        </span>
      </h3>
      <div class="muted" style="margin:0 0 12px 0">${escapeHtml(usedByText)}</div>
      <div class="grid2">
        <div>
          <label>Binary</label>
          <input type="text" id="bin-${key}" value="${escapeHtml(p.bin || '')}" placeholder="${key}">
          <div class="muted" style="margin-top:4px">
            ${p.bin_resolved
              ? '→ <code>' + escapeHtml(p.bin_resolved) + '</code>'
              : 'Not on PATH. <a href="' + p.homepage + '" target="_blank" rel="noopener">install instructions</a>'}
          </div>
        </div>
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

  // Build the AI sub-block from current form state.
  const aiProviders = {};
  for (const key of PROVIDERS) {
    aiProviders[key] = {
      bin:   document.getElementById('bin-' + key).value,
      model: document.getElementById('model-' + key).value,
    };
  }

  // Single request — every setting on the page is persisted into the active
  // brand profile (with rename if profile-name changed).
  const body = {
    profile_name:   document.getElementById('profile-name').value,
    brand_name:     document.getElementById('brand-name').value,
    social_handle:  document.getElementById('social-handle').value,
    content_domain: document.getElementById('content-domain').value,
    source_folder:  document.getElementById('source-folder').value,
    output_folder:  document.getElementById('output-folder').value,
    tag_schema:     tagSchema,
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
  refreshProfilePicker();
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
    return SETTINGS_PAGE
