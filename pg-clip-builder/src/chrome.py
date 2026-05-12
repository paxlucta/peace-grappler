"""chrome.py — Shared header/nav for every page.

Every page used to copy-paste its own ``<header>...</header>`` block, which
meant the nav drifted: Settings and Drive links were missing on every page,
and tweaking a label meant editing seven files. This module is the single
source of truth.

Usage from a route::

    from chrome import inject_chrome
    return inject_chrome(PAGE_HTML, active="library")

The page's HTML constant must contain the placeholder ``<!-- pg-chrome -->``
where the header should be inserted. Pages are responsible for the CSS that
styles ``header``/``nav``/``a`` — that part is intentionally still duplicated
because each page tweaks colors slightly.
"""

# Each top-level nav entry: (key, href, label, sub_tabs_or_None).
# When sub_tabs is provided, the entry is highlighted whenever *active* is
# either the parent key OR any sub-tab key, and a tab strip is rendered
# under the header so the user can jump between the sub-pages.
NAV_LINKS = [
    ("wizard",   "/wizard",   "AI Builder", None),
    ("builder",  "/builder",  "Builder",   None),
    ("library",  "/library",  "Generated Videos", None),
    ("analyze",  "/analyze",  "Input Videos", [
        ("analyze", "/analyze", "Files"),
        ("rate",    "/rate",    "Scenes"),
    ]),
    ("settings", "/settings", "Settings", None),
    ("drive",    "/drive",    "Drive",    None),
]


def _find_active_group(active):
    """Return ``(parent_key, sub_tabs)`` if *active* belongs to a group with
    sub-tabs, else ``(active, None)``."""
    for key, _href, _label, subs in NAV_LINKS:
        if subs and any(sub[0] == active for sub in subs):
            return key, subs
        if key == active and subs:
            # Default the child highlight to the parent's own key when the
            # caller passes only the parent (e.g. ``active="analyze"``).
            return key, subs
    return active, None


def header_html(active: str = "") -> str:
    """Return the ``<header>...</header>`` block. *active* is the key from
    NAV_LINKS that should render with class="active" (red underline)."""
    parent_active, sub_tabs = _find_active_group(active)

    parts = ["<header>", "<h1>Clip<span>Builder</span></h1>", "<nav>"]
    for key, href, label, _subs in NAV_LINKS:
        cls = ' class="active"' if key == parent_active else ""
        # Drive link is hidden on pages where the feature flag is off; the
        # tiny inline script at the bottom of the header takes care of it
        # so server-side rendering doesn't have to know feature state.
        extra = ' data-pg-drive="1"' if key == "drive" else ""
        parts.append(f'<a href="{href}"{cls}{extra}>{label}</a>')
    parts.append("</nav>")
    parts.append("</header>")

    if sub_tabs:
        parts.append('<div class="pg-subnav">')
        for sub_key, sub_href, sub_label in sub_tabs:
            cls = ' class="active"' if sub_key == active else ""
            parts.append(f'<a href="{sub_href}"{cls}>{sub_label}</a>')
        parts.append("</div>")
        parts.append(
            "<style>"
            # Strip is transparent on purpose so it sits flush against the
            # page chrome — only the active pill draws a background. Match
            # the size/look used on the rating page (correct reference).
            ".pg-subnav{display:flex;gap:10px;padding:16px 20px 14px;"
            "background:transparent;flex-shrink:0;align-items:center}"
            ".pg-subnav a{color:#888;text-decoration:none;font-size:14px;"
            "font-weight:600;padding:8px 18px;border-radius:8px;"
            "transition:background .15s,color .15s;"
            "display:inline-flex;align-items:center;line-height:1}"
            ".pg-subnav a:hover{color:#fff;background:#1a1a22}"
            ".pg-subnav a.active{color:#fff;background:#e53935}"
            "</style>"
        )

    # Hide the Drive link unless window.PG_FEATURES.drive is true. Doing
    # this inline avoids a flash-of-wrong-state when the page first paints.
    parts.append(
        "<script>(function(){"
        "var on=window.PG_FEATURES&&window.PG_FEATURES.drive;"
        "if(!on){var els=document.querySelectorAll('[data-pg-drive]');"
        "for(var i=0;i<els.length;i++){els[i].style.display='none';}}"
        "})();</script>"
    )
    return "".join(parts)


CHROME_PLACEHOLDER = "<!-- pg-chrome -->"


def inject_chrome(html: str, active: str = "") -> str:
    """Replace the ``<!-- pg-chrome -->`` placeholder in *html* with the
    rendered shared header. If the placeholder isn't present this is a
    no-op so we don't accidentally double-render."""
    if CHROME_PLACEHOLDER not in html:
        return html
    return html.replace(CHROME_PLACEHOLDER, header_html(active))
