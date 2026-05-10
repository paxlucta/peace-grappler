#!/bin/bash
set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       ClipBuilder Installer          ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Step 1: Homebrew ────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    echo "Step 1/5: Installing Homebrew (macOS package manager)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
else
    echo "Step 1/5: Homebrew already installed ✓"
fi

# ── Step 2: Python + FFmpeg ─────────────────────────────────────────────────
echo "Step 2/5: Installing Python and FFmpeg..."
brew install python ffmpeg 2>/dev/null || true

# ── Step 3: Node.js (required by all three AI CLIs) ─────────────────────────
if ! command -v node &>/dev/null; then
    echo "Step 3/5: Installing Node.js (required by AI CLIs)..."
    brew install node 2>/dev/null || true
else
    echo "Step 3/5: Node.js already installed ✓"
fi

# ── Step 4: AI CLIs (Claude, Gemini, Codex) ─────────────────────────────────
echo "Step 4/5: Installing AI CLI tools..."

# Each entry: "<bin>|<npm-package>|<friendly-name>|<auth-hint>"
AI_CLIS=(
    "claude|@anthropic-ai/claude-code|Claude Code|run 'claude' in Terminal to sign in"
    "gemini|@google/gemini-cli|Gemini CLI|run 'gemini' to sign in, or set GEMINI_API_KEY in .env"
    "codex|@openai/codex|Codex CLI|run 'codex login' in Terminal to sign in"
)

INSTALLED=()
SKIPPED=()
FAILED=()

for entry in "${AI_CLIS[@]}"; do
    IFS='|' read -r BIN PKG NAME HINT <<< "$entry"
    if command -v "$BIN" &>/dev/null; then
        echo "  • $NAME already installed ✓"
        SKIPPED+=("$NAME")
        continue
    fi
    echo "  • Installing $NAME ($PKG)..."
    if npm install -g "$PKG" >/dev/null 2>&1; then
        if command -v "$BIN" &>/dev/null; then
            echo "    $NAME installed ✓"
            INSTALLED+=("$NAME — $HINT")
        else
            echo "    ⚠  $NAME install reported success but '$BIN' not on PATH"
            FAILED+=("$NAME ($PKG)")
        fi
    else
        echo "    ⚠  Could not install $NAME automatically."
        FAILED+=("$NAME ($PKG)")
    fi
done

# ── Step 5: Python venv + deps + folders ────────────────────────────────────
echo "Step 5/5: Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Create runtime directories
mkdir -p videos assets/music assets/videos output data .cache/thumbnails

# ── Auto-update setup ───────────────────────────────────────────────────────
echo "Setting up auto-updates..."
PARENT_DIR="$(cd .. && pwd)"
if [ ! -d "$PARENT_DIR/.git" ]; then
    cd "$PARENT_DIR"
    git init -q
    git remote add origin https://github.com/paxlucta/peace-grappler.git 2>/dev/null || \
        git remote set-url origin https://github.com/paxlucta/peace-grappler.git
    git fetch -q origin 2>/dev/null || true
    git checkout -b main 2>/dev/null || true
    git branch --set-upstream-to=origin/main main 2>/dev/null || true
    cd "$(dirname "$0")"
    echo "Auto-updates configured ✓"
else
    echo "Git repo already configured ✓"
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════"

if [ ${#INSTALLED[@]} -gt 0 ]; then
    echo ""
    echo "Newly installed AI CLIs (sign-in needed before first use):"
    for line in "${INSTALLED[@]}"; do
        echo "  • $line"
    done
fi

if [ ${#FAILED[@]} -gt 0 ]; then
    echo ""
    echo "⚠  These AI CLIs could not be installed automatically:"
    for line in "${FAILED[@]}"; do
        echo "  • $line"
    done
    echo ""
    echo "  To install manually, run any of:"
    for entry in "${AI_CLIS[@]}"; do
        IFS='|' read -r _ PKG _ _ <<< "$entry"
        echo "    npm install -g $PKG"
    done
fi

echo ""
echo "  To launch: double-click PeaceGrappler.command"
echo "  Pick which AI to use on the /settings page after launching."
echo "  Drop .mp4 source videos into the 'videos' folder."
echo ""
