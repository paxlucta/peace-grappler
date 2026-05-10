"""captions.py — Burn-in subtitles onto video clips.

Renders each transcript segment as a transparent PNG (Pillow) and overlays
it onto the source video for the segment's time range. We use Pillow
instead of ffmpeg's `subtitles` / `ass` / `drawtext` filters because
Homebrew's default ffmpeg ships *without* libass, libfreetype, or
fontconfig — those filters simply don't exist in the binary, so the
filtergraph fails silently. Pillow has all of that built-in and gives us
exact control over font, color, background, and word wrap.

Used by:
    - The AI Wizard's "Add captions" checkbox.
    - The Builder's per-layer captions toggle (with per-clip override).

Public API:
    burn_captions(input_video, output_video, segments, style=None) -> bool
    build_caption_pngs(...) — exposed for tests / debugging.
"""

import os
import shutil
import subprocess
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None


# ── Font resolution ──────────────────────────────────────────────────────────
# Generic-family → known system font path on macOS. We bundle no fonts; this
# relies on what's in /System/Library/Fonts. If a user passes an explicit
# font name, we look it up case-insensitively under those system dirs as
# best-effort and fall back to Helvetica on miss.
_SYSTEM_FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
]

_FAMILY_FALLBACKS = {
    "sans":  ["Helvetica.ttc", "HelveticaNeue.ttc", "Arial.ttf"],
    "serif": ["Times.ttc", "Times New Roman.ttf", "Georgia.ttf"],
    "mono":  ["Menlo.ttc", "Courier.ttc", "Courier New.ttf"],
}


def _resolve_font_path(family):
    """Return the first existing font path for *family* or a system default."""
    fam = (family or "sans").strip()
    candidates = _FAMILY_FALLBACKS.get(fam.lower())
    if candidates is None:
        # User passed an arbitrary name — search by case-insensitive prefix.
        candidates = []
        for d in _SYSTEM_FONT_DIRS:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.lower().startswith(fam.lower()) and f.lower().endswith(
                        (".ttf", ".ttc", ".otf")):
                    candidates.append(f)
        if not candidates:
            candidates = _FAMILY_FALLBACKS["sans"]
    for name in candidates:
        for d in _SYSTEM_FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def _load_font(family, size):
    path = _resolve_font_path(family)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    # Last-ditch fallback — Pillow's bundled bitmap font (ugly but works).
    return ImageFont.load_default()


# ── Color parsing ───────────────────────────────────────────────────────────

def _parse_hex(hex_str, default=(255, 255, 255)):
    """`#RRGGBB` → (r,g,b) tuple. Accepts no-hash form too."""
    s = (hex_str or "").lstrip("#").strip()
    if len(s) != 6:
        return default
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return default


# ── Text wrapping ────────────────────────────────────────────────────────────

def _wrap_text(text, font, max_width):
    """Greedy word-wrap *text* into lines that each fit within *max_width*."""
    words = (text or "").split()
    if not words:
        return []
    lines = []
    cur = words[0]
    for w in words[1:]:
        trial = cur + " " + w
        bbox = font.getbbox(trial)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


# ── Caption PNG rendering ────────────────────────────────────────────────────

