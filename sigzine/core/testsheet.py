"""Printer test targets.

Everything here prints on one sheet at a time and is designed to be read
back either by eye or by scanning (see scan.py). Each generated sheet
writes a sidecar .patches.json describing where every patch sits in
normalised sheet coordinates, so a scan can be sampled automatically.
"""
from __future__ import annotations

import io
import json
import math
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import fitz
import numpy as np
from PIL import Image

from . import draw
from .tone import (PRESETS, SCREENS, ToneSettings, apply_preset, apply_tone,
                   combined_lut)
from .units import MM, INCH, Size, paper

Rect = Tuple[float, float, float, float]


@dataclass
class Patch:
    id: str
    nominal: float
    box: Tuple[float, float, float, float]   # normalised x0,y0,x1,y1


@dataclass
class SheetMap:
    kind: str
    sheet_w: float
    sheet_h: float
    created: float = field(default_factory=time.time)
    fiducials: List[Tuple[float, float]] = field(default_factory=list)
    patches: List[Patch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["patches"] = [asdict(p) for p in self.patches]
        return d

    @classmethod
    def load(cls, path: Path) -> "SheetMap":
        d = json.loads(Path(path).read_text())
        patches = [Patch(**p) for p in d.pop("patches", [])]
        d["fiducials"] = [tuple(f) for f in d.get("fiducials", [])]
        return cls(patches=patches, **d)

    def save(self, path: Path) -> Path:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))
        return Path(path)


def map_path_for(pdf_path: Path) -> Path:
    p = Path(pdf_path)
    return p.with_suffix(".patches.json")


def fit_box(box: Rect, img_size: Tuple[int, int]) -> Rect:
    """Shrink ``box`` so the image keeps its aspect ratio, centred."""
    from .units import fit_rect, Size
    return fit_rect(Size(float(img_size[0]), float(img_size[1])), box, "fit")


def target_px(box: Rect, s: "ToneSettings") -> Tuple[int, int]:
    """Pixel size for a placed image: device grid when screened, else contone."""
    dpi = s.device_dpi if s.screen not in ("none", "", None) else s.render_dpi
    return (max(1, int((box[2] - box[0]) / 72 * dpi)),
            max(1, int((box[3] - box[1]) / 72 * dpi)))


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A synthetic photographic target, so the proof sheets mean something
# without the user having to supply an image.
# ---------------------------------------------------------------------------

def synthetic_photo(width: int = 900, height: int = 650) -> Image.Image:
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    u, v = x / width, y / height

    # sky-like vertical gradient with a slow horizontal shift
    img = 0.92 - 0.55 * v + 0.08 * np.sin(u * 3.0)

    # a shaded sphere: the classic test for smooth midtones
    cx, cy, r = 0.34, 0.58, 0.26
    dx, dy = (u - cx) * (width / height), (v - cy)
    d2 = dx * dx + dy * dy
    inside = d2 < r * r
    nz = np.sqrt(np.clip(r * r - d2, 0, None)) / r
    lx, ly, lz = -0.45, -0.6, 0.66
    lam = np.clip(dx / r * lx + dy / r * ly + nz * lz, 0, 1)
    sphere = 0.06 + 0.9 * lam ** 1.4 + 0.35 * np.clip(lam, 0, 1) ** 12
    img = np.where(inside, sphere, img)

    # cast shadow
    sd = ((u - cx - 0.05) * 1.1) ** 2 + ((v - cy - r * 0.95) * 3.2) ** 2
    img = np.where((~inside) & (sd < 0.09), img * (0.35 + 2.2 * sd), img)

    # deep shadow block and specular white block, to check both ends
    def block(y0f, y1f, x0f, x1f, lo, hi):
        y0, y1 = int(y0f * height), int(y1f * height)
        x0, x1 = int(x0f * width), int(x1f * width)
        if y1 > y0 and x1 > x0:
            img[y0:y1, x0:x1] = np.linspace(lo, hi, x1 - x0)[None, :]

    block(0.80, 0.93, 0.06, 0.20, 0.0, 0.22)
    block(0.06, 0.16, 0.78, 0.94, 0.80, 1.0)

    # fine high-frequency texture to judge sharpening and screening
    tex_y0, tex_y1 = int(0.62 * height), int(0.95 * height)
    tex_x0, tex_x1 = int(0.60 * width), int(0.96 * width)
    ty, tx = np.mgrid[0:tex_y1 - tex_y0, 0:tex_x1 - tex_x0].astype(np.float32)
    freq = 0.15 + 0.9 * (tx / max(1, tex_x1 - tex_x0)) ** 2
    tex = 0.5 + 0.42 * np.sin(tx * freq) * np.cos(ty * 0.35)
    img[tex_y0:tex_y1, tex_x0:tex_x1] = tex

    # a 10-step wedge along the bottom
    wy0, wy1 = int(0.955 * height), height
    steps = np.repeat(np.linspace(1.0, 0.0, 10), int(np.ceil(width / 10)))[:width]
    if wy1 > wy0:
        img[wy0:wy1, :] = steps[None, :]

    arr = np.clip(img, 0, 1) * 255.0
    return Image.fromarray(arr.astype(np.uint8))


