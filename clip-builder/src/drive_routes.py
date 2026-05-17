"""drive_routes.py — Flask routes for Google Drive integration."""

import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, redirect, request

import drive
from db import (
    get_generated_video_by_id,
    get_video_by_drive_file_id,
    register_video,
    set_generated_video_drive_info,
    set_video_drive_info,
)
from video import VIDEO_DIR

drive_bp = Blueprint("drive", __name__)

# OAuth's installed-app config requires HTTP for localhost.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Pull/push job state (single-user app — one job at a time is fine).
_pull_state = {"running": False, "log": [], "done": False, "ok": False}
_push_state = {}  # video_id -> {running, log, done, ok, link}


def _redirect_uri():
    return request.url_root.rstrip("/") + "/drive/oauth/callback"


# ── Status / config ──────────────────────────────────────────────────────────

@drive_bp.route("/drive/api/status")
def drive_status():
    return jsonify(drive.get_config())


@drive_bp.route("/drive/api/folders", methods=["POST"])
def drive_set_folders():
    """Manual override (Advanced section)."""
    data = request.get_json(force=True) or {}
    drive.set_folders(
        inbox=data.get("inbox_folder_id"),
        outbox=data.get("outbox_folder_id"),
    )
    return jsonify({"ok": True, **drive.get_config()})


@drive_bp.route("/drive/api/folders/auto", methods=["POST"])
def drive_auto_folders():
    """Find-or-create the default ClipBuilder folders in Drive."""
    try:
        drive.find_or_create_default_folders()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **drive.get_config()})


@drive_bp.route("/drive/api/disconnect", methods=["POST"])
def drive_disconnect():
    drive.disconnect()
    return jsonify({"ok": True})


# ── OAuth flow ───────────────────────────────────────────────────────────────

@drive_bp.route("/drive/oauth/start")
def drive_oauth_start():
    try:
        url, _state = drive.auth_start(_redirect_uri())
    except Exception as exc:
        return f"OAuth start failed: {exc}", 400
    return redirect(url)


@drive_bp.route("/drive/oauth/callback")
def drive_oauth_callback():
    try:
        drive.auth_finish(_redirect_uri(), request.url)
    except Exception as exc:
        return f"<h2>OAuth callback failed</h2><pre>{exc}</pre>", 400
    # Auto-create the default folders so the user doesn't have to.
    try:
        drive.find_or_create_default_folders()
    except Exception as exc:
        print(f"[drive] folder auto-create failed: {exc}")
    return redirect("/drive?connected=1")


# ── Inbox: pull videos from Drive ────────────────────────────────────────────

def _pull_worker():
    state = _pull_state
    state["log"] = []
    state["running"] = True
    state["done"] = False
    state["ok"] = False

    def log(msg):
        state["log"].append(msg)
        print(f"[drive-pull] {msg}")

    try:
        log("Listing inbox folder...")
        items = drive.list_inbox_videos()
        log(f"Found {len(items)} video file(s) in Drive inbox")

        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        existing_local = {p.name for p in VIDEO_DIR.iterdir() if p.is_file()}

        downloaded = 0
        for it in items:
            fid = it["id"]
            name = it["name"]
            if get_video_by_drive_file_id(fid):
                continue
            if name in existing_local:
                log(f"Skip (already in videos/): {name}")
                continue
            dest = VIDEO_DIR / name
            log(f"Downloading {name}...")
            try:
                drive.download_file(fid, dest)
            except Exception as exc:
                log(f"FAILED {name}: {exc}")
                continue
            try:
                vid_id = register_video(dest)
                link = f"https://drive.google.com/file/d/{fid}/view"
                set_video_drive_info(vid_id, fid, link)
                log(f"Registered: {name}")
                downloaded += 1
            except Exception as exc:
                log(f"Register failed for {name}: {exc}")

        log(f"DONE — downloaded {downloaded} new file(s)")
        state["ok"] = True
    except Exception as exc:
        log(f"ERROR: {exc}")
    finally:
        state["running"] = False
        state["done"] = True


