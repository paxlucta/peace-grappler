#!/usr/bin/env python3
"""PeaceGrappler -- MMA clip builder, analyzer, and montage generator."""

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, redirect, send_file

from db import get_db
from clip_builder import clip_builder_bp
from analyzer import analyzer_bp

from library import library_bp
from wizard import wizard_bp
from rating import rating_bp

app = Flask(__name__)
app.register_blueprint(clip_builder_bp)
app.register_blueprint(analyzer_bp)

app.register_blueprint(library_bp)
app.register_blueprint(wizard_bp)
app.register_blueprint(rating_bp)

ROOT_DIR = Path(__file__).parent.parent

# Unique token per server process — changes on restart so the browser can detect it
_SERVER_ID = str(time.time())

_INJECTED_SCRIPT = """
<style>
/* -- Design System -- */
@import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Plus+Jakarta+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500&display=swap');

:root{
  --font-display:'Bebas Neue','Impact',sans-serif;
  --font-body:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  --font-mono:'JetBrains Mono','SF Mono','Consolas',monospace;
  --bg-base:#08080c;
  --bg-surface:#111118;
  --bg-elevated:#1a1a24;
  --bg-hover:#22222e;
  --accent:#e53935;
  --accent-hover:#ff5252;
  --accent-glow:rgba(229,57,53,0.25);
  --accent-gold:#d4a017;
  --text-1:#eeeef2;
  --text-2:#8888a0;
  --text-3:#55556a;
  --border:#1e1e2a;
  --border-hover:#2e2e3e;
  --success:#22c55e;
  --error:#ef4444;
  --radius:10px;
  --radius-sm:6px;
  --radius-lg:16px;
}

body{
  background:var(--bg-base) !important;
  color:var(--text-1) !important;
  font-family:var(--font-body) !important;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border-hover);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#3a3a4e}

/* -- Sticky header -- */
header{
  position:sticky !important;top:0;z-index:50;
  display:flex !important;align-items:center !important;
  gap:16px !important;flex-wrap:nowrap !important;
  background:linear-gradient(180deg,#0e0e16 0%,#0b0b12 100%) !important;
  border-bottom:1px solid var(--border) !important;
  padding:14px 24px !important;
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
}
header h1{
  font-family:var(--font-display) !important;
  font-size:26px !important;font-weight:400 !important;
  letter-spacing:3px !important;text-transform:uppercase !important;
  color:var(--text-1) !important;
  white-space:nowrap !important;flex-shrink:0 !important;
}
header h1 span{color:var(--accent) !important}
nav{
  display:flex !important;gap:8px !important;
  margin-left:auto !important;flex-shrink:0 !important;
  align-items:center !important;
}
nav a{
  font-family:var(--font-body) !important;font-weight:600 !important;
  font-size:11px !important;letter-spacing:0.8px !important;
  text-transform:uppercase !important;
  padding:6px 14px !important;border-radius:var(--radius-sm) !important;
  border:1px solid var(--border) !important;color:var(--text-2) !important;
  text-decoration:none !important;transition:all 0.2s ease !important;
}
nav a:hover{
  color:var(--text-1) !important;border-color:var(--border-hover) !important;
  background:var(--bg-hover) !important;
}
nav a.active{
  color:var(--accent) !important;border-color:var(--accent) !important;
  background:rgba(229,57,53,0.08) !important;
  box-shadow:0 0 12px rgba(229,57,53,0.1) !important;
}

/* -- Global overrides -- */
button{
  font-family:var(--font-body) !important;font-weight:600 !important;
  letter-spacing:0.3px !important;transition:all 0.2s ease !important;
  border-radius:var(--radius-sm) !important;
}
button.primary,.btn-primary{
  background:linear-gradient(135deg,#e53935 0%,#c62828 100%) !important;
  box-shadow:0 2px 12px rgba(229,57,53,0.25) !important;
  border-color:#e53935 !important;
}
button.primary:hover,.btn-primary:hover{
  background:linear-gradient(135deg,#ff5252 0%,#e53935 100%) !important;
  box-shadow:0 4px 20px rgba(229,57,53,0.35) !important;
  transform:translateY(-1px);
}
input,select,textarea{
  font-family:var(--font-body) !important;
  transition:border-color 0.2s ease,box-shadow 0.2s ease !important;
}
input:focus,select:focus,textarea:focus{
  border-color:var(--accent) !important;
  box-shadow:0 0 0 2px var(--accent-glow) !important;
  outline:none !important;
}
th{
  font-family:var(--font-body) !important;font-size:10px !important;
  font-weight:700 !important;letter-spacing:1.2px !important;
  text-transform:uppercase !important;color:var(--text-3) !important;
}
@keyframes pgFadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
main,.content,table{animation:pgFadeIn 0.35s ease-out}

/* -- Header logo -- */
.header-logo{
  height:28px;width:auto;flex-shrink:0;
  filter:invert(1);
  margin-right:4px;
}

/* -- Update button -- */
.update-btn{
  background:none;color:var(--text-3);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:4px 10px;font-size:10px;cursor:pointer;margin-left:8px;
  transition:all .2s;letter-spacing:0.5px;text-transform:uppercase;
}
.update-btn:hover{color:var(--accent);border-color:var(--accent)}
.update-btn.checking{color:var(--text-3);border-color:var(--border);cursor:wait}
.update-btn.updated{color:var(--success);border-color:var(--success)}
.update-btn.error{color:var(--error);border-color:var(--error)}

/* -- Global footer log -- */
#pg-footer{
  position:fixed;bottom:0;left:0;right:0;z-index:90;
  background:linear-gradient(180deg,#0c0c14 0%,#08080e 100%);
  border-top:1px solid var(--border);
  font-family:var(--font-body);
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
}
#pg-footer-bar{
  display:flex;align-items:center;padding:5px 20px;cursor:pointer;
  user-select:none;gap:10px;
}
#pg-footer-bar:hover{background:rgba(255,255,255,0.02)}
#pg-footer-bar .ft-label{font-size:10px;color:var(--text-3);font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
#pg-footer-bar .ft-count{
  font-size:10px;color:var(--text-3);background:var(--bg-elevated);border-radius:8px;padding:1px 7px;font-weight:600;
}
#pg-footer-bar .ft-last{
  font-size:11px;color:var(--text-3);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
#pg-footer-bar .ft-arrow{color:var(--text-3);font-size:10px;transition:transform .2s}
#pg-footer-bar .ft-arrow.open{transform:rotate(180deg)}
#pg-footer-bar .ft-clear{
  background:none;border:1px solid var(--border);color:var(--text-3);border-radius:4px;
  padding:1px 7px;font-size:10px;cursor:pointer;
}
#pg-footer-bar .ft-clear:hover{color:var(--accent);border-color:var(--accent)}
#pg-log-panel{
  display:none;max-height:200px;overflow-y:auto;padding:6px 20px 8px;
  border-top:1px solid var(--border);
}
#pg-log-panel.open{display:block}
#pg-log-panel .lg{font-size:11px;padding:2px 0;color:var(--text-2);font-family:var(--font-mono)}
#pg-log-panel .lg .lg-time{color:var(--text-3);margin-right:8px}
#pg-log-panel .lg.lg-error{color:var(--error)}
#pg-log-panel .lg.lg-ok{color:var(--success)}
</style>

<div id="pg-footer">
  <div id="pg-footer-bar" onclick="window._pgToggleLog()">
    <span class="ft-label">LOG</span>
    <span class="ft-count" id="pg-log-count">0</span>
    <span class="ft-last" id="pg-log-last"></span>
    <button class="ft-clear" onclick="event.stopPropagation();window._pgClearLog()">Clear</button>
    <span class="ft-arrow" id="pg-log-arrow">&#9650;</span>
  </div>
  <div id="pg-log-panel"></div>
</div>

<script>
(function(){
  /* ── Live reload ── */
  var sid='""" + _SERVER_ID + """';
  setInterval(function(){
    fetch('/api/server-id').then(function(r){return r.json()}).then(function(d){
      if(d.id!==sid){location.reload()}
    }).catch(function(){});
  },1500);

  /* ── Header: inject logo ── */
  var h1 = document.querySelector('header h1');
  if (h1 && !document.querySelector('.header-logo')) {
    var logo = document.createElement('img');
    logo.src = '/api/logo';
    logo.alt = 'PeaceGrappler';
    logo.className = 'header-logo';
    h1.parentNode.insertBefore(logo, h1);
  }

  /* ── Header: inject Rate badge + Update button ── */
  var nav = document.querySelector('header nav');
  if (nav) {
    var scenesLink = nav.querySelector('a[href="/rate"]');
    if (scenesLink) {
      scenesLink.style.position = 'relative';
      fetch('/rate/api/scenes').then(function(r){return r.json()}).then(function(data){
        var unrated = data.filter(function(s){return !s.excluded && s.status==='unrated'}).length;
        if (unrated > 0) {
          var badge = document.createElement('span');
          badge.style.cssText = 'position:absolute;top:-6px;right:-6px;background:var(--accent,#e53935);color:#fff;font-size:8px;font-weight:800;min-width:16px;height:16px;border-radius:8px;display:flex;align-items:center;justify-content:center;padding:0 4px;';
          badge.textContent = unrated;
          scenesLink.appendChild(badge);
        }
      });
    }
    var btn = document.createElement('button');
    btn.className = 'update-btn';
    btn.textContent = 'Check for Updates';
    btn.onclick = async function() {
      btn.className = 'update-btn checking';
      btn.textContent = 'Checking...';
      btn.disabled = true;
      try {
        var res = await fetch('/api/update', {method:'POST'});
        var data = await res.json();
        if (data.status === 'updated') {
          btn.className = 'update-btn updated';
          btn.textContent = 'Updated! Restarting...';
        } else if (data.status === 'up-to-date') {
          btn.className = 'update-btn updated';
          btn.textContent = 'Up to date';
          setTimeout(function(){ btn.className='update-btn'; btn.textContent='Check for Updates'; btn.disabled=false; }, 3000);
        } else {
          btn.className = 'update-btn error';
          btn.textContent = data.message || 'Update failed';
          setTimeout(function(){ btn.className='update-btn'; btn.textContent='Check for Updates'; btn.disabled=false; }, 4000);
        }
      } catch(e) {
        btn.className = 'update-btn error';
        btn.textContent = 'Error';
        setTimeout(function(){ btn.className='update-btn'; btn.textContent='Check for Updates'; btn.disabled=false; }, 3000);
      }
    };
    nav.appendChild(btn);
  }

  /* ── Global footer log ── */
  var logCount = 0;
  var panel = document.getElementById('pg-log-panel');
  var countEl = document.getElementById('pg-log-count');
  var lastEl = document.getElementById('pg-log-last');
  var arrowEl = document.getElementById('pg-log-arrow');

  window.pgLog = function(msg, cls) {
    if (!msg || typeof msg !== 'string') return;
    msg = msg.trim();
    if (!msg || msg === 'waiting...') return;
    logCount++;
    countEl.textContent = logCount;
    lastEl.textContent = msg;

    var div = document.createElement('div');
    div.className = 'lg' + (cls ? ' lg-' + cls : '');
    var now = new Date();
    var ts = now.getHours().toString().padStart(2,'0') + ':'
           + now.getMinutes().toString().padStart(2,'0') + ':'
           + now.getSeconds().toString().padStart(2,'0');
    div.innerHTML = '<span class="lg-time">' + ts + '</span>' + msg.replace(/</g,'&lt;');
    panel.appendChild(div);
    panel.scrollTop = panel.scrollHeight;
  };

  window._pgToggleLog = function() {
    panel.classList.toggle('open');
    arrowEl.classList.toggle('open');
  };

  window._pgClearLog = function() {
    panel.innerHTML = '';
    logCount = 0;
    countEl.textContent = '0';
    lastEl.textContent = '';
  };

  /* Hook into existing addLine functions to feed the global log */
  var _origAddLine = window.addLine;
  if (typeof _origAddLine === 'function') {
    window.addLine = function(text, cls) {
      _origAddLine(text, cls);
      var logCls = (cls === 'done' || cls === 'video-ready') ? 'ok' : (cls === 'error' ? 'error' : '');
      window.pgLog(text, logCls);
    };
  }

  /* Also hook addPipelineLine if it exists */
  var _origPipeLine = window.addPipelineLine;
  if (typeof _origPipeLine === 'function') {
    window.addPipelineLine = function(text, cls) {
      _origPipeLine(text, cls);
      var logCls = (cls === 'done') ? 'ok' : (cls === 'error' ? 'error' : '');
      window.pgLog(text, logCls);
    };
  }

  /* Log page load */
  window.pgLog('Page loaded: ' + location.pathname);

  /* Patch EventSource to log SSE messages to the footer */
  var _OrigES = window.EventSource;
  if (_OrigES) {
    window.EventSource = function(url) {
      var es = new _OrigES(url);
      es.addEventListener('message', function(e) {
        try {
          var data = JSON.parse(e.data);
          if (data.message) {
            var msg = data.message;
            var cls = '';
            if (msg.startsWith('DONE:')) cls = msg === 'DONE:ok' ? 'ok' : 'error';
            window.pgLog(msg, cls);
          }
        } catch(ex) {}
      });
      return es;
    };
    window.EventSource.prototype = _OrigES.prototype;
    window.EventSource.CONNECTING = _OrigES.CONNECTING;
    window.EventSource.OPEN = _OrigES.OPEN;
    window.EventSource.CLOSED = _OrigES.CLOSED;
  }
})();
</script>
"""