# ---------------------------------------------------------------------------
# 1. Linearisation sheet
# ---------------------------------------------------------------------------

def linearisation_sheet(path: Path, sheet: Size = None, title: str = "",
                        coarse_step: float = 0.02,
                        fine_step: float = 0.005) -> Tuple[Path, SheetMap]:
    sheet = sheet or paper("Letter")
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height
    margin = 14 * MM
    smap = SheetMap(kind="linearisation", sheet_w=W, sheet_h=H)

    # corner fiducials, used to find the sheet in a scan
    fx, fy = 8 * MM, 8 * MM
    for (x, y) in ((fx, fy), (W - fx, fy), (W - fx, H - fy), (fx, H - fy)):
        draw.fiducial(page, x, y, 5 * MM)
        smap.fiducials.append((x / W, y / H))

    draw.text(page, margin, 20 * MM, "LINEARISATION TARGET", size=13,
              font=draw.FONT_BOLD)
    sub = title or time.strftime("%Y-%m-%d")
    draw.text(page, margin, 25 * MM, sub, size=8)
    draw.text(page, margin, 29.5 * MM,
              "Print at 100%. Turn OFF toner save, sharpening, contrast and any "
              "\"photo enhancement\" in the driver.", size=7)

    def add_grid(y0: float, values: Sequence[float], cols: int, cw: float,
                 ch: float, label_size: float, tag: str,
                 heading: str) -> float:
        draw.text(page, margin, y0 - 2.5 * MM, heading, size=8.5,
                  font=draw.FONT_BOLD)
        for i, val in enumerate(values):
            c, r = i % cols, i // cols
            x = margin + c * cw
            y = y0 + r * ch
            box = (x, y, x + cw - 1.2 * MM, y + ch - 4.2 * MM)
            draw.rect(page, box, fill=draw.ink(val), color=(0.6,), width=0.15)
            draw.text(page, x + (cw - 1.2 * MM) / 2, y + ch - 1.0 * MM,
                      f"{val * 100:.1f}".rstrip("0").rstrip("."), size=label_size,
                      align="center")
            smap.patches.append(Patch(f"{tag}{i}", float(val),
                                      (box[0] / W, box[1] / H, box[2] / W, box[3] / H)))
        rows = math.ceil(len(values) / cols)
        return y0 + rows * ch

    coarse = [round(i * coarse_step, 4) for i in
              range(int(round(1.0 / coarse_step)) + 1)]
    cols = 9
    cw = (W - 2 * margin) / cols
    y = add_grid(36 * MM, coarse, cols, cw, 15 * MM, 5.5, "c",
                 f"Main ramp - 0 to 100% in {coarse_step * 100:g}% steps")

    hi = [round(i * fine_step, 4) for i in range(int(round(0.10 / fine_step)) + 1)]
    y = add_grid(y + 9 * MM, hi, len(hi), (W - 2 * margin) / len(hi), 14 * MM,
                 4.2, "h", "Highlight wedge - the first patch you can see is "
                           "your minimum printable dot")

    lo = [round(0.90 + i * fine_step, 4)
          for i in range(int(round(0.10 / fine_step)) + 1)]
    y = add_grid(y + 9 * MM, lo, len(lo), (W - 2 * margin) / len(lo), 14 * MM,
                 4.2, "s", "Shadow wedge - the last patch you can tell from "
                           "solid is your shadow limit")

    # continuous gradient, to see banding and screen artefacts
    y += 8 * MM
    draw.text(page, margin, y, "Continuous gradient (look for banding and "
                               "screen texture)", size=8.5, font=draw.FONT_BOLD)
    grad_h = 16 * MM
    ramp = np.tile(np.linspace(255, 0, 1400).astype(np.uint8), (90, 1))
    draw.place_image(page, (margin, y + 2 * MM, W - margin, y + 2 * MM + grad_h),
                     png_bytes(Image.fromarray(ramp)))
    y += grad_h + 8 * MM

    # reversed text and thin rules, the first things to fill in
    draw.text(page, margin, y, "Fill-in check", size=8.5, font=draw.FONT_BOLD)
    y += 3 * MM
    bw = (W - 2 * margin) / 4
    for i, cov in enumerate((0.6, 0.75, 0.9, 1.0)):
        bx = margin + i * bw
        draw.rect(page, (bx, y, bx + bw - 2 * MM, y + 12 * MM),
                  fill=draw.ink(cov), color=None, width=0)
        draw.centered_text(page, (bx, y + 3 * MM, bx + bw - 2 * MM, y + 10 * MM),
                           f"{int(cov * 100)}% reversed 6pt", size=6,
                           color=(1.0,))
    y += 16 * MM

    draw.text(page, margin, y,
              "Read the patch numbers back into Signature Zine: Calibrate tab > "
              "enter measurements, or scan this sheet at 300 dpi and load it.",
              size=7)

    doc.save(str(path))
    doc.close()
    smap.save(map_path_for(path))
    return Path(path), smap


