#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
echo "Starting PeaceGrappler on http://localhost:5555"
open http://localhost:5555
python src/app.py