@drive_bp.route("/drive/api/pull", methods=["POST"])
def drive_pull():
    if _pull_state["running"]:
        return jsonify({"ok": False, "error": "Already running"}), 409
    t = threading.Thread(target=_pull_worker, daemon=True)
    t.start()
    return jsonify({"ok": True})


@drive_bp.route("/drive/api/pull/status")
def drive_pull_status():
    return jsonify(_pull_state)


# ── Outbox: upload generated video to Drive ──────────────────────────────────

def _push_worker(video_id):
    state = _push_state[video_id]
    state["log"] = []
    state["running"] = True
    state["done"] = False
    state["ok"] = False
    state["link"] = None

    def log(msg):
        state["log"].append(msg)
        print(f"[drive-push] {msg}")

    try:
        row = get_generated_video_by_id(video_id)
        if not row:
            log("No such generated video")
            return
        path = Path(row["path"])
        if not path.exists():
            log(f"File missing: {path}")
            return
        log(f"Uploading {path.name} ({path.stat().st_size // (1024*1024)} MB)...")
        result = drive.upload_file(str(path), name=path.name)
        link = result.get("webViewLink") or ""
        set_generated_video_drive_info(video_id, result["id"], link)
        state["link"] = link
        state["ok"] = True
        log(f"DONE — {link}")
    except Exception as exc:
        log(f"ERROR: {exc}")
    finally:
        state["running"] = False
        state["done"] = True


@drive_bp.route("/drive/api/push/<int:video_id>", methods=["POST"])
def drive_push(video_id):
    cur = _push_state.get(video_id)
    if cur and cur.get("running"):
        return jsonify({"ok": False, "error": "Already running"}), 409
    _push_state[video_id] = {"running": True, "log": [], "done": False,
                             "ok": False, "link": None}
    t = threading.Thread(target=_push_worker, args=(video_id,), daemon=True)
    t.start()
    return jsonify({"ok": True})


@drive_bp.route("/drive/api/push/<int:video_id>/status")
def drive_push_status(video_id):
    return jsonify(_push_state.get(video_id, {"running": False, "done": False}))


# ── Settings page ────────────────────────────────────────────────────────────

