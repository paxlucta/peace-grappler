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

Theming: when the active theme isn't ``default``, ``inject_chrome`` also
inlines a global stylesheet overlay (e.g. art_deco) and a Google-fonts link
right next to the header. Pages don't need to know about themes — their
inline CSS is overridden by the higher-specificity theme rules below.
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


def _current_theme():
    """Read the active UI theme from app_config. Defaults to ``'default'``
    when anything goes wrong (e.g. config not initialised during a test)."""
    try:
        import app_config
        return (app_config.get_config().get("theme") or "default").lower()
    except Exception:
        return "default"


def header_html(active: str = "", theme: str = "default") -> str:
    """Return the ``<header>...</header>`` block. *active* is the key from
    NAV_LINKS that should render with class="active" (red underline).
    *theme* swaps the brand title for the logo image when art_deco is on."""
    parent_active, sub_tabs = _find_active_group(active)

    if theme == "art_deco":
        # Logo replaces the text title under art_deco — the logo IS the
        # brand mark in this theme. h1 kept so screen readers still get a
        # title; image alt covers visual users.
        brand = ('<h1 class="pg-brand-art">'
                 '<img src="/api/logo" alt="ClipBuilder">'
                 '</h1>')
    else:
        brand = "<h1>Clip<span>Builder</span></h1>"

    parts = ["<header>", brand, "<nav>"]
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
            # Underline-style tab strip — matches the Profile/General tabs
            # on /settings. The whole strip sits on a shared baseline so
            # the active tab's red underline reads as a real tab control,
            # not just a pill button. Container is transparent; only the
            # baseline border + active-tab underline are visible.
            ".pg-subnav{display:flex;gap:4px;padding:14px 20px 0;"
            "background:transparent;flex-shrink:0;align-items:flex-end;"
            "border-bottom:1px solid #1e1e2a;margin-bottom:18px}"
            ".pg-subnav a{color:#888;text-decoration:none;font-size:13px;"
            "font-weight:600;padding:10px 18px;letter-spacing:0.4px;"
            "border:none;border-bottom:2px solid transparent;"
            "margin-bottom:-1px;background:transparent;"
            "transition:color .15s,border-color .15s;"
            "display:inline-flex;align-items:center;line-height:1}"
            ".pg-subnav a:hover{color:#fff}"
            ".pg-subnav a.active{color:#fff;border-bottom-color:#e53935}"
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


# ── Art Deco theme overlay ─────────────────────────────────────────────────
#
# Drops in as a single <style> block right after each page's own inline
# stylesheet, so its element-level rules (with !important on color/
# background/border properties) override the page-specific defaults. The
# goal is a unified gold-on-black look inspired by the ClipBuilder logo
# (Metropolis-era sunburst typography, Poiret One display font, brass
# accents) — not a full redesign of every component.
ART_DECO_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poiret+One&family=Cinzel:wght@500;700&display=swap" rel="stylesheet">
<style id="pg-theme-art-deco">
/* Palette derived from the logo: deep umber background, brass/gold borders
   and accents, champagne text. Variables are useful for tweaks even though
   most page CSS doesn't read them. */
:root{
  --ad-bg:        #0e0a05;
  --ad-bg-soft:   #15100a;
  --ad-panel:     #1a140c;
  --ad-panel-2:   #221912;
  --ad-line:      #3a2e1a;
  --ad-line-bright:#5a4625;
  --ad-text:      #ead8a6;
  --ad-text-soft: #b59866;
  --ad-muted:     #8a7042;
  --ad-gold:      #c8a052;
  --ad-gold-hi:   #e6c074;
  --ad-bronze:    #a07832;
}

html, body{
  background:var(--ad-bg) !important;
  color:var(--ad-text) !important;
  font-family:'Cinzel','Cormorant Garamond',Georgia,serif !important;
}
body{
  background-image:
    radial-gradient(circle at 50% -10%, rgba(200,160,82,.06), transparent 60%),
    repeating-linear-gradient(0deg, transparent 0 3px, rgba(0,0,0,.18) 3px 4px) !important;
}

