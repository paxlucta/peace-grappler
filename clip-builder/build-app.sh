#!/bin/bash
set -e
cd "$(dirname "$0")"

DMG_NAME="ClipBuilder"
APP_NAME="${DMG_NAME}.app"
STAGE_DIR="/tmp/${DMG_NAME}-app-build"
DMG_PATH="${HOME}/Desktop/${DMG_NAME}.dmg"
GH_REPO="abghandour/clip-builder"
GH_REPO_HTTPS="https://github.com/${GH_REPO}.git"
APP_DATA_DIR="\$HOME/ClipBuilder"        # in-quotes: literal $HOME for the
                                          # heredoc'd launcher/install scripts

echo "=== Building ${APP_NAME} ==="

rm -rf "$STAGE_DIR"
rm -f "$DMG_PATH"
mkdir -p "$STAGE_DIR"

# ── Step 1: Create native AppleScript app (gives us a real arm64 binary) ──
echo "Creating native launcher..."
APP="${STAGE_DIR}/${APP_NAME}"

osacompile -o "${APP}" -e '
on run
    set bundlePath to POSIX path of (path to me)
    set launcherPath to bundlePath & "Contents/MacOS/_launcher"
    do shell script "bash " & quoted form of launcherPath & " >> /dev/null 2>&1"
end run
'

# osacompile created the .app with an "applet" binary. Now add our stuff.
CONTENTS="${APP}/Contents"
MACOS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"
BUNDLED="${RESOURCES}/app"

mkdir -p "$BUNDLED"

# ── Step 2: Bundle source files ──
echo "Bundling source files..."
cp -R src "$BUNDLED/src"
cp -R assets "$BUNDLED/assets"
cp requirements.txt "$BUNDLED/"
cp Install.command "$BUNDLED/"
cp ClipBuilder.command "$BUNDLED/"
cp .env.example "$BUNDLED/" 2>/dev/null || true
cp README.md "$BUNDLED/" 2>/dev/null || true
find "$BUNDLED" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLED" -name "*.pyc" -delete 2>/dev/null || true

# ── Step 3: Create icon ──
# Source PNG is the ClipBuilder logo in assets/images/. Anything reasonably
# square works — sips downsamples for each icon size.
echo "Creating app icon..."
LOGO_SRC="assets/images/Clip Builder Logo.png"
if [ ! -f "$LOGO_SRC" ]; then
    echo "  ⚠  Logo missing at $LOGO_SRC — building with default app icon."
else
    SRC_PNG="/tmp/cb_logo_src.png"
    sips -s format png "$LOGO_SRC" --out "$SRC_PNG" &>/dev/null
    ICON_DIR="/tmp/CB_icon.iconset"
    rm -rf "$ICON_DIR" && mkdir -p "$ICON_DIR"
    for sz in 16 32 128 256 512; do
        sips -z $sz $sz "$SRC_PNG" --out "${ICON_DIR}/icon_${sz}x${sz}.png" &>/dev/null
        sz2=$((sz * 2))
        sips -z $sz2 $sz2 "$SRC_PNG" --out "${ICON_DIR}/icon_${sz}x${sz}@2x.png" &>/dev/null
    done
    iconutil -c icns "$ICON_DIR" -o "${RESOURCES}/AppIcon.icns" 2>/dev/null && echo "Icon ✓" || echo "Icon failed"
    rm -rf "$ICON_DIR" "$SRC_PNG"
fi

# ── Step 4: Info.plist ──
cat > "${CONTENTS}/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ClipBuilder</string>
    <key>CFBundleDisplayName</key>
    <string>ClipBuilder</string>
    <key>CFBundleIdentifier</key>
    <string>com.clipbuilder.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>applet</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# ── Step 5: Launcher + install scripts (heredoc'd into the bundle) ──
# These are template-expanded (not 'EOF'-quoted) so $GH_REPO_HTTPS and friends
# substitute at build time. Inner $-references that must survive to runtime
# use \$ escaping.
cat > "${MACOS}/_launcher" << LAUNCHER
#!/bin/bash
DATA_DIR="${APP_DATA_DIR}"
BUNDLE_DIR="\$(cd "\$(dirname "\$0")/.." && pwd)"
RESOURCES="\${BUNDLE_DIR}/Resources/app"
LOG="\$DATA_DIR/.launch.log"

# First-run: hand off to the Terminal-visible installer so the user sees
# Homebrew/pip output. The installer re-launches us when it finishes.
if [ ! -d "\$DATA_DIR/.venv" ]; then
    INSTALL_SCRIPT="\${BUNDLE_DIR}/MacOS/_install"
    osascript -e "tell application \\"Terminal\\"
        activate
        do script \\"bash '\${INSTALL_SCRIPT}' ; exit\\"
    end tell" 2>/dev/null
    exit 0
