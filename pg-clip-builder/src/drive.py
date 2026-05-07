"""drive.py — Google Drive integration (OAuth + list/download/upload)."""

import json
import re
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
SRC_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"

# Bundled OAuth client config (developer-side, one-time setup — see README).
CLIENT_CONFIG_PATH = SRC_DIR / "drive_client.json"
CLIENT_EXAMPLE_PATH = SRC_DIR / "drive_client.example.json"

# Per-user state (gitignored).
TOKEN_PATH = DATA_DIR / "drive_token.json"
CONFIG_PATH = DATA_DIR / "drive_config.json"

# Folder names auto-created on first connect.
INBOX_FOLDER_NAME = "PeaceGrappler Inbox"
OUTBOX_FOLDER_NAME = "PeaceGrappler Output"

SCOPES = ["https://www.googleapis.com/auth/drive"]


# ── Bundled client config ────────────────────────────────────────────────────

def _load_client_config():
    """Return the parsed bundled client_config dict, or None if missing/placeholder."""
    if not CLIENT_CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CLIENT_CONFIG_PATH.read_text())
    except Exception:
        return None
    block = data.get("web") or data.get("installed")
    if not block:
        return None
    cid = block.get("client_id", "")
    csec = block.get("client_secret", "")
    if not cid or not csec or "PASTE_" in cid or "PASTE_" in csec:
        return None
    return data


def is_app_configured():
    """True if the developer has filled in src/drive_client.json with real values."""
    return _load_client_config() is not None


# ── Per-user config (folder IDs) ─────────────────────────────────────────────

def _load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_config():
    cfg = _load_config()
    return {
        "inbox_folder_id": cfg.get("inbox_folder_id", ""),
        "outbox_folder_id": cfg.get("outbox_folder_id", ""),
        "inbox_folder_link": _folder_link(cfg.get("inbox_folder_id", "")),
        "outbox_folder_link": _folder_link(cfg.get("outbox_folder_id", "")),
        "app_configured": is_app_configured(),
        "has_token": TOKEN_PATH.exists(),
    }


def _folder_link(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


def set_folders(inbox=None, outbox=None):
    cfg = _load_config()
    if inbox is not None:
        cfg["inbox_folder_id"] = _extract_folder_id(inbox)
    if outbox is not None:
        cfg["outbox_folder_id"] = _extract_folder_id(outbox)
    _save_config(cfg)


def _extract_folder_id(value):
    """Accept a raw folder ID or a Drive URL — extract the ID."""
    value = (value or "").strip()
    if not value:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    return value


def disconnect():
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


# ── OAuth ────────────────────────────────────────────────────────────────────

def _build_flow(redirect_uri):
    from google_auth_oauthlib.flow import Flow
    if not is_app_configured():
        raise RuntimeError(
            "Drive integration not configured by app admin. "
            f"See {CLIENT_EXAMPLE_PATH.name} and the README."
        )
    return Flow.from_client_config(
        _load_client_config(), scopes=SCOPES, redirect_uri=redirect_uri,
    )


def auth_start(redirect_uri):
    """Return (auth_url, state) to send the user to Google's consent screen."""
    flow = _build_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    return auth_url, state


def auth_finish(redirect_uri, authorization_response_url):
    """Exchange the OAuth callback URL for credentials and persist them."""
    flow = _build_flow(redirect_uri)
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())


def _get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def _service():
    from googleapiclient.discovery import build
    creds = _get_credentials()
    if not creds:
        raise RuntimeError("Not authenticated with Google Drive")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Folder helpers ───────────────────────────────────────────────────────────

def _find_folder_by_name(svc, name):
    """Find a non-trashed folder owned by the user with the given name."""
    q = (
        f"name = '{name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and 'me' in owners "
        f"and trashed = false"
    )
    resp = svc.files().list(
        q=q, fields="files(id, name)", pageSize=10,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(svc, name):
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    f = svc.files().create(body=body, fields="id").execute()
    return f["id"]


def find_or_create_default_folders():
    """Idempotent: ensure both PeaceGrappler folders exist; save IDs to config.
    Returns the updated config dict."""
    svc = _service()
    cfg = _load_config()

    # Inbox
    if not cfg.get("inbox_folder_id"):
        fid = _find_folder_by_name(svc, INBOX_FOLDER_NAME)
        if not fid:
            fid = _create_folder(svc, INBOX_FOLDER_NAME)
        cfg["inbox_folder_id"] = fid

    # Outbox
    if not cfg.get("outbox_folder_id"):
        fid = _find_folder_by_name(svc, OUTBOX_FOLDER_NAME)
        if not fid:
            fid = _create_folder(svc, OUTBOX_FOLDER_NAME)
        cfg["outbox_folder_id"] = fid

    _save_config(cfg)
    return cfg


# ── Drive ops ────────────────────────────────────────────────────────────────

def list_inbox_videos():
    """Return list of video files in the configured inbox folder."""
    cfg = _load_config()
    folder_id = cfg.get("inbox_folder_id")
    if not folder_id:
        raise RuntimeError("No inbox folder configured")
    svc = _service()
    mime_filter = "mimeType contains 'video/'"
    q = f"'{folder_id}' in parents and trashed = false and ({mime_filter})"
    items, page_token = [], None
    while True:
        resp = svc.files().list(
            q=q,
            fields="nextPageToken, files(id, name, size, mimeType, modifiedTime)",
            pageSize=200, pageToken=page_token,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def download_file(file_id, dest_path):
    """Stream a file from Drive to dest_path. Returns bytes downloaded."""
    from googleapiclient.http import MediaIoBaseDownload
    svc = _service()
    req = svc.files().get_media(fileId=file_id)
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    return dest_path.stat().st_size


def upload_file(local_path, name=None, make_shareable=True):
    """Upload local_path to the configured outbox folder. Returns
    {id, webViewLink}."""
    from googleapiclient.http import MediaFileUpload
    cfg = _load_config()
    folder_id = cfg.get("outbox_folder_id")
    if not folder_id:
        raise RuntimeError("No outbox folder configured")

    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(str(local_path))

    svc = _service()
    metadata = {"name": name or local_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=True,
                            chunksize=8 * 1024 * 1024)
    file = svc.files().create(
        body=metadata, media_body=media,
        fields="id, webViewLink, webContentLink",
    ).execute()

    if make_shareable:
        try:
            svc.permissions().create(
                fileId=file["id"],
                body={"role": "reader", "type": "anyone"},
            ).execute()
        except Exception:
            pass

    return {
        "id": file["id"],
        "webViewLink": file.get("webViewLink", ""),
        "webContentLink": file.get("webContentLink", ""),
    }
