#!/usr/bin/env python3
"""PeaceGrappler -- MMA clip builder, analyzer, and montage generator."""

import sys
import webbrowser
from flask import Flask, redirect

from db import get_db
from clip_builder import clip_builder_bp
from analyzer import analyzer_bp
from generator import generator_bp
from library import library_bp

app = Flask(__name__)
app.register_blueprint(clip_builder_bp)
app.register_blueprint(analyzer_bp)
app.register_blueprint(generator_bp)
app.register_blueprint(library_bp)


@app.route("/")
def index():
    return redirect("/builder")


def main():
    port = 5555
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Initialize DB
    get_db()

    print(f"Starting PeaceGrappler at http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
