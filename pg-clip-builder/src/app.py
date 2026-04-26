#!/usr/bin/env python3
"""PeaceGrappler -- MMA clip builder, analyzer, and montage generator."""

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, redirect

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
/* -- Sticky header -- */
header{position:sticky !important;top:0;z-index:50}

/* -- Update button -- */
.update-btn{
  background:none;color:#666;border:1px solid #333;border-radius:6px;
  padding:3px 8px;font-size:11px;cursor:pointer;margin-left:8px;
  transition:all .15s;
}
.update-btn:hover{color:#e53935;border-color:#e53935}
.update-btn.checking{color:#888;border-color:#555;cursor:wait}
.update-btn.updated{color:#4caf50;border-color:#4caf50}
.update-btn.error{color:#ef5350;border-color:#ef5350}

/* -- Global footer log -- */
#pg-footer{
  position:fixed;bottom:0;left:0;right:0;z-index:90;
  background:#111;border-top:1px solid #2a2a2a;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
}
#pg-footer-bar{
  display:flex;align-items:center;padding:4px 16px;cursor:pointer;
  user-select:none;gap:10px;
}
#pg-footer-bar:hover{background:#1a1a1a}
#pg-footer-bar .ft-label{font-size:11px;color:#888;font-weight:600}
#pg-footer-bar .ft-count{
  font-size:10px;color:#555;background:#222;border-radius:8px;padding:1px 6px;
}
#pg-footer-bar .ft-last{
  font-size:11px;color:#666;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
#pg-footer-bar .ft-arrow{color:#555;font-size:10px;transition:transform .2s}
#pg-footer-bar .ft-arrow.open{transform:rotate(180deg)}
#pg-footer-bar .ft-clear{
  background:none;border:1px solid #333;color:#555;border-radius:4px;
  padding:1px 6px;font-size:10px;cursor:pointer;
}
#pg-footer-bar .ft-clear:hover{color:#e53935;border-color:#e53935}
#pg-log-panel{
  display:none;max-height:200px;overflow-y:auto;padding:6px 16px 8px;
  border-top:1px solid #1a1a1a;
}
#pg-log-panel.open{display:block}
#pg-log-panel .lg{font-size:11px;padding:1px 0;color:#777;font-family:monospace}
#pg-log-panel .lg .lg-time{color:#444;margin-right:6px}
#pg-log-panel .lg.lg-error{color:#ef5350}
#pg-log-panel .lg.lg-ok{color:#4caf50}
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

  /* ── Header: inject Rate link + Update button ── */
  var nav = document.querySelector('header nav');
  if (nav) {
    if (!nav.querySelector('a[href="/rate"]')) {
      var rateLink = document.createElement('a');
      rateLink.href = '/rate';
      rateLink.textContent = 'Rate';
      if (location.pathname === '/rate') rateLink.className = 'active';
      nav.appendChild(rateLink);
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
