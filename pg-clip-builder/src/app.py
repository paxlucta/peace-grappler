#!/usr/bin/env python3
"""PeaceGrappler -- MMA clip builder, analyzer, and montage generator."""

import os
import sys
import time
import webbrowser

from flask import Flask, jsonify, redirect

from db import get_db
from clip_builder import clip_builder_bp
from analyzer import analyzer_bp
from generator import generator_bp
from library import library_bp
from wizard import wizard_bp

app = Flask(__name__)
app.register_blueprint(clip_builder_bp)
app.register_blueprint(analyzer_bp)
app.register_blueprint(generator_bp)
app.register_blueprint(library_bp)
app.register_blueprint(wizard_bp)

# Unique token per server process — changes on restart so the browser can detect it
_SERVER_ID = str(time.time())

_LIVE_RELOAD_SCRIPT = """
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
"""


@app.route("/")
def index():
    return redirect("/builder")


@app.route("/api/server-id")
def server_id():
    """Returns a token that changes when the server restarts."""
    return jsonify({"id": _SERVER_ID})


@app.after_request
def inject_live_reload(response):
    """Inject live-reload script into HTML responses when debug mode is on."""
    if (app.debug
            and response.content_type
            and "text/html" in response.content_type):
        data = response.get_data(as_text=True)
        data = data.replace("</body>", _LIVE_RELOAD_SCRIPT + "</body>")
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
