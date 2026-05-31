#!/bin/bash
set -e
set -o pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

export PATH="/usr/local/bin:$PATH"

LOG_FILE="$PROJECT_DIR/logs/sync.log"
MAILER_ERR_LOG="$PROJECT_DIR/logs/sync-mailer-errors.log"
exec >> "$LOG_FILE" 2>&1

# Load secrets from .env (gitignored). Expects GMAIL_PASSWORD_TOKEN.
set -a
. "$PROJECT_DIR/.env"
set +a
: "${GMAIL_PASSWORD_TOKEN:?GMAIL_PASSWORD_TOKEN not set in .env}"

# Lock to prevent overlapping runs (portable: mkdir is atomic).
LOCK_DIR="$PROJECT_DIR/.sync.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date): another instance is running (lock: $LOCK_DIR). Exiting."
  exit 0
fi

YEAR_MONTH=$(date +%Y-%m)

cleanup() {
  local rc=$?
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if [ $rc -ne 0 ]; then
    echo "FAILED (exit code $rc): $(date)"
    NOW=$(date)
    DATE_YMD=$(date +%Y-%m-%d)
    GMAIL_PASSWORD_TOKEN="$GMAIL_PASSWORD_TOKEN" NOW="$NOW" DATE_YMD="$DATE_YMD" EXIT_CODE="$rc" LOG_FILE="$LOG_FILE" \
    python3 -c "
import os, smtplib
from email.mime.text import MIMEText
msg = MIMEText(f'sync-and-publish.sh failed at {os.environ[\"NOW\"]} with exit code {os.environ[\"EXIT_CODE\"]}.\n\nCheck {os.environ[\"LOG_FILE\"]} for details.')
msg['Subject'] = f'sync-and-publish.sh FAILED - {os.environ[\"DATE_YMD\"]}'
msg['From'] = 'paxlucta@gmail.com'
msg['To'] = 'abghandour@gmail.com'
with smtplib.SMTP('smtp.gmail.com', 587) as s:
    s.ehlo(); s.starttls()
    s.login('paxlucta@gmail.com', os.environ['GMAIL_PASSWORD_TOKEN'])
    s.sendmail('paxlucta@gmail.com', ['abghandour@gmail.com'], msg.as_string())
" 2>> "$MAILER_ERR_LOG" && echo "Failure email sent." || echo "Failure email FAILED (see $MAILER_ERR_LOG)."
  fi
}
trap cleanup EXIT

echo "========================================"
echo "Sync started: $(date)"
echo "========================================"

echo "Running sync..."
node src/ig-sync.js

echo "Generating engagement reports..."
node src/ig-engagement-report.js

echo "Generating comprehensive growth report..."
node src/ig-comprehensive-growth-report.js --evening

echo "Generating monthly comprehensive growth report..."
node src/ig-comprehensive-growth-report.js --monthly

echo "Generating insights report..."
node src/ig-insights-report.js

echo "Generating index page..."
node src/generate-index.js

echo "Running video analysis..."
python3 src/ig-video-analysis.py

# Stage report artifacts at repo root (where GitHub Pages serves from).
echo "Staging report files at repo root..."
ARTIFACTS=(
  "output/styles.css"
  "output/engagement-report.html"
  "output/engagement-rankings.html"
  "output/Engagement Rankings.xlsx"
  "output/comprehensive-growth-report.html"
  "output/comprehensive-growth-report-${YEAR_MONTH}.html"
  "output/peacegrappler-insights.html"
  "output/engagement-report-${YEAR_MONTH}.html"
  "output/engagement-rankings-${YEAR_MONTH}.html"
  "output/Engagement Rankings ${YEAR_MONTH}.xlsx"
  "output/index.html"
  "output/video-analysis-index.html"
)
STAGED=()
for src in "${ARTIFACTS[@]}"; do
  if [ -f "$src" ]; then
    cp "$src" ./
    STAGED+=("$(basename "$src")")
  else
    echo "WARN: missing artifact $src"
  fi
done
for f in output/video-analysis-*.html output/video-analysis-*.json; do
  cp "$f" ./
  STAGED+=("$(basename "$f")")
done

# Explicit add of only published artifacts — avoids sweeping in unrelated edits.
if [ ${#STAGED[@]} -gt 0 ]; then
  git add -- "${STAGED[@]}"
fi
if git diff --cached --quiet 2>/dev/null; then
  echo "No changes to report files, skipping commit."
else
  git commit -m "Update reports $(date +%Y-%m-%d)"
  git push
fi

python3 src/mailer.py
echo "Success email sent."

echo "SUCCESS: $(date)"
echo ""