# ---------------------------------------------------------------------------
# 1b. Verification sheet - the linearisation target, printed through the
#     correction, so measuring it says whether the correction worked.
# ---------------------------------------------------------------------------

def verification_sheet(path: Path, profile, sheet: Size = None,
                       title: str = "", step: float = 0.05
                       ) -> Tuple[Path, SheetMap]:
    """Patches that ask for an even ladder of coverage, after correction.

    Every patch is drawn with the ink value the profile says is needed to
    land on its label. Measure them and you are measuring the error that is
    left, directly in the units you care about: a patch labelled 40% that
    reads 46% means the profile is 6% out there. The patch map records the
    label - the wanted coverage - not the ink that was sent, so a scan can
    be compared against it without knowing anything about the profile.
    """
    sheet = sheet or paper("Letter")
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height
    margin = 14 * MM
    smap = SheetMap(kind="verification", sheet_w=W, sheet_h=H)

    fx, fy = 8 * MM, 8 * MM
    for (x, y) in ((fx, fy), (W - fx, fy), (W - fx, H - fy), (fx, H - fy)):
        draw.fiducial(page, x, y, 5 * MM)
        smap.fiducials.append((x / W, y / H))

    lin = profile.linearisation_lut(1024)
    grid = np.linspace(0.0, 1.0, lin.size)

    def send_for(wanted: float) -> float:
        return float(np.interp(wanted, grid, lin))

    draw.text(page, margin, 20 * MM, "LINEARISATION CHECK", size=13,
              font=draw.FONT_BOLD)
    name = getattr(profile, "name", "") or "unnamed profile"
    draw.text(page, margin, 25 * MM,
              f"{title or time.strftime('%Y-%m-%d')} - through '{name}'"
              + (f", pass {profile.passes}" if getattr(profile, "passes", 0)
                 else ""), size=8)
    draw.text(page, margin, 29.5 * MM,
              "Same driver settings as the target you measured. Each patch is "
              "labelled with the coverage it is aiming at, not the ink that "
              "was sent.", size=7)

    wanted = [round(i * step, 4)
              for i in range(int(round(1.0 / step)) + 1)]
    # The ends are where a correction usually falls over, so sample them finer.
    wanted += [0.01, 0.02, 0.03, 0.97, 0.98, 0.99]
    wanted = sorted(set(w for w in wanted if 0.0 <= w <= 1.0))

    cols = 7
    cw = (W - 2 * margin) / cols
    ch = 17 * MM
    y0 = 38 * MM
    draw.text(page, margin, y0 - 2.5 * MM,
              "Measure these. Each should read as its label.", size=8.5,
              font=draw.FONT_BOLD)
    for i, want in enumerate(wanted):
        c, r = i % cols, i // cols
        x = margin + c * cw
        y = y0 + r * ch
        box = (x, y, x + cw - 1.2 * MM, y + ch - 4.6 * MM)
        draw.rect(page, box, fill=draw.ink(send_for(want)),
                  color=(0.6,), width=0.15)
        draw.text(page, x + (cw - 1.2 * MM) / 2, y + ch - 1.2 * MM,
                  f"{want * 100:.0f}" if want * 100 >= 1 or want == 0
                  else f"{want * 100:.0f}",
                  size=5.5, align="center")
        smap.patches.append(Patch(f"v{i}", float(want),
                                  (box[0] / W, box[1] / H,
                                   box[2] / W, box[3] / H)))
    y = y0 + math.ceil(len(wanted) / cols) * ch + 6 * MM

    # Corrected against uncorrected, so the difference is visible by eye.
    for label, corrected in (("Corrected ramp - should look evenly spaced",
                              True),
                             ("Uncorrected ramp, for comparison", False)):
        draw.text(page, margin, y, label, size=8.5, font=draw.FONT_BOLD)
        y += 2.5 * MM
        px = 1200
        want_row = np.linspace(0.0, 1.0, px)
        ink_row = (np.interp(want_row, grid, lin) if corrected else want_row)
        ramp = np.tile(((1.0 - ink_row) * 255).astype(np.uint8), (70, 1))
        draw.place_image(page, (margin, y, W - margin, y + 14 * MM),
                         png_bytes(Image.fromarray(ramp)))
        y += 18 * MM

    # A step wedge in equal coverage steps: banding here is the fit wobbling.
    draw.text(page, margin, y, "Even steps of 10% - look for a step that "
                               "jumps or stalls", size=8.5,
              font=draw.FONT_BOLD)
    y += 2.5 * MM
    steps = [i / 10.0 for i in range(11)]
    sw = (W - 2 * margin) / len(steps)
    for i, want in enumerate(steps):
        bx = margin + i * sw
        draw.rect(page, (bx, y, bx + sw, y + 13 * MM),
                  fill=draw.ink(send_for(want)), color=None, width=0)
    y += 17 * MM

    draw.text(page, margin, y,
              "Scan this at 300 dpi and load it under \"Check a verification "
              "scan\". If the error is larger than a couple of percent, "
              "refine the profile and print this sheet again.", size=7)

    doc.save(str(path))
    doc.close()
    smap.save(map_path_for(path))
    return Path(path), smap


