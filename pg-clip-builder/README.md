# PeaceGrappler Clip Builder

MMA highlight reel builder with AI-powered video analysis, scene tagging, and autonomous Instagram Reels generation.

## Requirements

- **macOS** (uses Homebrew, AppleScript for email)
- **Python 3.9+**
- **FFmpeg** (installed via Homebrew)
- **Claude CLI** (optional, required for AI features: analysis, wizard)

## Quick Start

1. **Install dependencies** — double-click `Install.command` or run:
   ```bash
   brew install python ffmpeg
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   mkdir -p videos assets/music assets/videos output data
   ```

2. **Install Claude CLI** (for AI features):
   ```bash
   brew install node
   npm install -g @anthropic-ai/claude-code
   ```

3. **Launch** — double-click `PeaceGrappler.command` or run:
   ```bash
   source .venv/bin/activate
   python src/app.py
   ```
   Opens at [http://localhost:5555](http://localhost:5555)

## Pages

| Page | Path | Description |
|------|------|-------------|
| **Builder** | `/builder` | Drag-and-drop timeline editor — assemble clips, add music, transitions, and generate videos |
| **Analyze** | `/analyze` | Scan and AI-tag video scenes using Claude (activity, setting, camera, energy tags) |
| **Generate** | `/generate` | Auto-generate montages by selecting tags, duration, and music |
| **Library** | `/library` | Browse and play all generated videos, filter by tags, sort by date/duration |
| **AI Wizard** | `/wizard` | Autonomous Instagram Reels generator — AI researches best practices and makes all creative decisions |

## Directory Structure

```
pg-clip-builder/
  src/            # Flask application source
  videos/         # Place source video files here
  assets/music/   # Background music files (mp3, m4a, wav)
  assets/videos/  # Intro/outro videos (intro.mp4, outro.mp4)
  output/         # Generated videos (organized by date)
  data/           # SQLite database (auto-created)
```

## Adding Content

- **Source videos**: Drop `.mp4`, `.mov`, `.avi`, `.mkv`, or `.webm` files into `videos/`
- **Music**: Drop audio files into `assets/music/`
- **Intro/Outro**: Place `intro.mp4` and/or `outro.mp4` in `assets/videos/` — they'll be automatically prepended/appended to generated videos

## Options

```bash
python src/app.py --port 8080      # Custom port (default: 5555)
python src/app.py --no-reload      # Disable auto-reload on file changes
```
