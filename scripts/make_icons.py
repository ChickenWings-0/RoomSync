#!/usr/bin/env python3
"""Generate every icon size RoomSync needs, from one source image or from code.

Four consumers, three formats, and they do not overlap:

  Windows .lnk / taskbar     .ico, multi-resolution in ONE file
  PWA manifest               .png at 192 and 512, plus a maskable variant
  Linux .desktop             .png in the hicolor theme's sized directories
  favicon                    .ico at the web root, for a normal browser tab

Run with no arguments and it draws the source itself — the same amber ring the
tray icon uses, so the shortcut, the taskbar and the tray are visibly one app.
Pass a PNG and it uses that instead:

    python scripts/make_icons.py                     # draw it
    python scripts/make_icons.py art/roomsync.png    # use my artwork

Everything lands in web/static/icons/, which is inside the directory the app
already serves, so the manifest and the .desktop file can both point at files
that exist without a build step or an install path to resolve.
"""

import argparse
import sys
from pathlib import Path

# Run from anywhere: the project root is this file's grandparent.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

OUT_DIR = ROOT / "web" / "static" / "icons"

# ── What each consumer needs ─────────────────────────────────────────
# .ico carries every size in one file; Windows picks per context (16 in the
# title bar, 32 in the taskbar, 256 in large-icon Explorer views). Shipping a
# single 256 and letting Windows downscale is what makes taskbar icons look
# muddy, so the small sizes are rendered rather than resampled where it counts.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# The manifest needs 192 and 512 at minimum; 384 fills the gap on mid-DPI
# Android. Linux hicolor wants the standard theme sizes.
PNG_SIZES = (16, 32, 48, 64, 128, 192, 256, 384, 512)

# Maskable icons are cropped to whatever shape the platform wants (circle,
# squircle, rounded square). The safe zone is the centre 80% — content outside
# it WILL be clipped — so the artwork is scaled to 60% and padded. Without a
# maskable variant, Android crops the plain icon and takes the ring's edge off.
MASKABLE_SIZES = (192, 512)
MASKABLE_SAFE_FRACTION = 0.6

BG = (16, 16, 20, 255)          # matches the dashboard's background
AMBER = (255, 147, 41, 255)     # the app's accent, and the tray icon's ring


def draw_source(size: int = 1024) -> "Image.Image":
    """The generated fallback: the tray icon's ring, at poster resolution.

    Drawn at 1024 and downsampled by the caller rather than drawn per size —
    for a shape this simple that is indistinguishable from re-rendering, and it
    keeps the generated icon identical in construction to a supplied PNG so
    both paths go through the same resize code.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size * 0.08
    hole = size * 0.28
    d.ellipse((pad, pad, size - pad, size - pad), fill=AMBER)
    d.ellipse((hole, hole, size - hole, size - hole), fill=BG)
    return img


def load_source(path: Path) -> "Image.Image":
    img = Image.open(path).convert("RGBA")
    if img.width != img.height:
        # Pad to square rather than stretch: a stretched icon is immediately
        # obvious in a taskbar full of correctly-proportioned ones.
        side = max(img.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
        img = square
    return img


def resize(src: "Image.Image", size: int) -> "Image.Image":
    return src.resize((size, size), Image.LANCZOS)


def make_maskable(src: "Image.Image", size: int) -> "Image.Image":
    """Artwork at 60% on an opaque background, for platforms that crop icons.

    Opaque on purpose: a maskable icon with a transparent background shows the
    launcher's own colour through the corners it did not crop, which reads as a
    rendering bug rather than a design.
    """
    canvas = Image.new("RGBA", (size, size), BG)
    inner = int(size * MASKABLE_SAFE_FRACTION)
    art = resize(src, inner)
    offset = (size - inner) // 2
    canvas.paste(art, (offset, offset), art)
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate RoomSync's icon set.")
    ap.add_argument("source", nargs="?", type=Path,
                    help="Source image (square PNG preferred). Omit to draw one.")
    ap.add_argument("--out", type=Path, default=OUT_DIR,
                    help="Output directory (default: web/static/icons)")
    args = ap.parse_args()

    if args.source:
        if not args.source.is_file():
            return print("No such file: %s" % args.source) or 1
        src = load_source(args.source)
        print("Source: %s (%dx%d)" % (args.source, src.width, src.height))
        if src.width < 512:
            # Not fatal — upscaling still produces usable small sizes — but the
            # 512 manifest icon will be soft and worth knowing about.
            print("  warning: smaller than 512px; the large icons will be soft.")
    else:
        src = draw_source()
        print("Source: generated (matches the tray icon)")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    # ── PNGs ──
    for size in PNG_SIZES:
        resize(src, size).save(out / ("icon-%d.png" % size), "PNG", optimize=True)
    print("PNG   %s" % ", ".join("%d" % s for s in PNG_SIZES))

    # ── Maskable PNGs ──
    for size in MASKABLE_SIZES:
        make_maskable(src, size).save(
            out / ("icon-maskable-%d.png" % size), "PNG", optimize=True)
    print("PNG   maskable %s" % ", ".join("%d" % s for s in MASKABLE_SIZES))

    # ── ICO ──
    # One file, every size. Pillow builds the multi-resolution ICO from the
    # `sizes` argument, downsampling from the image it is given — so it is
    # handed the largest render, not a small one.
    ico_src = resize(src, max(ICO_SIZES))
    ico_path = out / "roomsync.ico"
    ico_src.save(ico_path, "ICO", sizes=[(s, s) for s in ICO_SIZES])
    print("ICO   %s -> %s" % (", ".join("%d" % s for s in ICO_SIZES), ico_path.name))

    # The favicon lives at the static root as well: browsers request
    # /static/favicon.ico by convention, and the shortcut scripts want a
    # stable path that does not depend on the icons/ layout.
    fav = out.parent / "favicon.ico"
    ico_src.save(fav, "ICO", sizes=[(s, s) for s in (16, 32, 48)])
    print("ICO   favicon -> %s" % fav.relative_to(ROOT))

    print("\nDone. %d files in %s" % (len(list(out.glob('*'))), out))
    print("Windows shortcut icon:  %s" % ico_path)
    print("Linux .desktop icon:    %s" % (out / "icon-256.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