# ---------------------------------------------------------------------------
# 2. Screening comparison
# ---------------------------------------------------------------------------

def screening_sheet(path: Path, sheet: Size = None, dpi: int = 600,
                    photo: Optional[Image.Image] = None) -> Path:
    sheet = sheet or paper("Letter")
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height
    margin = 13 * MM
    draw.text(page, margin, 16 * MM, "SCREENING COMPARISON", size=13,
              font=draw.FONT_BOLD)
    draw.text(page, margin, 21 * MM,
              "Same image, same tone curve, different halftone. Pick the one "
              "that holds detail without looking noisy on your printer.", size=7.5)

    src = photo or synthetic_photo(800, 560)
    variants = [
        ("Contone - printer does its own screening", dict(screen="none")),
        ("Floyd-Steinberg error diffusion", dict(screen="floyd-steinberg")),
        ("Stochastic (blue noise)", dict(screen="stochastic")),
        ("Ordered / Bayer 8x8", dict(screen="ordered (bayer)")),
        ("Clustered dot 85 lpi at 45 deg",
         dict(screen="halftone dot", screen_lpi=85, screen_angle=45)),
        ("Clustered dot 53 lpi at 45 deg",
         dict(screen="halftone dot", screen_lpi=53, screen_angle=45)),
    ]
    top = 26 * MM
    row_h = (H - top - 12 * MM) / len(variants)
    img_w = (W - 2 * margin) * 0.52
    ramp_w = (W - 2 * margin) - img_w - 4 * MM

    for i, (label, kw) in enumerate(variants):
        s = ToneSettings(device_dpi=dpi, dot_gain=0.15, contrast=0.15,
                         sharpen=0.5, **kw)
        y0 = top + i * row_h
        draw.text(page, margin, y0 + 3.2 * MM, label, size=8, font=draw.FONT_BOLD)
        box = fit_box((margin, y0 + 4.5 * MM, margin + img_w, y0 + row_h - 3 * MM),
                      src.size)
        out = apply_tone(src, s, target_px=target_px(box, s))
        draw.place_image(page, box, png_bytes(out))

        rbox = (margin + img_w + 4 * MM, y0 + 4.5 * MM, W - margin,
                y0 + row_h - 3 * MM)
        rpx = target_px(rbox, s)
        ramp = Image.fromarray(
            np.tile(np.linspace(255, 0, rpx[0]).astype(np.uint8), (rpx[1], 1)))
        draw.place_image(page, rbox, png_bytes(apply_tone(ramp, s)))

    doc.save(str(path))
    doc.close()
    return Path(path)


