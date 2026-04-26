#!/bin/bash
set -e
cd "$(dirname "$0")"

DMG_NAME="PeaceGrappler"
STAGE_DIR="/tmp/${DMG_NAME}-build"
DMG_PATH="${HOME}/Desktop/${DMG_NAME}.dmg"

echo "=== Building ${DMG_NAME}.dmg ==="

# Clean previous build
rm -rf "$STAGE_DIR"
rm -f "$DMG_PATH"

# Create staging directory
APP_DIR="${STAGE_DIR}/${DMG_NAME}"
mkdir -p "$APP_DIR"

# Copy source files
echo "Copying source files..."
cp -R src "$APP_DIR/src"
cp Install.command "$APP_DIR/"
cp PeaceGrappler.command "$APP_DIR/"
cp requirements.txt "$APP_DIR/"
cp README.md "$APP_DIR/" 2>/dev/null || true

# Create empty directories for user content
mkdir -p "$APP_DIR/videos"
mkdir -p "$APP_DIR/assets/music"
mkdir -p "$APP_DIR/assets/videos"
mkdir -p "$APP_DIR/output"
mkdir -p "$APP_DIR/data"

# Make scripts executable
chmod +x "$APP_DIR/Install.command"
chmod +x "$APP_DIR/PeaceGrappler.command"

# Remove any __pycache__ or .pyc
find "$APP_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$APP_DIR" -name "*.pyc" -delete 2>/dev/null || true

# Create DMG
echo "Creating DMG..."
hdiutil create \
    -volname "$DMG_NAME" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

# Clean up staging
rm -rf "$STAGE_DIR"

echo ""
echo "DMG created: ${DMG_PATH}"
echo "Size: $(du -h "$DMG_PATH" | cut -f1)"

# Open email with DMG attached
echo "Opening Mail with DMG attached..."
osascript -e "
tell application \"Mail\"
    set newMsg to make new outgoing message with properties {subject:\"PeaceGrappler Installer\", content:\"Here's the PeaceGrappler installer.\n\nInstructions:\n1. Open the DMG\n2. Drag the PeaceGrappler folder to a location on your Mac (e.g. Desktop or Applications)\n3. Double-click Install.command to install dependencies\n4. Double-click PeaceGrappler.command to launch the app\n\nRequirements: macOS with internet connection (installs Homebrew, Python, FFmpeg automatically).\", visible:true}
    tell newMsg
        make new attachment with properties {file name:POSIX file \"${DMG_PATH}\"} at after the last paragraph
    end tell
    activate
end tell
"

echo "Done!"
