"""Low level PDF drawing helpers shared by the imposer and the test sheets."""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

import fitz

from .units import MM

Rect = Tuple[float, float, float, float]

FONT = "helv"
FONT_BOLD = "hebo"
FONT_MONO = "cour"
GRAY = (0.45,)
BLACK = (0.0,)


def gray(value: float) -> Tuple[float]:
    """DeviceGray colour tuple; 0 = black, 1 = white."""
    return (max(0.0, min(1.0, float(value))),)


def ink(coverage: float) -> Tuple[float]:
    """DeviceGray colour for a requested ink coverage (0 = paper)."""
    return gray(1.0 - coverage)


def rect(page: fitz.Page, r: Rect, fill=None, color=None, width: float = 0.5,
         dashes: Optional[str] = None) -> None:
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(*r))
    shape.finish(fill=fill, color=color, width=width, dashes=dashes)
    shape.commit()


def line(page: fitz.Page, x0: float, y0: float, x1: float, y1: float,
         color=BLACK, width: float = 0.4, dashes: Optional[str] = None) -> None:
    shape = page.new_shape()
    shape.draw_line(fitz.Point(x0, y0), fitz.Point(x1, y1))
    shape.finish(color=color, width=width, dashes=dashes)
    shape.commit()


def text(page: fitz.Page, x: float, y: float, s: str, size: float = 7.0,
         font: str = FONT, color=BLACK, align: str = "left",
         rotate: int = 0) -> None:
    if not s:
        return
    if align == "left" and rotate == 0:
        page.insert_text(fitz.Point(x, y), s, fontsize=size, fontname=font,
                         color=color)
        return
    w = fitz.get_text_length(s, fontname=font, fontsize=size)
    pad = 2.0
    if rotate in (90, 270):
        box = fitz.Rect(x - size - pad, y - w / 2 - pad, x + size + pad,
                        y + w / 2 + pad)
    else:
        if align == "center":
            box = fitz.Rect(x - w / 2 - pad, y - size, x + w / 2 + pad, y + size + pad)
        elif align == "right":
            box = fitz.Rect(x - w - pad, y - size, x + pad, y + size + pad)
        else:
            box = fitz.Rect(x - pad, y - size, x + w + pad, y + size + pad)
    page.insert_textbox(box, s, fontsize=size, fontname=font, color=color,
                        align=fitz.TEXT_ALIGN_CENTER if align == "center"
                        else (fitz.TEXT_ALIGN_RIGHT if align == "right"
                              else fitz.TEXT_ALIGN_LEFT),
                        rotate=rotate)


def centered_text(page: fitz.Page, box: Rect, s: str, size: float = 8.0,
                  font: str = FONT, color=BLACK, rotate: int = 0) -> None:
    page.insert_textbox(fitz.Rect(*box), s, fontsize=size, fontname=font,
                        color=color, align=fitz.TEXT_ALIGN_CENTER, rotate=rotate)


def crop_marks(page: fitz.Page, r: Rect, length: float = 5 * MM,
               offset: float = 2 * MM, width: float = 0.25) -> None:
    x0, y0, x1, y1 = r
    s = page.new_shape()
    for (x, y, dx, dy) in (
        (x0, y0, -1, 0), (x0, y0, 0, -1),
        (x1, y0, 1, 0), (x1, y0, 0, -1),
        (x0, y1, -1, 0), (x0, y1, 0, 1),
        (x1, y1, 1, 0), (x1, y1, 0, 1),
    ):
        sx, sy = x + dx * offset, y + dy * offset
        s.draw_line(fitz.Point(sx, sy),
                    fitz.Point(sx + dx * length, sy + dy * length))
    s.finish(color=BLACK, width=width)
    s.commit()


def fold_mark(page: fitz.Page, x0: float, y0: float, x1: float, y1: float,
              bleed: float = 4 * MM, width: float = 0.25) -> None:
    """Dashed tick marks in the waste at each end of a fold line."""
    s = page.new_shape()
    if abs(x1 - x0) < 1e-6:            # vertical fold
        s.draw_line(fitz.Point(x0, y0), fitz.Point(x0, y0 + bleed))
        s.draw_line(fitz.Point(x0, y1 - bleed), fitz.Point(x0, y1))
    else:
        s.draw_line(fitz.Point(x0, y0), fitz.Point(x0 + bleed, y0))
        s.draw_line(fitz.Point(x1 - bleed, y0), fitz.Point(x1, y0))
    s.finish(color=BLACK, width=width, dashes="[1 2] 0")
    s.commit()


def registration_target(page: fitz.Page, x: float, y: float,
                        radius: float = 3 * MM, width: float = 0.3) -> None:
    s = page.new_shape()
    s.draw_circle(fitz.Point(x, y), radius)
    s.draw_circle(fitz.Point(x, y), radius * 0.45)
    s.draw_line(fitz.Point(x - radius * 1.7, y), fitz.Point(x + radius * 1.7, y))
    s.draw_line(fitz.Point(x, y - radius * 1.7), fitz.Point(x, y + radius * 1.7))
    s.finish(color=BLACK, width=width)
    s.commit()


def fiducial(page: fitz.Page, x: float, y: float, size: float = 6 * MM) -> None:
    """Solid square used to find the sheet again in a scan."""
    rect(page, (x - size / 2, y - size / 2, x + size / 2, y + size / 2),
         fill=BLACK, color=None, width=0)


def ruler(page: fitz.Page, x: float, y: float, length: float,
          horizontal: bool = True, step: float = MM, label_every: int = 5,
          tick: float = 2.0) -> None:
    s = page.new_shape()
    n = int(length / step)
    for i in range(n + 1):
        big = (i % label_every == 0)
        t = tick * (2.2 if big else 1.0)
        if horizontal:
            s.draw_line(fitz.Point(x + i * step, y), fitz.Point(x + i * step, y + t))
        else:
            s.draw_line(fitz.Point(x, y + i * step), fitz.Point(x + t, y + i * step))
    s.finish(color=BLACK, width=0.25)
    s.commit()
    for i in range(0, n + 1, label_every):
        if horizontal:
            text(page, x + i * step, y + tick * 2.2 + 6, str(i), size=5,
                 align="center")
        else:
            text(page, x + tick * 2.2 + 8, y + i * step + 2, str(i), size=5)


def collation_mark(page: fitz.Page, x: float, y: float, index: int, total: int,
                   height: float = 10 * MM, width_: float = 3 * MM) -> None:
    """Spine staircase mark: a descending block per signature."""
    if total <= 0:
        return
    step = height / max(1, total)
    top = y + index * step
    rect(page, (x, top, x + width_, top + step), fill=BLACK, color=None, width=0)


def place_image(page: fitz.Page, r: Rect, png_bytes: bytes,
                rotate: int = 0) -> None:
    page.insert_image(fitz.Rect(*r), stream=png_bytes, rotate=rotate,
                      keep_proportion=False)


def siemens_star(page: fitz.Page, cx: float, cy: float, radius: float,
                 spokes: int = 36) -> None:
    s = page.new_shape()
    for i in range(spokes):
        a0 = 2 * math.pi * i / spokes
        a1 = a0 + math.pi / spokes
        s.draw_polyline([
            fitz.Point(cx, cy),
            fitz.Point(cx + radius * math.cos(a0), cy + radius * math.sin(a0)),
            fitz.Point(cx + radius * math.cos(a1), cy + radius * math.sin(a1)),
        ])
    s.finish(fill=BLACK, color=None, width=0)
    s.commit()