# ---------------------------------------------------------------------------
# 3. Detail and resolution
# ---------------------------------------------------------------------------

def detail_sheet(path: Path, sheet: Size = None, dpi: int = 600) -> Path:
    sheet = sheet or paper("Letter")
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height
    m = 14 * MM
    draw.text(page, m, 16 * MM, "DETAIL AND RESOLUTION", size=13,
              font=draw.FONT_BOLD)
    draw.text(page, m, 21 * MM, "Vector rules, type sizes and a resolution "
                                "wedge. Read with a loupe if you have one.", size=7.5)

    y = 28 * MM
    draw.text(page, m, y, "Hairlines (pt)", size=8.5, font=draw.FONT_BOLD)
    y += 4 * MM
    widths = [0.05, 0.1, 0.15, 0.2, 0.25, 0.35, 0.5, 0.75, 1.0]
    for w in widths:
        draw.line(page, m, y, m + 70 * MM, y, width=w)
        draw.text(page, m + 73 * MM, y + 2, f"{w:g} pt", size=6)
        y += 4.6 * MM

    y += 3 * MM
    draw.text(page, m, y, "Type sizes, positive and reversed", size=8.5,
              font=draw.FONT_BOLD)
    y += 4 * MM
    sample = "Handgloves 0123 - the quick brown fox"
    for size in (4, 5, 6, 7, 8, 9, 10):
        draw.text(page, m, y + size * 0.8, f"{size}pt  {sample}", size=size)
        bx = m + 105 * MM
        draw.rect(page, (bx, y - 0.5 * MM, W - m, y + size * 1.2),
                  fill=draw.BLACK, color=None, width=0)
        page.insert_text(fitz.Point(bx + 2, y + size * 0.85),
                         f"{size}pt  {sample}", fontsize=size, fontname=draw.FONT,
                         color=(1.0,))
        y += size * 1.45 + 2.2 * MM

    y += 2 * MM
    draw.text(page, m, y, "Resolution wedge and Siemens star", size=8.5,
              font=draw.FONT_BOLD)
    y += 4 * MM
    star_r = 22 * MM
    draw.siemens_star(page, m + star_r, y + star_r, star_r, spokes=48)
    # line pair wedge as a 1-bit image so it is exactly on the device grid
    wedge_w, wedge_h = 90 * MM, 44 * MM
    px_w = int(wedge_w / 72 * dpi)
    px_h = int(wedge_h / 72 * dpi)
    xs = np.arange(px_w, dtype=np.float32)
    freq = np.linspace(2, 60, px_w) / dpi * 72      # cycles per point
    phase = np.cumsum(freq) * 2 * np.pi / 72 * (72 / dpi) * dpi / 72
    row = (np.sin(np.cumsum(freq * 2 * np.pi / dpi * dpi / 72)) > 0).astype(np.uint8) * 255
    wedge = np.tile(row, (px_h, 1))
    draw.place_image(page, (m + 2 * star_r + 8 * MM, y, m + 2 * star_r + 8 * MM + wedge_w,
                            y + wedge_h),
                     png_bytes(Image.fromarray(wedge).convert("1")))
    draw.text(page, m + 2 * star_r + 8 * MM, y + wedge_h + 4 * MM,
              "coarse  <-  line pairs  ->  fine", size=6)
    y += 2 * star_r + 10 * MM

    draw.text(page, m, y, "Grey text on grey, for toner scatter", size=8.5,
              font=draw.FONT_BOLD)
    y += 4 * MM
    for i, (fg, bg) in enumerate(((0.0, 0.15), (0.25, 0.0), (0.4, 0.1), (0.6, 0.25))):
        bx = m + i * (W - 2 * m) / 4
        bw = (W - 2 * m) / 4 - 3 * MM
        draw.rect(page, (bx, y, bx + bw, y + 14 * MM), fill=draw.ink(bg),
                  color=None, width=0)
        page.insert_textbox(fitz.Rect(bx, y + 4 * MM, bx + bw, y + 12 * MM),
                            f"{int(fg*100)} on {int(bg*100)}", fontsize=7,
                            fontname=draw.FONT, color=draw.ink(fg),
                            align=fitz.TEXT_ALIGN_CENTER)
    doc.save(str(path))
    doc.close()
    return Path(path)


