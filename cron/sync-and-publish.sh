#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Add node to PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

LOG_FILE="$PROJECT_DIR/logs/sync.log"
exec >> "$LOG_FILE" 2>&1

YEAR_MONTH=$(date +%Y-%m)

# Send email on failure via Gmail SMTP
on_failure() {
  local exit_code=$?
  echo "FAILED (exit code $exit_code): $(date)"
  python3 -c "
import smtplib
from email.mime.text import MIMEText
msg = MIMEText('sync-and-publish.sh failed at $(date) with exit code $exit_code.\n\nCheck $LOG_FILE for details.')
msg['Subject'] = 'sync-and-publish.sh FAILED - $(date +%Y-%m-%d)'
msg['From'] = 'paxlucta@gmail.com'
msg['To'] = 'abghandour@gmail.com'
with smtplib.SMTP('smtp.gmail.com', 587) as s:
    s.ehlo(); s.starttls()
    s.login('paxlucta@gmail.com', 'ftfqkshvbfwwtxvz')
    s.sendmail('paxlucta@gmail.com', ['abghandour@gmail.com'], msg.as_string())
" 2>/dev/null || true
  echo "Failure email sent."
}
trap on_failure ERR

echo "========================================"
echo "Sync started: $(date)"
echo "========================================"

# 1) Run sync
echo "Running sync..."
node src/ig-sync.js

# 2) Generate engagement reports (30-day rolling + MTD)
echo "Generating engagement reports..."
node src/ig-engagement-report.js

# 3) Generate comprehensive growth report (daily email report)
echo "Generating comprehensive growth report..."
node src/ig-comprehensive-growth-report.js --evening

# 4) Generate insights report (PDF-style dashboard)
echo "Generating insights report..."
node src/ig-insights-report.js

# 5) Generate index page
echo "Generating index page..."
node src/generate-index.js

# 6) Stage report artifacts at repo root (where GitHub Pages serves from).
# Working tree and publish repo are the same directory now; copies are intra-repo.
echo "Staging report files at repo root..."
cp output/engagement-report.html ./
cp output/engagement-rankings.html ./
cp "output/Engagement Rankings.xlsx" ./
cp output/comprehensive-growth-report.html ./
cp output/peacegrappler-insights.html ./
cp "output/engagement-report-${YEAR_MONTH}.html" ./
cp "output/engagement-rankings-${YEAR_MONTH}.html" ./
cp "output/Engagement Rankings ${YEAR_MONTH}.xlsx" ./
cp output/index.html ./

# Video analysis reports (HTML + JSON sidecars)
for f in output/video-analysis-*.html output/video-analysis-*.json; do
  [ -f "$f" ] && cp "$f" ./
done

# 7) Commit and push published artifacts.
# .gitignore excludes .env, node_modules, *.db, logs/, etc., so `git add -A`
# is safe — it only stages the published report files (and any source edits).
git add -A
if git diff --cached --quiet 2>/dev/null; then
  echo "No changes to report files, skipping commit."
else
  git commit -m "Update reports $(date +%Y-%m-%d)"
  git push
fi

# 8) Send success email with attachments
python3 src/mailer.py
echo "Success email sent."

echo "SUCCESS: $(date)"
echo ""
