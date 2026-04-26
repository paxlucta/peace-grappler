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
from generator import generator_bp
from library import library_bp
from wizard import wizard_bp
from rating import rating_bp

app = Flask(__name__)
app.register_blueprint(clip_builder_bp)
app.register_blueprint(analyzer_bp)
app.register_blueprint(generator_bp)
app.register_blueprint(library_bp)
app.register_blueprint(wizard_bp)
app.register_blueprint(rating_bp)

ROOT_DIR = Path(__file__).parent.parent

# Unique token per server process — changes on restart so the browser can detect it
_SERVER_ID = str(time.time())

_INJECTED_SCRIPT = """
<script>
(function(){
  var sid='""" + _SERVER_ID + """';
  setInterval(function(){
    fetch('/api/server-id').then(function(r){return r.json()}).then(function(d){
      if(d.id!==sid){location.reload()}
    }).catch(function(){});
  },1500);
})();
</script>
<style>
.update-btn{
  background:none;color:#666;border:1px solid #333;border-radius:6px;
  padding:3px 8px;font-size:11px;cursor:pointer;margin-left:8px;
  transition:all .15s;
}
.update-btn:hover{color:#e53935;border-color:#e53935}
.update-btn.checking{color:#888;border-color:#555;cursor:wait}
.update-btn.updated{color:#4caf50;border-color:#4caf50}
.update-btn.error{color:#ef5350;border-color:#ef5350}
</style>
<script>
(function(){
  var nav = document.querySelector('header nav');
  if (!nav) return;
  // Inject Rate link if missing
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