# ---------------------------------------------------------------------------
# 4. Duplex registration and scaling
# ---------------------------------------------------------------------------

def _scale_bar(page, x: float, y: float, length: float,
               horizontal: bool = True) -> None:
    """A bar of a whole number of centimetres, ticked at both ends."""
    step = 10 * MM
    n = int(round(length / step))
    length = n * step
    if horizontal:
        draw.line(page, x, y, x + length, y, width=0.5)
    else:
        draw.line(page, x, y, x, y + length, width=0.5)
    for i in range(n + 1):
        big = (i % 5 == 0)
        t = (3.0 if big else 1.8) * MM
        if horizontal:
            draw.line(page, x + i * step, y - t, x + i * step, y + t,
                      width=0.4 if big else 0.25)
        else:
            draw.line(page, x - t, y + i * step, x + t, y + i * step,
                      width=0.4 if big else 0.25)
    label = f"{length / MM:.0f} mm exactly"
    if horizontal:
        draw.text(page, x + length / 2, y + 9 * MM, label, size=7,
                  align="center")
    else:
        draw.text(page, x + 6 * MM, y + length / 2, label, size=7)


MIRROR_X = True          # the sheet is turned left to right between passes


def _registration_scale(page, cx: float, cy: float, reach: float = 25 * MM,
                        step: float = MM, label: str = "",
                        compact: bool = False) -> None:
    """A graduated cross at the sheet centre, read against the other side."""
    draw.line(page, cx - reach, cy, cx + reach, cy, width=0.35)
    draw.line(page, cx, cy - reach, cx, cy + reach, width=0.35)
    n = int(reach / step)
    for i in range(-n, n + 1):
        if i == 0:
            continue
        ten = (i % 10 == 0)
        five = (i % 5 == 0)
        if compact and not five:
            pass
        t = (3.4 if ten else (2.4 if five else 1.4)) * MM
        # the across scale hangs below its line, the down scale sits left of its
        draw.line(page, cx + i * step, cy, cx + i * step, cy + t,
                  width=0.35 if five else 0.2)
        draw.line(page, cx - t, cy + i * step, cx, cy + i * step,
                  width=0.35 if five else 0.2)
        if ten:
            draw.text(page, cx + i * step, cy + t + 4.4 * MM, f"{i:+d}",
                      size=6, align="center")
            draw.text(page, cx - t - 2 * MM, cy + i * step + 2, f"{i:+d}",
                      size=6, align="right")
    if not compact:
        draw.text(page, cx + reach + 4 * MM, cy - 2 * MM, "ACROSS  (mm)",
                  size=7, font=draw.FONT_BOLD)
        draw.text(page, cx - reach - 4 * MM, cy + reach + 9 * MM, "DOWN  (mm)",
                  size=7, font=draw.FONT_BOLD)
    if label:
        draw.text(page, cx - reach - 2 * MM, cy - reach - 3 * MM, label,
                  size=11, font=draw.FONT_BOLD)
    # a hollow box at dead centre so zero is unmistakable
    draw.rect(page, (cx - 1.2 * MM, cy - 1.2 * MM, cx + 1.2 * MM,
                     cy + 1.2 * MM), fill=None, color=draw.BLACK, width=0.4)


