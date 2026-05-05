#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=== Clean Test: Fresh Install ==="

# 1. Kill the running app
echo "Stopping PeaceGrappler..."
lsof -ti:5555 | xargs kill -9 2>/dev/null || true

# 2. Remove the installed app
echo "Removing /Applications/PeaceGrappler.app..."
rm -rf /Applications/PeaceGrappler.app

# 3. Remove the data directory
echo "Removing ~/PeaceGrappler..."
rm -rf "$HOME/PeaceGrappler"

# 4. Rebuild
echo "Building..."
bash build-app.sh

echo ""
echo "Ready to test. Now do what a real user would:"
echo "  1. Open ~/Desktop/PeaceGrappler.dmg"
echo "  2. Drag PeaceGrappler to Applications"
echo "  3. Run: xattr -dr com.apple.quarantine /Applications/PeaceGrappler.app; xattr -dr com.apple.provenance /Applications/PeaceGrappler.app"
echo "  4. Open PeaceGrappler from Applications"