fi

# Bootstrap the git working tree on first launch after install so future
# 'Check for Updates' can pull from origin/main.
if [ ! -d "\$DATA_DIR/.git" ]; then
    rsync -a --delete "\$RESOURCES/src/" "\$DATA_DIR/src/"
    cp "\$RESOURCES/requirements.txt" "\$DATA_DIR/" 2>/dev/null || true
    cd "\$DATA_DIR"
    git init -q 2>/dev/null || true
    git remote add origin ${GH_REPO_HTTPS} 2>/dev/null || true
    git fetch -q origin 2>/dev/null || true
    git reset --hard origin/main 2>/dev/null || true
    git branch -M main 2>/dev/null || true
    git branch --set-upstream-to=origin/main main 2>/dev/null || true
fi

# Launch
cd "\$DATA_DIR"
lsof -ti:5555 | xargs kill -9 2>/dev/null || true
sleep 0.3

echo "Starting ClipBuilder at \$(date)" > "\$LOG"
.venv/bin/python src/app.py --no-reload >> "\$LOG" 2>&1 &
APP_PID=\$!

# Wait for server to be ready
for i in \$(seq 1 30); do
    if curl -s http://localhost:5555 > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if ! curl -s http://localhost:5555 > /dev/null 2>&1; then
    osascript -e 'display dialog "ClipBuilder failed to start." with title "ClipBuilder" buttons {"OK"} with icon caution' 2>/dev/null
    kill \$APP_PID 2>/dev/null
    exit 1
fi

# Open Chrome in app mode, fall back to default browser
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CB_CHROME_DIR="\$DATA_DIR/.chrome-app"
if [ -x "\$CHROME" ]; then
    "\$CHROME" --app=http://localhost:5555 --user-data-dir="\$CB_CHROME_DIR" --window-size=1400,900 &
    BROWSER_PID=\$!
else
    open http://localhost:5555
    BROWSER_PID=""
fi

# Wait for browser to close, then stop the server
if [ -n "\$BROWSER_PID" ]; then
    wait \$BROWSER_PID 2>/dev/null
    kill \$APP_PID 2>/dev/null
fi
LAUNCHER
chmod +x "${MACOS}/_launcher"

cat > "${MACOS}/_install" << INSTALL
#!/bin/bash
set -e
BUNDLE_DIR="\$(cd "\$(dirname "\$0")/.." && pwd)"
RESOURCES="\${BUNDLE_DIR}/Resources/app"
DATA_DIR="${APP_DATA_DIR}"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ClipBuilder — First-Time Setup     ║"
echo "╚══════════════════════════════════════╝"
echo ""

mkdir -p "\$DATA_DIR" && cd "\$DATA_DIR"

echo "Copying app files..."
rsync -a "\$RESOURCES/src/" "\$DATA_DIR/src/"
rsync -a "\$RESOURCES/assets/" "\$DATA_DIR/assets/"
cp "\$RESOURCES/requirements.txt" "\$DATA_DIR/"
cp "\$RESOURCES/.env.example" "\$DATA_DIR/" 2>/dev/null || true
# App-wide runtime dirs. Per-profile Input/Output folders live under
# ~/Documents/ClipBuilder/<ProfileName>/ and are auto-created on save.
mkdir -p assets/music assets/videos data .cache/thumbnails
mkdir -p "\$HOME/Documents/ClipBuilder"

if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "\$(/opt/homebrew/bin/brew shellenv)"
else
    echo "Homebrew ✓"
fi

echo "Installing Python and FFmpeg..."
brew install python ffmpeg 2>/dev/null || true

if ! command -v node &>/dev/null; then
    echo "Installing Node.js..."
    brew install node 2>/dev/null || true
fi

# Install all three supported AI CLIs (user picks which to use on /settings).
# A fourth provider — MiniMax — is API-only and authenticates via
# MINIMAX_API_KEY in .env, so it has no install step here. Same for the
# transcription-only cloud providers (OpenAI Whisper + Gemini audio): they
# read OPENAI_API_KEY / GEMINI_API_KEY from .env on first use.
for entry in \\
    "claude|@anthropic-ai/claude-code|Claude Code" \\
    "gemini|@google/gemini-cli|Gemini CLI" \\
    "codex|@openai/codex|Codex CLI"; do
    IFS='|' read -r BIN PKG NAME <<< "\$entry"
    if ! command -v "\$BIN" &>/dev/null; then
        echo "Installing \$NAME..."
        npm install -g "\$PKG" 2>/dev/null || true
    fi
done

echo "Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Ensure yt-dlp is current. YouTube/TikTok/Instagram defenses change often
# and stale yt-dlp is the most common cause of "Import Video" failures.
pip install -q --upgrade yt-dlp 2>/dev/null || true