DRIVE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Google Drive — ClipBuilder</title>
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
  main{max-width:760px;margin:30px auto;padding:0 20px 80px}
  h2{font-family:'Bebas Neue',Impact,sans-serif;letter-spacing:2px;margin-top:32px;color:#fff}
  h3{margin-top:0;font-size:15px;letter-spacing:0.5px}
  .card{background:#111118;border:1px solid #1e1e2a;border-radius:10px;padding:22px;margin-bottom:18px}
  .row{display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap}
  label{display:block;font-size:11px;color:#8888a0;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
  input[type=text]{width:100%;padding:10px;background:#0c0c14;border:1px solid #2e2e3e;color:#eee;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:12px;box-sizing:border-box}
  button{padding:9px 16px;background:#1a1a24;border:1px solid #2e2e3e;color:#eee;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px}
  button.primary{background:linear-gradient(135deg,#e53935,#c62828);border-color:#e53935;color:#fff}
  button:hover{background:#22222e}
  button.primary:hover{background:linear-gradient(135deg,#ff5252,#e53935)}
  button[disabled]{opacity:0.5;cursor:not-allowed}
  .ok{color:#22c55e}
  .err{color:#ef4444}
  .muted{color:#8888a0;font-size:13px;line-height:1.6}
  pre{background:#0a0a10;border:1px solid #1e1e2a;border-radius:6px;padding:10px;font-size:11px;max-height:200px;overflow:auto;white-space:pre-wrap;font-family:'JetBrains Mono',monospace}
  .pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase}
  .pill.on{background:rgba(34,197,94,0.15);color:#22c55e}
  .pill.off{background:rgba(239,68,68,0.15);color:#ef4444}
  a{color:#ff5252}
  details{margin-top:18px}
  details summary{cursor:pointer;color:#8888a0;font-size:12px;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:8px 0;user-select:none}
  details summary:hover{color:#eee}
  details[open] summary{color:#eee;margin-bottom:10px}
  code.path{background:#0a0a10;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#ff8a8a}
  ol{padding-left:22px;line-height:1.8}
  ol li{margin:4px 0}
  .step-num{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:11px;background:#e53935;color:#fff;font-weight:700;font-size:11px;margin-right:8px;vertical-align:middle}
  .folder-link{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;background:#1a1a24;border:1px solid #2e2e3e;border-radius:5px;font-size:12px;text-decoration:none;color:#eee}
  .folder-link:hover{background:#22222e;color:#fff}
  .badge-ready{background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);color:#22c55e;padding:12px 16px;border-radius:8px;margin-bottom:16px;display:none}
  .badge-ready.show{display:block}
  .admin-warn{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.3);padding:14px 18px;border-radius:8px;margin-bottom:18px}
  .admin-warn h3{color:#ef4444;margin-top:0}
</style>
</head>
<body>
<!-- pg-chrome -->

<main>
  <h2>Google Drive</h2>
  <p class="muted">
    Pull source videos from Drive and push generated videos out — no manual file
    transfers, no Finder dance.
  </p>

  <div id="ready-banner" class="badge-ready">
    <b>Drive is connected.</b> Drop new videos into your inbox folder, then use
    "Pull from Drive" on the Analyze page. Generated videos can be uploaded
    from the Library page.
  </div>

  <div id="admin-warn" class="admin-warn" style="display:none">
    <h3>Drive integration not configured</h3>
    <p class="muted" style="margin:0">
      The app admin needs to set up Google Drive credentials before this page
      will work. Open the <b>Admin setup</b> section below for the one-time
      steps, or check the README.
    </p>
  </div>

  <div id="connect-card" class="card">
    <h3>Connect to Google Drive</h3>
    <p class="muted">
      Sign in with the Google account whose Drive you want to use. The app
      will create two folders for you on first connect:
      <code class="path">ClipBuilder Inbox</code> (drop source videos here)
      and <code class="path">ClipBuilder Output</code> (generated videos
      land here).
    </p>
    <div class="row">
      <button class="primary" id="connect-btn"
              onclick="window.location='/drive/oauth/start'">
        Sign in with Google
      </button>
      <span class="muted" style="font-size:12px">
        First time: you'll see a Google sign-in screen, then a permission
        prompt. Approve, and you're done.
      </span>
    </div>
  </div>

  <div id="connected-card" class="card" style="display:none">
    <h3>Connected</h3>
    <div class="row" style="margin-top:14px">
      Inbox folder:
      <a id="inbox-link" class="folder-link" target="_blank" rel="noopener">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
        Open in Drive
      </a>
      <button onclick="autoFolders()">Re-create / refresh</button>
    </div>
    <div class="row">
      Outbox folder:
      <a id="outbox-link" class="folder-link" target="_blank" rel="noopener">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
        Open in Drive
      </a>
    </div>
    <div class="row" style="margin-top:18px">
      <button onclick="pullNow()">Pull from Drive Inbox now</button>
      <button onclick="disconnect()">Disconnect</button>
    </div>
    <pre id="pull-log" style="display:none;margin-top:14px"></pre>
  </div>

  <details>
    <summary>Advanced — use existing Drive folders instead</summary>
    <div class="card">
      <p class="muted">
        Already have folders you want to use? Paste their Drive URLs (or just
        the folder IDs) below.
      </p>
      <label>Inbox folder</label>
      <input type="text" id="inbox" placeholder="https://drive.google.com/drive/folders/...">
      <div style="height:10px"></div>
      <label>Outbox folder</label>
      <input type="text" id="outbox" placeholder="https://drive.google.com/drive/folders/...">
      <div style="height:14px"></div>
      <button onclick="saveFolders()">Save</button>
      <span id="fld-msg" class="muted" style="margin-left:10px"></span>
    </div>
  </details>

  <details id="admin-details">
    <summary>Admin setup — one-time, for whoever installed the app</summary>
    <div class="card">
      <p class="muted">
        Google requires the app developer to register an OAuth client once.
        End-users never see this — they just click "Sign in with Google".
      </p>
      <ol>
        <li>Go to <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud Console</a> and create a project (or pick one).</li>
        <li><b>APIs &amp; Services → Library</b> → search "Google Drive API" → <b>Enable</b>.</li>
        <li><b>APIs &amp; Services → OAuth consent screen</b>: choose <b>External</b>, fill in app name "ClipBuilder", your support email, then <b>Save and continue</b>. On the "Test users" step, add your own Google email (and any other users who'll run the app).</li>
        <li><b>APIs &amp; Services → Credentials → Create Credentials → OAuth client ID</b>:
          <ul>
            <li>Type: <b>Web application</b></li>
            <li>Name: <b>ClipBuilder Local</b></li>
            <li>Authorized redirect URI: <code class="path" id="redirect-uri"></code></li>
          </ul>
          Click <b>Create</b>, then <b>Download JSON</b>.
        </li>
        <li>Save the downloaded file as <code class="path">src/drive_client.json</code> in your ClipBuilder folder, then restart the app.</li>
      </ol>
      <p class="muted" style="margin-top:14px">
        Once <code class="path">src/drive_client.json</code> exists, this page becomes
        a one-click "Sign in with Google" flow for everyone.
      </p>
    </div>
  </details>
</main>

<script>
function setText(id, txt){ var el = document.getElementById(id); if (el) el.textContent = txt; }
async function refresh(){
  const r = await fetch('/drive/api/status'); const d = await r.json();

  // Admin not configured?
  document.getElementById('admin-warn').style.display = d.app_configured ? 'none' : 'block';
  document.getElementById('connect-btn').disabled = !d.app_configured;
  if (!d.app_configured) {
    document.getElementById('admin-details').open = true;
  }

  // Connected vs not
  document.getElementById('connect-card').style.display = d.has_token ? 'none' : '';
  document.getElementById('connected-card').style.display = d.has_token ? '' : 'none';
  document.getElementById('ready-banner').classList.toggle('show', d.has_token);

  if (d.inbox_folder_link) document.getElementById('inbox-link').href = d.inbox_folder_link;
  if (d.outbox_folder_link) document.getElementById('outbox-link').href = d.outbox_folder_link;
  document.getElementById('inbox').value = d.inbox_folder_id || '';
  document.getElementById('outbox').value = d.outbox_folder_id || '';
  setText('redirect-uri', location.origin + '/drive/oauth/callback');
}
async function saveFolders(){
  const body = {
    inbox_folder_id: document.getElementById('inbox').value,
    outbox_folder_id: document.getElementById('outbox').value,
  };
  const r = await fetch('/drive/api/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  document.getElementById('fld-msg').textContent = d.ok ? 'Saved' : 'Error';
  document.getElementById('fld-msg').className = d.ok ? 'ok' : 'err';
  refresh();
}
async function autoFolders(){
  const r = await fetch('/drive/api/folders/auto', {method:'POST'});
  const d = await r.json();
  if (!d.ok) alert('Failed: ' + (d.error || 'unknown'));
  refresh();
}
async function disconnect(){
  if (!confirm('Disconnect Google Drive?')) return;
  await fetch('/drive/api/disconnect', {method:'POST'});
  refresh();
}
async function pullNow(){
  const log = document.getElementById('pull-log');
  log.style.display = 'block';
  log.textContent = 'Starting...';
  const r = await fetch('/drive/api/pull', {method:'POST'});
  if (r.status === 409) { log.textContent = 'Already running'; }
  const poll = async () => {
    const s = await (await fetch('/drive/api/pull/status')).json();
    log.textContent = (s.log || []).join('\\n');
    if (!s.done) setTimeout(poll, 800);
  };
  poll();
}
refresh();
</script>
</body></html>
"""


@drive_bp.route("/drive")
def drive_page():
    from chrome import inject_chrome
    return inject_chrome(DRIVE_PAGE, active="drive")
