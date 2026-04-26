#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
echo "Starting PeaceGrappler on http://localhost:5555"
echo "(the browser will open automatically)"
python src/app.py