def _pointer_cross(page, cx: float, cy: float, reach: float = 33 * MM,
                   label: str = "") -> None:
    """The mark on the back that is read against the front's scale."""
    draw.line(page, cx, cy - reach, cx, cy + reach, width=0.4)
    draw.line(page, cx - reach, cy, cx + reach, cy, width=0.4)
    tip = min(1.6 * MM, reach * 0.09)
    for (dx, dy) in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        px, py = cx + dx * reach, cy + dy * reach
        shape = page.new_shape()
        shape.draw_polyline([
            fitz.Point(px, py),
            fitz.Point(px - dy * tip - dx * tip * 2, py - dx * tip - dy * tip * 2),
            fitz.Point(px + dy * tip - dx * tip * 2, py + dx * tip - dy * tip * 2),
        ])
        shape.finish(fill=draw.BLACK, color=None, width=0)
        shape.commit()
    # a small open square around the crossing, to match the front's zero box
    draw.rect(page, (cx - 1.2 * MM, cy - 1.2 * MM, cx + 1.2 * MM,
                     cy + 1.2 * MM), fill=None, color=draw.BLACK, width=0.4)
    if label:
        draw.text(page, cx + 4 * MM, cy - 4 * MM, label, size=11,
                  font=draw.FONT_BOLD)


def station_geometry(sheet: Size) -> Dict[str, Tuple[float, float]]:
    """Where the three measuring stations sit, in front-page coordinates.

    A and C are level with each other and symmetric about the centre line,
    which is what lets rotation be separated from a scale difference.
    """
    W, H = sheet.width, sheet.height
    d = min(70 * MM, (W - 60 * MM) / 2)
    e = min(75 * MM, (H - 150 * MM) / 2)
    return {
        "A": (W / 2 - d, H / 2 - e),
        "B": (W / 2, H / 2),
        "C": (W / 2 + d, H / 2 - e),
    }


def duplex_sheet(path: Path, sheet: Size = None,
                 correction: Tuple[float, float] = (0.0, 0.0)) -> Path:
    """Front-to-back registration, printer scaling, and flip-axis check.

    ``correction`` shifts everything on the back page by that much, in the
    back page's own coordinates, exactly as the imposer does once a profile
    carries a measured offset. Printing it that way proves the correction
    cancels the error instead of doubling it.
    """
    sheet = sheet or paper("Letter")
    doc = fitz.open()
    W, H = sheet.width, sheet.height
    cx, cy = W / 2, H / 2
    m = 14 * MM

    ox, oy = correction
    scratch = fitz.open() if (ox or oy) else None

    for side in ("FRONT", "BACK"):
        if side == "BACK" and scratch is not None:
            # draw into a scratch page, then place it shifted
            page = scratch.new_page(width=W, height=H)
        else:
            page = doc.new_page(width=W, height=H)
        draw.text(page, m, 15 * MM, f"DUPLEX REGISTRATION - {side}", size=13,
                  font=draw.FONT_BOLD)

        if side == "FRONT":
            body = ("Print two sided, then hold the sheet up to a bright "
                    "window with THIS side facing you.\n"
                    "Three pointers show through, marked A, B and C. At each "
                    "one read where the upright line crosses the ACROSS "
                    "scale and where the flat line crosses the DOWN scale. "
                    "Right and down are positive.\n"
                    "B on its own gives the offset. A and C together say "
                    "whether the sheet is also going through skewed, which "
                    "no offset can fix.")
        else:
            body = ("This side carries the pointers. Nothing to read here.\n"
                    "Each is lettered for the station it should land on - A "
                    "is on the right here because the sheet turns over.\n"
                    "If this HEAD bar shows through near the FOOT of the "
                    "front, the sheet was flipped about the wrong edge: "
                    "change the duplex setting rather than the offset.")
        page.insert_textbox(fitz.Rect(m, 18 * MM, W - m, 40 * MM), body,
                            fontsize=7.5, fontname=draw.FONT)

        # head bar, so a wrong flip axis is obvious at a glance
        draw.rect(page, (m, 42 * MM, W - m, 49 * MM), fill=draw.BLACK,
                  color=None, width=0)
        draw.centered_text(page, (m, 43.4 * MM, W - m, 48.5 * MM),
                           f"{side} HEAD", size=8, font=draw.FONT_BOLD,
                           color=(1.0,))

        stations = station_geometry(sheet)
        for tag, (sx, sy) in stations.items():
            big = (tag == "B")
            if side == "FRONT":
                _registration_scale(page, sx, sy,
                                    reach=25 * MM if big else 13 * MM,
                                    label=tag, compact=not big)
            else:
                # mirrored across the sheet, so it lands on its own station
                px = W - sx if MIRROR_X else sx
                _pointer_cross(page, px, sy,
                               reach=33 * MM if big else 18 * MM, label=tag)

        # quick visual check, kept clear of the measuring stations
        for (x, y) in ((m, 56 * MM), (W - m, 56 * MM),
                       (m, H - 58 * MM), (W - m, H - 58 * MM)):
            draw.registration_target(page, x, y, 4 * MM)

        # scaling check, both directions, whole centimetres
        bar_w = int((W - 2 * m - 30 * MM) / (10 * MM)) * 10 * MM
        _scale_bar(page, (W - bar_w) / 2, H - 40 * MM, bar_w, horizontal=True)
        bar_h = int((H - 150 * MM) / (10 * MM)) * 10 * MM
        _scale_bar(page, m + 6 * MM, 70 * MM, bar_h, horizontal=False)
        draw.text(page, m, H - 22 * MM,
                  "Measure both bars with a steel rule. Short means the "
                  "driver is scaling: turn off 'fit to page'.", size=7)
        tail = f"{side} - {sheet.describe()} - print at 100%"
        if side == "BACK" and (ox or oy):
            tail += (f" - correction applied: {ox / MM:+.2f} mm across, "
                     f"{oy / MM:+.2f} mm down")
        draw.text(page, m, H - 15 * MM, tail, size=7, color=(0.45,))
        if side == "BACK" and (ox or oy):
            draw.text(page, m, H - 8 * MM,
                      "If the pointer now reads zero on the front, the "
                      "correction is right. If it reads double, the sign is "
                      "inverted.", size=7)

    if scratch is not None:
        back = doc.new_page(width=W, height=H)
        back.show_pdf_page(fitz.Rect(ox, oy, W + ox, H + oy), scratch, 0)
        scratch.close()

    doc.save(str(path))
    doc.close()
    return Path(path)


