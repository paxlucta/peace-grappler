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

# Open email with DMG attached and detailed instructions
echo "Opening Mail with DMG attached..."

EMAIL_BODY="Hi!

Here's PeaceGrappler — the AI-powered MMA highlight reel builder.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO INSTALL (one-time setup, ~5 min)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Download the attached PeaceGrappler.dmg file
2. Double-click the DMG file to open it
3. Drag the \"PeaceGrappler\" folder to your Desktop (or wherever you like)
4. Open the PeaceGrappler folder
5. Double-click \"Install.command\"
   - If macOS says it can't be opened: right-click it → Open → click Open
   - A Terminal window will appear — let it finish (it installs everything automatically)
   - You may be asked for your Mac password — this is normal
6. Wait until you see \"Installation complete!\" — then close the Terminal window

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO USE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Double-click \"PeaceGrappler.command\" to launch the app
   - If macOS says it can't be opened: right-click it → Open → click Open
   - The app will open in your web browser at localhost:5555

2. ADD YOUR VIDEOS
   - Drop your raw MMA video files (.mp4, .mov) into the \"videos\" folder inside PeaceGrappler

3. ANALYZE (one-time per video)
   - Click \"Analyze\" in the top menu
   - Click \"Scan for New Videos\" to find your files
   - Click \"Analyze All\" to let the AI tag every scene (takes a few minutes per video)

4. GENERATE HIGHLIGHTS
   - Click \"AI Wizard\" in the top menu
   - Choose your AI model (Sonnet is recommended)
   - Click \"Generate\" — the AI will create an Instagram-ready highlight reel
   - Watch the video, leave feedback to improve future videos

5. BROWSE & POST
   - Click \"Library\" to see all generated videos
   - Click any video to watch it, copy the AI-generated caption, and post to Instagram

6. RATE SCENES
   - Click \"Scenes\" to browse all detected scenes
   - Thumbs up scenes you like, thumbs down scenes you don't want used
   - The AI learns from your ratings!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• To add background music: drop .mp3 files into PeaceGrappler → assets → music
• To add an intro/outro: put intro.mp4 or outro.mp4 in PeaceGrappler → assets → videos
• The app auto-updates: click \"Check for Updates\" in the top-right corner
• To stop the app: close the Terminal window that opened when you launched it

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Mac computer (macOS 12 or later)
• Internet connection (for initial setup and AI features)

Everything else is installed automatically. Enjoy!"

osascript -e "
tell application \"Mail\"
    set newMsg to make new outgoing message with properties {subject:\"PeaceGrappler — MMA Highlight Reel Builder\", content:\"$(echo "$EMAIL_BODY" | sed 's/"/\\"/g')\", visible:true}
    tell newMsg
        make new attachment with properties {file name:POSIX file \"${DMG_PATH}\"} at after the last paragraph
    end tell
    activate
end tell
"

echo "Done!"