@app.route("/")
def index():
    return redirect("/builder")


@app.route("/api/server-id")
def server_id():
    """Returns a token that changes when the server restarts."""
    return jsonify({"id": _SERVER_ID})


@app.route("/api/logo")
def logo():
    """Serve the logo image."""
    logo_path = ROOT_DIR / "assets" / "logo.jpeg"
    if logo_path.exists():
        return send_file(str(logo_path), mimetype="image/jpeg")
    return "", 404


@app.route("/api/update", methods=["POST"])
def check_for_updates():
    """Pull latest code from git. Werkzeug reloader restarts if .py files changed."""
    try:
        # Find the git repo root (may be parent of pg-clip-builder)
        git_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=str(ROOT_DIR), timeout=10,
        ).stdout.strip()

        if not git_root:
            return jsonify({"status": "error", "message": "Not a git repository"})

        # Fetch latest
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True, text=True, cwd=git_root, timeout=30,
        )
        if fetch.returncode != 0:
            return jsonify({"status": "error",
                            "message": "Fetch failed: " + fetch.stderr.strip()})

        # Check if there are updates
        local = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=git_root, timeout=10,
        ).stdout.strip()

        remote = subprocess.run(
            ["git", "rev-parse", "@{u}"],
            capture_output=True, text=True, cwd=git_root, timeout=10,
        ).stdout.strip()

        if local == remote:
            return jsonify({"status": "up-to-date"})

        # Pull (reset to match remote — safe because user code is in videos/assets/output)
        result = subprocess.run(
            ["git", "reset", "--hard", "@{u}"],
            capture_output=True, text=True, cwd=git_root, timeout=30,
        )

        if result.returncode != 0:
            return jsonify({"status": "error",
                            "message": "Pull failed: " + result.stderr.strip()})

        # Reinstall deps in case requirements.txt changed
        venv_pip = ROOT_DIR / ".venv" / "bin" / "pip"
        req_file = ROOT_DIR / "requirements.txt"
        if venv_pip.exists() and req_file.exists():
            subprocess.Popen(
                [str(venv_pip), "install", "-q", "-r", str(req_file)],
                cwd=str(ROOT_DIR),
            )

        return jsonify({"status": "updated",
                        "from": local[:8], "to": remote[:8]})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.after_request
def inject_scripts(response):
    """Inject live-reload and update button into HTML responses."""
    if (response.content_type
            and "text/html" in response.content_type):
        data = response.get_data(as_text=True)
        data = data.replace("</body>", _INJECTED_SCRIPT + "</body>")
        response.set_data(data)
    return response


def main():
    port = 5555
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Initialize DB
    get_db()

    use_reloader = "--no-reload" not in sys.argv
    debug = use_reloader

    # Only open browser on first launch, not on reloader restarts
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        print(f"Starting PeaceGrappler at http://localhost:{port}")
        if use_reloader:
            print("Auto-reload enabled — watching for file changes")
        webbrowser.open(f"http://localhost:{port}")

    app.run(host="0.0.0.0", port=port, debug=debug,
            use_reloader=use_reloader)


if __name__ == "__main__":
    main()
