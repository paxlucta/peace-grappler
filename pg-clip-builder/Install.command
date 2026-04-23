#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "=== PeaceGrappler Installer ==="

# Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

# Check/install dependencies
echo "Installing dependencies..."
brew install python ffmpeg 2>/dev/null || true

# Check Claude CLI
if ! command -v claude &>/dev/null; then
    echo ""
    echo "Warning: Claude CLI not found. Install it with:"
    echo "  npm install -g @anthropic-ai/claude-code"
    echo "  (requires Node.js: brew install node)"
    echo ""
fi

# Create virtual environment
echo "Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Create directories
mkdir -p videos assets/music output/generated data .cache/thumbnails

echo ""
echo "Installation complete!"
echo "  Double-click PeaceGrappler.command to launch."
