"""features.py — Feature flags.

Single source of truth for opt-in features. Server reads these to conditionally
register routes / start background tasks; client JS reads `window.PG_FEATURES`
(injected by app.py) to conditionally render UI.

Toggle a flag and restart the app for the change to take effect.
"""

# Cloud Drive (Google Drive) integration. When False:
#   - /drive blueprint is NOT registered (settings page + APIs return 404).
#   - Background watcher does not auto-pull from Drive.
#   - Drive nav link, Pull-from-Drive button (Analyze), and
#     Upload-to-Drive button (Library) are hidden in the UI.
DRIVE_ENABLED = False
