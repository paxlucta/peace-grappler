#!/bin/bash
set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║     PeaceGrappler Installer          ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo "Step 1/4: Installing Homebrew (macOS package manager)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
else
    echo "Step 1/4: Homebrew already installed ✓"
fi

# Check/install dependencies
echo "Step 2/4: Installing Python and FFmpeg..."
brew install python ffmpeg 2>/dev/null || true

# Install Node.js and Claude CLI for AI features
if ! command -v node &>/dev/null; then
    echo "Step 3/4: Installing Node.js (needed for AI features)..."
    brew install node 2>/dev/null || true
fi
if ! command -v claude &>/dev/null; then
    echo "Step 3/4: Installing Claude CLI (AI engine)..."
    npm install -g @anthropic-ai/claude-code 2>/dev/null || true
    if command -v claude &>/dev/null; then
        echo "Claude CLI installed ✓"
        echo ""
        echo "NOTE: You need to sign in to Claude on first use."
        echo "Run 'claude' in Terminal to set up your account."
        echo ""
    else
        echo ""
        echo "⚠  Claude CLI could not be installed automatically."
        echo "   AI features (Analyze, AI Wizard) won't work without it."
        echo "   To install manually, run in Terminal:"
        echo "     npm install -g @anthropic-ai/claude-code"
        echo ""
    fi
else
    echo "Step 3/4: Claude CLI already installed ✓"
fi

# Create virtual environment and install Python dependencies
echo "Step 4/4: Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Create directories
mkdir -p videos assets/music assets/videos output data .cache/thumbnails

# Set up git repo for auto-updates
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

echo ""
echo "════════════════════════════════════════"
echo "  Installation complete!"
echo ""
echo "  To launch: double-click PeaceGrappler.command"
echo "  To add videos: drop .mp4 files into the 'videos' folder"
echo "════════════════════════════════════════"
echo ""