# ---------------------------------------------------------------------------
# 5. Photo proof: the same picture through several treatments
# ---------------------------------------------------------------------------

def proof_sheet(path: Path, image: Optional[Image.Image] = None,
                sheet: Size = None, profile=None, dpi: int = 600,
                preset_names: Optional[Sequence[str]] = None,
                base: Optional[ToneSettings] = None) -> Path:
    sheet = (sheet or paper("Letter")).landscape()
    src = image or synthetic_photo(1000, 700)
    names = list(preset_names or ["Neutral (no correction)",
                                  "Laser photo (default)",
                                  "Laser photo (punchy)",
                                  "Laser photo (soft / open shadows)",
                                  "Dithered (Floyd-Steinberg)",
                                  "Halftone 85 lpi"])
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height
    m = 10 * MM
    draw.text(page, m, 11 * MM, "PHOTO PROOF", size=12, font=draw.FONT_BOLD)
    draw.text(page, m, 15 * MM,
              "Same photograph, one treatment per tile. Mark the winner and use "
              "it as your default. The corrected tiles look pale on screen on "
              "purpose - the printer puts the contrast back.", size=7)

    cols = 3
    rows = int(math.ceil(len(names) / cols))
    top = 19 * MM
    cw = (W - 2 * m) / cols
    ch = (H - top - m) / rows
    for i, name in enumerate(names):
        c, r = i % cols, i // cols
        x0 = m + c * cw
        y0 = top + r * ch
        s = apply_preset(base.copy() if base else ToneSettings(), name)
        s.device_dpi = dpi
        box = fit_box((x0, y0 + 4 * MM, x0 + cw - 4 * MM, y0 + ch - 4 * MM),
                      src.size)
        draw.place_image(page, box,
                         png_bytes(apply_tone(src, s, profile, target_px(box, s))))
        draw.text(page, x0, y0 + 3 * MM, f"{i + 1}. {name}", size=7.5,
                  font=draw.FONT_BOLD)
    doc.save(str(path))
    doc.close()
    return Path(path)


TEST_SHEETS = {
    "Linearisation target": linearisation_sheet,
    "Linearisation check": verification_sheet,
    "Screening comparison": screening_sheet,
    "Detail and resolution": detail_sheet,
    "Duplex registration": duplex_sheet,
    "Photo proof": proof_sheet,
}
