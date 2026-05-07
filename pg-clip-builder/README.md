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

## Google Drive Integration

The `/drive` page lets you pull source videos from a Drive folder and push generated videos out for sharing.

### End-user setup

Once Drive is configured (see admin setup below), the entire end-user flow is:

1. Open `/drive`, click **Sign in with Google**, approve permissions.
2. The app auto-creates two folders in your Drive: `PeaceGrappler Inbox` and `PeaceGrappler Output`.
3. Drop new source videos into the inbox folder — then on `/analyze` click **Pull from Drive**.
4. On `/library`, the player has an **Upload to Drive** button that pushes generated videos into the output folder and gives you a shareable link.

### Admin setup (one-time, by whoever installs the app)

Google requires an OAuth client to be registered before any app can talk to a user's Drive. Do this once:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a project (or pick an existing one).
2. **APIs & Services → Library** → search **Google Drive API** → click **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**, click **Create**.
   - App name: `PeaceGrappler`. Fill in support email + developer contact.
   - **Save and continue** through Scopes (no changes needed).
   - On **Test users**, add your own Google email (and any other users who'll run the app — up to 100).
4. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Name: `PeaceGrappler Local`.
   - **Authorized redirect URIs**: add `http://localhost:5555/drive/oauth/callback` (and `:8080` if you sometimes use that port).
   - Click **Create**, then **Download JSON**.
5. Save the downloaded file as `src/drive_client.json` in your PeaceGrappler folder. (A template is at `src/drive_client.example.json` for reference.)
6. Restart the app.

That's it. From then on, anyone running this copy of the app just clicks **Sign in with Google** at `/drive`.

### Privacy & scope notes

- The app requests the broad `https://www.googleapis.com/auth/drive` scope so it can both list user-dropped files in the inbox folder and create the outbox folder. If you want a tighter scope, edit `src/drive.py`.
- `src/drive_client.json` and `data/drive_token.json` are gitignored — your OAuth client and per-user tokens never get committed.
- Generated videos uploaded to Drive are made `anyone with the link` readable by default so the share link works without requiring viewers to sign in. Tweak `make_shareable=True` in `src/drive.py:upload_file` to change this.