# Seed .env from .env.example so users have one place to paste keys for
# the API-only providers (MiniMax, OpenAI transcription, Gemini transcription).
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
fi

echo "Setting up auto-updates..."
if [ ! -d ".git" ]; then
    git init -q
    git remote add origin ${GH_REPO_HTTPS} 2>/dev/null || true
    git fetch -q origin 2>/dev/null || true
    git reset --hard origin/main 2>/dev/null || true
    git branch -M main 2>/dev/null || true
    git branch --set-upstream-to=origin/main main 2>/dev/null || true
fi

echo ""
echo "════════════════════════════════════════"
echo "  Installation complete!"
echo "  Launching ClipBuilder..."
echo "════════════════════════════════════════"
echo ""

open -a "ClipBuilder" 2>/dev/null || open "\${BUNDLE_DIR}/../.." 2>/dev/null

osascript -e 'tell application "Terminal" to close front window' 2>/dev/null &
INSTALL
chmod +x "${MACOS}/_install"

# Remove osacompile's default icon (legacy form) AND the modern asset
# catalog. macOS 12+ osacompile bundles the applet's default icon as
# AppIcon inside Assets.car, which silently outranks AppIcon.icns in
# Resources — so without nuking Assets.car the bundle would render the
# generic AppleScript applet icon instead of our ClipBuilder logo.
rm -f "${RESOURCES}/applet.icns" 2>/dev/null
rm -f "${RESOURCES}/Assets.car" 2>/dev/null

# Ad-hoc sign the app so macOS treats it as a valid bundle.
# Users still need to clear quarantine (xattr -cr) for internet downloads,
# but this prevents the "can't be opened" error once quarantine is cleared.
codesign -s - --force --deep "${APP}" 2>/dev/null || true

echo "App bundle ready ✓"

# ── Create DMG ──
echo "Creating DMG..."
ln -s /Applications "${STAGE_DIR}/Applications"

hdiutil create \
    -volname "$DMG_NAME" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

rm -rf "$STAGE_DIR"

echo ""
echo "DMG created: ${DMG_PATH}"
echo "Size: $(du -h "$DMG_PATH" | cut -f1)"

# ── Upload to GitHub ──
echo "Uploading to GitHub..."
if command -v gh &>/dev/null; then
    gh release delete latest --yes --repo "$GH_REPO" 2>/dev/null || true
    git tag -d latest 2>/dev/null || true
    git push origin :refs/tags/latest 2>/dev/null || true

    gh release create latest "$DMG_PATH" \
        --repo "$GH_REPO" \
        --title "ClipBuilder Installer" \
        --notes "Drag to Applications and launch. First run installs everything automatically." \
        --latest 2>/dev/null

    RELEASE_URL=$(gh release view latest --repo "$GH_REPO" --json assets --jq '.assets[0].url' 2>/dev/null)
    [ -z "$RELEASE_URL" ] && RELEASE_URL="https://github.com/${GH_REPO}/releases/latest"
    echo "Release: $RELEASE_URL"
else
    RELEASE_URL="https://github.com/${GH_REPO}/releases/latest"
fi

# ── Email ──
DOWNLOAD_LINK="$RELEASE_URL"
EMAIL_BODY="Hi!

Here's ClipBuilder — the AI-powered short-form video builder.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOWNLOAD: ${DOWNLOAD_LINK}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HOW TO INSTALL:
1. Download ClipBuilder.dmg
2. Open the DMG and drag ClipBuilder to Applications
3. Open Terminal (search \"Terminal\" in Spotlight) and paste this line:
   xattr -dr com.apple.quarantine /Applications/ClipBuilder.app; xattr -dr com.apple.provenance /Applications/ClipBuilder.app
   Press Enter. (One-time step — macOS blocks apps downloaded outside the App Store.)
4. Double-click ClipBuilder in Applications
5. A Terminal window will appear and install everything automatically (~5 min)
6. When it finishes, ClipBuilder launches on its own — you're ready to go!

HOW TO USE:
• On first launch, create or pick a brand profile from /settings
• Each profile gets its own input/output folders under ~/Documents/ClipBuilder/<ProfileName>/
• Drop source videos into <ProfileName>/Input
• Click Analyze to scan scenes
• Click AI Wizard to generate highlights
• Click Builder for manual editing

TIPS:
• Music: drop .mp3 into ~/ClipBuilder/assets/music
• Intro/outro: put in ~/ClipBuilder/assets/videos
• Click 'Check for Updates' in the app for latest features

Enjoy!"

echo "Opening Mail..."
osascript -e "
tell application \"Mail\"
    set newMsg to make new outgoing message with properties {subject:\"ClipBuilder — AI Short-Form Video Builder\", content:\"$(echo "$EMAIL_BODY" | sed 's/"/\\"/g')\", visible:true}
    activate
end tell
" 2>/dev/null

echo "Done!"