def _render_caption_png(text, video_w, video_h, style, out_path,
                        position_override=None):
    """Render *text* as a caption strip PNG at the size needed by the
    chosen vertical band. Returns (x_offset, y_offset, width, height)
    so the caller knows where to overlay it on the source video."""
    font_size = int(style.get("size") or max(36, video_w // 22))
    font = _load_font(style.get("font"), font_size)

    text_color = _parse_hex(style.get("color"), (255, 255, 255))
    has_bg     = bool(style.get("bg") and style.get("bg") != "none")
    bg_color   = _parse_hex(style.get("bg"), (0, 0, 0))
    bg_alpha   = 255 if not has_bg else int(round(
        max(0, min(100, 100 - int(style.get("bg_alpha") or 0))) * 2.55))
    show_outline = (not has_bg) and bool(style.get("outline", True))

    # Wrap text to ~80% of the video width to leave breathing room.
    avail_w = int(video_w * 0.86)
    lines = _wrap_text(text, font, avail_w)
    if not lines:
        return None

    # Measure: line height + small gap between lines, plus padding.
    pad_x = max(18, video_w // 60)
    pad_y = max(10, font_size // 4)
    line_gap = max(4, font_size // 6)
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]

    text_w = max(font.getbbox(ln)[2] - font.getbbox(ln)[0] for ln in lines)
    text_h = line_h * len(lines) + line_gap * (len(lines) - 1)
    box_w = min(video_w, text_w + pad_x * 2)
    box_h = text_h + pad_y * 2

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if has_bg:
        draw.rounded_rectangle(
            [(0, 0), (box_w - 1, box_h - 1)],
            radius=max(6, font_size // 6),
            fill=bg_color + (bg_alpha,),
        )

    # Per-line draw, centered horizontally inside the box.
    y = pad_y
    for ln in lines:
        bbox = font.getbbox(ln)
        line_w = bbox[2] - bbox[0]
        x = (box_w - line_w) // 2
        if show_outline:
            # Cheap 1px outline (8 directions). Skipped when there's a
            # background box since the box already separates from video.
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                           (-1, -1), (1, -1), (-1, 1), (1, 1)):
                draw.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0, 220))
        draw.text((x, y), ln, font=font, fill=text_color + (255,))
        y += line_h + line_gap

    img.save(out_path, "PNG")

    # Position inside the video frame. Per-segment override beats the
    # global style — caller passes one when different clips in the same
    # composited frame want captions at different vertical bands.
    pos = (position_override or style.get("position") or "bottom").lower()
    margin_v = max(40, video_h // 18)
    x_off = (video_w - box_w) // 2
    if pos == "top":
        y_off = margin_v
    elif pos == "middle":
        y_off = (video_h - box_h) // 2
    else:  # 'bottom' default
        y_off = video_h - box_h - margin_v
    return (x_off, max(0, y_off), box_w, box_h)


# ── ffprobe helper ──────────────────────────────────────────────────────────

def _probe_dimensions(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split(",")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1080, 1920


# ── Public API ───────────────────────────────────────────────────────────────

def burn_captions(input_video, output_video, segments, style=None,
                  timeout=180):
    """Burn *segments* onto *input_video*, writing *output_video*.

    *segments*: ``[{"start": float, "end": float, "text": str,
        "position": "top|middle|bottom" (optional)}, ...]`` —
        clip-relative seconds. ``position`` overrides ``style.position``
        for that one segment so multiple clips in the same composited
        frame can render at different vertical bands.
    *style*: ``font``, ``color``, ``bg``, ``bg_alpha``, ``position``,
        ``size``, ``outline``.

    Strategy: render each segment to a transparent PNG, then overlay
    them all in a single ffmpeg pass with ``enable='between(t,s,e)'``
    expressions. Avoids the libass/drawtext dependency that Homebrew's
    ffmpeg lacks by default.

    Returns True on success; on any failure returns False so callers
    can fall back to the un-captioned clip.
    """
    if Image is None:
        return False  # Pillow unavailable
    if not segments:
        return False

    style = style or {}
    video_w, video_h = _probe_dimensions(input_video)

    workdir = tempfile.mkdtemp(prefix="pg_caps_")
    try:
        # Render each non-empty segment to its own PNG, capturing position.
        rendered = []  # list of (png_path, start, end, x, y)
        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(seg.get("start", 0))
                end   = float(seg.get("end", 0))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            png_path = os.path.join(workdir, f"seg_{i:04d}.png")
            placement = _render_caption_png(
                text, video_w, video_h, style, png_path,
                position_override=seg.get("position"),
            )
            if not placement:
                continue
            x, y, _w, _h = placement
            rendered.append((png_path, start, end, x, y))

        if not rendered:
            return False

        # Build ffmpeg command. One -i per PNG, then chain overlay filters.
        cmd = ["ffmpeg", "-y", "-i", str(input_video)]
        for png_path, _s, _e, _x, _y in rendered:
            cmd += ["-i", png_path]

        chain = []
        prev_label = "0:v"
        for idx, (_p, s, e, x, y) in enumerate(rendered):
            in_label = f"{idx + 1}:v"
            out_label = f"v{idx + 1}"
            # Last filter doesn't need to relabel the output stream.
            label_suffix = f"[{out_label}]" if idx < len(rendered) - 1 else ""
            chain.append(
                f"[{prev_label}][{in_label}]overlay="
                f"x={x}:y={y}:enable='between(t,{s:.3f},{e:.3f})'"
                f"{label_suffix}"
            )
            prev_label = out_label

        # If only one overlay, ffmpeg still wants the filter; the loop above
        # handles that case (no relabel suffix).
        filter_complex = ";".join(chain)

        cmd += ["-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", "-pix_fmt", "yuv420p",
                str(output_video)]

        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return r.returncode == 0 and os.path.exists(output_video)
        except Exception:
            return False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Backwards-compat shim — older code path imported build_ass for tests.
# The Pillow renderer doesn't produce ASS, so we keep the symbol as a
# stub returning empty so anything that imports it doesn't crash, and
# return type is preserved (str).
def build_ass(*_args, **_kwargs):
    return ""
