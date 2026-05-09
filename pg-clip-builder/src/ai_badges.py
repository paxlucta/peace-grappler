"""ai_badges.py — Inline SVG attribution marks for each AI provider.

Used by the UI to show *which* AI produced a piece of content. Marks are
intentionally simple, recognizable shapes (not the official trademarked
logos) — colored with each vendor's brand palette so they're identifiable at
a glance.
"""

import json


# Each entry: a small (16×16-ish viewBox) SVG with paths colored explicitly,
# safe to embed inline in HTML. Background-aware (no fixed white bg).
BADGES = {
    "claude": {
        "label": "Claude (Anthropic)",
        "color": "#cc785c",
        # Stylized "A" / asterisk-ish mark — Anthropic-orange.
        "svg": (
            '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" '
            'aria-label="Claude">'
            '<path fill="#cc785c" d="M4.5 19 9 5h2.6L7.1 19H4.5zm6.9 0L15.9 5h2.6l-4.5 14h-2.6z"/>'
            '<path fill="#cc785c" d="M8.6 13.4h6.7v2.2H8.6z"/>'
            '</svg>'
        ),
    },
    "codex": {
        "label": "Codex (OpenAI)",
        "color": "#10a37f",
        # OpenAI-green concentric rings as a recognizable token.
        "svg": (
            '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" '
            'aria-label="Codex">'
            '<circle cx="12" cy="12" r="9" fill="none" stroke="#10a37f" stroke-width="2"/>'
            '<circle cx="12" cy="12" r="3.5" fill="#10a37f"/>'
            '</svg>'
        ),
    },
    "gemini": {
        "label": "Gemini (Google)",
        "color": "#4285F4",
        # 4-point sparkle in Gemini blue.
        "svg": (
            '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" '
            'aria-label="Gemini">'
            '<defs><linearGradient id="g_grad" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0%" stop-color="#4285F4"/>'
            '<stop offset="55%" stop-color="#9B72CB"/>'
            '<stop offset="100%" stop-color="#D96570"/>'
            '</linearGradient></defs>'
            '<path fill="url(#g_grad)" d="M12 2c.4 5 2 6.6 7 7-5 .4-6.6 2-7 7-.4-5-2-6.6-7-7 5-.4 6.6-2 7-7z"/>'
            '</svg>'
        ),
    },
}


def badge_svg(provider):
    """Return the inline SVG string for a provider, or empty if unknown."""
    if not provider:
        return ""
    return (BADGES.get(provider) or {}).get("svg", "")


def badge_label(provider):
    if not provider:
        return ""
    return (BADGES.get(provider) or {}).get("label", provider)


def badges_js_blob():
    """Return a small JSON blob safe to inline as `window.PG_AI_BADGES`."""
    payload = {k: {"svg": v["svg"], "label": v["label"], "color": v["color"]}
               for k, v in BADGES.items()}
    return json.dumps(payload)
