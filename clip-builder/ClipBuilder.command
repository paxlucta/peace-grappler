#!/bin/bash
cd "$(dirname "$0")"
echo "Starting ClipBuilder on http://localhost:5555"
echo "(the browser will open automatically)"
.venv/bin/python src/app.py
