#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=== Clean Test: Fresh Install ==="

# 1. Kill the running app
echo "Stopping ClipBuilder..."
lsof -ti:5555 | xargs kill -9 2>/dev/null || true

# 2. Remove the installed app
echo "Removing /Applications/ClipBuilder.app..."
rm -rf /Applications/ClipBuilder.app

# 3. Remove the data directory
echo "Removing ~/ClipBuilder..."
rm -rf "$HOME/ClipBuilder"

# 4. Rebuild
echo "Building..."
bash build-app.sh

echo ""
echo "Ready to test. Now do what a real user would:"
echo "  1. Open ~/Desktop/ClipBuilder.dmg"
echo "  2. Drag ClipBuilder to Applications"
echo "  3. Run: xattr -dr com.apple.quarantine /Applications/ClipBuilder.app; xattr -dr com.apple.provenance /Applications/ClipBuilder.app"
echo "  4. Open ClipBuilder from Applications"
