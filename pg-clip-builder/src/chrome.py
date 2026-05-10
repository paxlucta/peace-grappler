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

# Order here is the order users see in every page header. Settings + Drive
# were missing before; Drive is hidden client-side when the feature flag
# is off (window.PG_FEATURES.drive), matching the existing pattern for
# Drive-gated buttons elsewhere.
NAV_LINKS = [
    ("wizard",   "/wizard",   "AI Wizard"),
    ("builder",  "/builder",  "Builder"),
    ("library",  "/library",  "Library"),
    ("rate",     "/rate",     "Scenes"),
    ("analyze",  "/analyze",  "Analyze"),
    ("settings", "/settings", "Settings"),
    ("drive",    "/drive",    "Drive"),
]


def header_html(active: str = "") -> str:
    """Return the ``<header>...</header>`` block. *active* is the key from
    NAV_LINKS that should render with class="active" (red underline)."""
    parts = ["<header>", "<h1>Clip<span>Builder</span></h1>", "<nav>"]
    for key, href, label in NAV_LINKS:
        cls = ' class="active"' if key == active else ""
        # Drive link is hidden on pages where the feature flag is off; the
        # tiny inline script at the bottom of the header takes care of it
        # so server-side rendering doesn't have to know feature state.
        extra = ' data-pg-drive="1"' if key == "drive" else ""
        parts.append(f'<a href="{href}"{cls}{extra}>{label}</a>')
    parts.append("</nav>")
    parts.append("</header>")
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