/* Header — the brand bar. Thin gold rule, black background, no red. */
header{
  background:linear-gradient(180deg, #181009, #0c0805) !important;
  border-bottom:1px solid var(--ad-gold) !important;
  box-shadow:0 1px 0 #2a1d0c, 0 4px 14px rgba(0,0,0,.5) !important;
}
header h1, header h1 span{
  font-family:'Poiret One','Limelight',serif !important;
  font-weight:400 !important;
  color:var(--ad-gold-hi) !important;
  letter-spacing:.18em !important;
  text-transform:uppercase !important;
}
header h1 span{ color:var(--ad-gold) !important; }
header h1.pg-brand-art{display:flex;align-items:center;gap:10px;height:36px}
header h1.pg-brand-art img{
  height:36px;width:36px;border-radius:6px;
  box-shadow:0 0 0 1px var(--ad-gold), 0 2px 10px rgba(200,160,82,.25);
}

nav a{
  font-family:'Cinzel',serif !important;
  text-transform:uppercase !important;
  letter-spacing:.18em !important;
  font-size:11px !important;
  color:var(--ad-text-soft) !important;
  border:1px solid var(--ad-line) !important;
  border-radius:0 !important;
  padding:5px 12px !important;
  background:transparent !important;
}
nav a:hover{ color:var(--ad-gold-hi) !important; border-color:var(--ad-gold) !important; }
nav a.active{
  color:var(--ad-bg) !important;
  background:linear-gradient(180deg, var(--ad-gold-hi), var(--ad-bronze)) !important;
  border-color:var(--ad-gold-hi) !important;
}

/* Subnav (sub-tabs strip) */
.pg-subnav{ border-bottom-color:var(--ad-line) !important; }
.pg-subnav a{
  font-family:'Cinzel',serif !important;
  letter-spacing:.16em !important;
  text-transform:uppercase !important;
  color:var(--ad-text-soft) !important;
}
.pg-subnav a:hover{ color:var(--ad-gold-hi) !important; }
.pg-subnav a.active{
  color:var(--ad-gold-hi) !important;
  border-bottom-color:var(--ad-gold) !important;
}

/* Buttons. Sweep across most patterns used throughout the app:
   raw <button>, .save, .go, .pd-btn, .bulk-btn, .act-btn, .folder-btn,
   .prov-pill, .folder-act-btn, etc. */
button, input[type=button], input[type=submit],
.pd-btn, .bulk-btn, .folder-btn, .folder-act-btn, .prov-pill,
.save, .vote-btn, .ra-tx, .card-del, .ff-actions button,
.tx-lang-toggle button, .vtx-lang-toggle button,
.tag-chip, .ctx-menu div, .aopts .aopts-actions button{
  font-family:'Cinzel',serif !important;
  text-transform:uppercase !important;
  letter-spacing:.12em !important;
  background:linear-gradient(180deg, #1f1610, #120c08) !important;
  color:var(--ad-text) !important;
  border:1px solid var(--ad-line-bright) !important;
  border-radius:2px !important;
  box-shadow:inset 0 1px 0 rgba(255,220,170,.08) !important;
}
button:hover, .pd-btn:hover, .bulk-btn:hover, .folder-btn:hover,
.prov-pill:hover, .save:hover, .ra-tx:hover, .tag-chip:hover,
.tx-lang-toggle button:hover, .vtx-lang-toggle button:hover{
  border-color:var(--ad-gold) !important;
  color:var(--ad-gold-hi) !important;
}
button:disabled, .pd-btn:disabled, .bulk-btn:disabled{
  opacity:.4 !important; color:var(--ad-muted) !important;
}

/* Primary / "go" buttons are gold filled */
.go, button.go, .save, .pd-btn.ig, .bulk-btn.danger, .vtx-prev, .vtx-add,
.act-btn.danger:hover, .pg-subnav a.active{
  background:linear-gradient(180deg, var(--ad-gold-hi), var(--ad-bronze)) !important;
  color:#1a120a !important;
  border-color:var(--ad-gold-hi) !important;
  text-shadow:0 1px 0 rgba(255,235,200,.4);
}
.go:hover, button.go:hover, .save:hover{ filter:brightness(1.08); }

/* Form inputs */
input[type=text], input[type=search], input[type=number],
input[type=email], input[type=url], input[type=password],
select, textarea, .search-input{
  background:#0a0703 !important;
  color:var(--ad-text) !important;
  border:1px solid var(--ad-line) !important;
  border-radius:2px !important;
  font-family:'Cinzel',serif !important;
}
input:focus, select:focus, textarea:focus, .search-input:focus{
  border-color:var(--ad-gold) !important;
  outline:none !important;
  box-shadow:0 0 0 1px var(--ad-gold-hi) inset !important;
}

/* Cards, panels, popovers */
.card, .clip-card, .video-card, .scene-card, .tx-modal, .vtx-modal,
.aopts, .ff-pop, .share-pop, .folder-dropdown, .ctx-menu, .modal,
.player-detail, .pd-section{
  background:var(--ad-panel) !important;
  border:1px solid var(--ad-line) !important;
  border-radius:3px !important;
  box-shadow:0 0 0 1px rgba(200,160,82,.06), 0 12px 36px rgba(0,0,0,.6) !important;
}
.tx-head, .vtx-head, .tx-toolbar, .vtx-toolbar{
  background:linear-gradient(180deg, #1d150d, #14100a) !important;
  border-bottom:1px solid var(--ad-line-bright) !important;
}
.vtx-help{ background:#11100a !important; color:var(--ad-text-soft) !important; }

/* Section titles and headings */
.section-title, h1, h2, h3, h4{
  font-family:'Poiret One',serif !important;
  color:var(--ad-gold-hi) !important;
  letter-spacing:.14em !important;
  text-transform:uppercase !important;
}
.section-title{
  border-bottom:1px solid var(--ad-line) !important;
}

/* Status badges / accent text — repaint red accents to gold. */
.muted, .video-count, .scene-count, .date, .pd-meta,
.tx-seg-time, .vtx-seg-time, .ff-count, .fi-count{
  color:var(--ad-muted) !important;
}

/* Marks (search highlights) */
mark, .tx-seg-text mark, .vtx-seg-text mark{
  background:var(--ad-gold-hi) !important;
  color:#1a120a !important;
}
mark.active, .tx-seg-text mark.active, .vtx-seg-text mark.active{
  background:#ff9800 !important; color:#1a120a !important;
}

/* Tag chips */
.tag-chip{
  border-radius:0 !important;
  background:transparent !important;
}
.tag-chip.active{
  background:linear-gradient(180deg, var(--ad-gold-hi), var(--ad-bronze)) !important;
  color:#1a120a !important;
  border-color:var(--ad-gold-hi) !important;
}

/* Scrollbars: a thin brass scrollbar in webkit so the look carries through
   even on long pages. */
*::-webkit-scrollbar{ width:9px; height:9px; }
*::-webkit-scrollbar-track{ background:#0a0703; }
*::-webkit-scrollbar-thumb{
  background:linear-gradient(180deg, var(--ad-bronze), #4a3618);
  border-radius:1px;
}
*::-webkit-scrollbar-thumb:hover{ background:var(--ad-gold); }

/* Selection */
::selection{ background:var(--ad-gold); color:#1a120a; }

/* Logo-aware corner ornament: every <main> gets a hairline gold border on
   the top edge so pages feel like framed cards. Optional luxury. */
main{ border-top:1px solid var(--ad-line) !important; }
</style>
"""


def inject_chrome(html: str, active: str = "") -> str:
    """Replace the ``<!-- pg-chrome -->`` placeholder in *html* with the
    rendered shared header. If the placeholder isn't present this is a
    no-op so we don't accidentally double-render.

    When the user has selected a non-default theme, a global CSS overlay
    is prepended to the chrome block so it lands AFTER each page's inline
    ``<style>`` (which is in ``<head>``). Equal-specificity rules later
    in the cascade win, and we add ``!important`` to the load-bearing
    color / border properties so the overlay reliably re-skins pages
    without us having to touch each page's CSS individually.
    """
    if CHROME_PLACEHOLDER not in html:
        return html
    theme = _current_theme()
    chrome = header_html(active, theme=theme)
    if theme == "art_deco":
        chrome = ART_DECO_STYLE + chrome
    return html.replace(CHROME_PLACEHOLDER, chrome)
