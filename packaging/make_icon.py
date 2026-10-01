"""Draw the application icon.

Generated rather than checked in as a binary, so it can be changed by
editing code and so the build needs nothing but Pillow. Produces a PNG at
every size a platform asks for, a Windows .ico, and - only on a Mac, where
iconutil exists - a .icns.

The mark is a folded signature seen from the spine: a stack of nested
sheets, which is what the whole program is about.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ICONSET = HERE / "icon.iconset"

PAPER = (247, 245, 240)
INK = (24, 24, 28)
SPINE = (188, 60, 48)
SIZES = (16, 32, 64, 128, 256, 512, 1024)


def draw_icon(size: int = 1024) -> Image.Image:
    """An open signature seen from the spine.

    Drawn at 4x and reduced, because the leaf edges are thin diagonals and
    Pillow will not antialias a polygon on its own. Everything is laid out
    in a nominal 1024 grid and scaled, so the shape is identical at every
    size; only the stroke widths are clamped so they survive down at 16px.
    """
    ss = 4
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 1024.0

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(180 * s),
                        fill=INK)

    cx = 512 * s
    fold_top, fold_bottom = 300 * s, 742 * s
    stroke = max(1, int(3 * s))

    # Four nested sheets per side. The outermost is the widest and sits
    # lowest, so the stack fans the way a folded section really does.
    leaves = [(360, 300, 742), (300, 318, 724), (240, 336, 706),
              (180, 354, 688)]
    for i, (reach, top, bottom) in enumerate(leaves):
        shade = tuple(int(c - 14 * i) for c in PAPER)
        for direction in (-1, 1):
            edge = cx + direction * reach * s
            d.polygon([(cx, fold_top + (top - 300) * s),
                       (edge, top * s + 44 * s),
                       (edge, bottom * s),
                       (cx, fold_bottom - (742 - bottom) * s)], fill=shade)
            # the top edge of each leaf, so the sheets stay countable
            d.line([(cx, fold_top + (top - 300) * s),
                    (edge, top * s + 44 * s)], fill=INK, width=stroke)

    # the fold, and the sewing stations along it
    d.line([(cx, fold_top), (cx, fold_bottom)], fill=SPINE,
           width=max(2, int(16 * s)))
    for t in (0.18, 0.5, 0.82):
        y = fold_top + t * (fold_bottom - fold_top)
        r = max(1.5, 17 * s)
        d.ellipse([cx - r, y - r, cx + r, y + r], fill=INK)

    return img.resize((size, size), Image.LANCZOS)


def write_pngs(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    master = draw_icon(1024)
    written = []
    for size in SIZES:
        p = out_dir / f"icon_{size}.png"
        master.resize((size, size), Image.LANCZOS).save(p)
        written.append(p)
    master.save(out_dir / "icon.png")
    return written


def write_ico(path: Path) -> Path:
    draw_icon(1024).save(path, sizes=[(s, s) for s in
                                      (16, 32, 48, 64, 128, 256)])
    return path


def write_icns(path: Path) -> Path | None:
    """macOS only: iconutil turns an .iconset folder into an .icns."""
    ICONSET.mkdir(parents=True, exist_ok=True)
    master = draw_icon(1024)
    # iconutil insists on these exact names.
    for size in (16, 32, 128, 256, 512):
        master.resize((size, size), Image.LANCZOS).save(
            ICONSET / f"icon_{size}x{size}.png")
        master.resize((size * 2, size * 2), Image.LANCZOS).save(
            ICONSET / f"icon_{size}x{size}@2x.png")
    try:
        subprocess.run(["iconutil", "-c", "icns", str(ICONSET),
                        "-o", str(path)], check=True)
        return path
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"iconutil unavailable ({exc}); skipping .icns", file=sys.stderr)
        return None


def main() -> int:
    write_pngs(HERE / "icons")
    write_ico(HERE / "icon.ico")
    print(f"wrote {HERE / 'icons'} and {HERE / 'icon.ico'}")
    if sys.platform == "darwin":
        if write_icns(HERE / "icon.icns"):
            print(f"wrote {HERE / 'icon.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
