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

# API-key-only providers (no CLI to install). Listed here so the summary at
# the end of this script can remind the user which keys unlock which
# providers. Auth happens via env vars loaded from .env at app startup.
echo "  • MiniMax  (AI tasks, API only)      — set MINIMAX_API_KEY in .env to enable"
echo "  • OpenAI   (transcription, API only) — set OPENAI_API_KEY in .env to enable"
echo "  • Gemini   (transcription, API only) — set GEMINI_API_KEY in .env (or use Gemini CLI)"

# ── Step 5: Python venv + deps + folders ────────────────────────────────────
echo "Step 5/5: Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# yt-dlp powers every "Import Video" path (YouTube, TikTok, Instagram, and
# generic "From URL"). YouTube's anti-bot defenses change frequently, so we
# always upgrade to the latest release here even when the venv exists —
# stale yt-dlp is the #1 reason imports start failing months after install.
echo "  • Ensuring yt-dlp is up to date for YouTube / TikTok / Instagram imports…"
if pip install -q --upgrade yt-dlp; then
    YT_DLP_VERSION="$(python -c 'import yt_dlp,sys;print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"
    echo "    yt-dlp $YT_DLP_VERSION ✓"
else
    echo "    ⚠  yt-dlp upgrade failed — YouTube imports may not work until you run:"
    echo "        source .venv/bin/activate && pip install -U yt-dlp"
fi

# Create runtime directories. App-wide state (data/, .cache/) still lives
# under the repo root; per-profile source/output folders live under
# ~/Documents/ClipBuilder/<ProfileName>/{Input,Output} and are auto-created
# when a profile is saved.
mkdir -p assets/music assets/videos data .cache/thumbnails
mkdir -p "$HOME/Documents/ClipBuilder"

# Seed .env from .env.example on first install so the user has a single
# place to paste API keys (MiniMax / OpenAI / Gemini). Never overwrite an
# existing .env — that's where their keys live.
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "  • Created .env from .env.example — paste API keys there to enable"
    echo "    MiniMax, OpenAI transcription, and Gemini transcription."
fi

# ── Auto-update setup ───────────────────────────────────────────────────────
echo "Setting up auto-updates..."
if [ ! -d ".git" ]; then
    git init -q
    git remote add origin https://github.com/abghandour/clip-builder.git 2>/dev/null || \
        git remote set-url origin https://github.com/abghandour/clip-builder.git
    git fetch -q origin 2>/dev/null || true
    git checkout -b main 2>/dev/null || true
    git branch --set-upstream-to=origin/main main 2>/dev/null || true
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
echo "API-key providers (optional — edit .env to enable):"
echo "  • MiniMax (AI)             — MINIMAX_API_KEY"
echo "  • OpenAI Whisper (transcribe) — OPENAI_API_KEY"
echo "  • Gemini audio (transcribe)   — GEMINI_API_KEY"
echo ""
echo "  To launch: double-click ClipBuilder.command"
echo "  Pick which AI to use on the /settings page after launching."
echo "  Drop .mp4 source videos into the active profile's Input folder under"
echo "  ~/Documents/ClipBuilder/<ProfileName>/Input/"
echo ""
