"""Units, paper sizes and small geometry helpers.

Everything inside the application is stored in PostScript points (1/72").
Users see millimetres or inches depending on the document preference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

PT = 1.0
MM = 72.0 / 25.4
CM = 10.0 * MM
INCH = 72.0


def mm(value: float) -> float:
    return value * MM


def inch(value: float) -> float:
    return value * INCH


def to_mm(points: float) -> float:
    return points / MM


def to_inch(points: float) -> float:
    return points / INCH


def parse_length(text: str, default_unit: str = "mm") -> float:
    """Parse '10mm', '0.5in', '36pt', '1.2cm' or a bare number into points."""
    text = str(text).strip().lower().replace(" ", "")
    for suffix, factor in (("mm", MM), ("cm", CM), ("in", INCH), ('"', INCH),
                           ("pt", PT), ("px", PT)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    factor = {"mm": MM, "cm": CM, "in": INCH, "pt": PT}[default_unit]
    return float(text) * factor


def format_length(points: float, unit: str = "mm", digits: int = 1) -> str:
    factor = {"mm": MM, "cm": CM, "in": INCH, "pt": PT}[unit]
    return f"{points / factor:.{digits}f} {unit}"


@dataclass(frozen=True)
class Size:
    width: float
    height: float

    def landscape(self) -> "Size":
        return Size(max(self.width, self.height), min(self.width, self.height))

    def portrait(self) -> "Size":
        return Size(min(self.width, self.height), max(self.width, self.height))

    def rotated(self) -> "Size":
        return Size(self.height, self.width)

    def scaled(self, factor: float) -> "Size":
        return Size(self.width * factor, self.height * factor)

    def as_tuple(self) -> Tuple[float, float]:
        return (self.width, self.height)

    def half_width(self) -> "Size":
        return Size(self.width / 2.0, self.height)

    def quarter(self) -> "Size":
        return Size(self.width / 2.0, self.height / 2.0)

    def describe(self, unit: str = "mm") -> str:
        return f"{format_length(self.width, unit, 0)} x {format_length(self.height, unit, 0)}"


# --- Stock sheet sizes (portrait orientation) -------------------------------

PAPER_SIZES: Dict[str, Size] = {
    "Letter": Size(inch(8.5), inch(11)),
    "Legal": Size(inch(8.5), inch(14)),
    "Tabloid": Size(inch(11), inch(17)),
    "Executive": Size(inch(7.25), inch(10.5)),
    "Statement": Size(inch(5.5), inch(8.5)),
    "A3": Size(mm(297), mm(420)),
    "A4": Size(mm(210), mm(297)),
    "A5": Size(mm(148), mm(210)),
    "A6": Size(mm(105), mm(148)),
    "A7": Size(mm(74), mm(105)),
    "B4": Size(mm(250), mm(353)),
    "B5": Size(mm(176), mm(250)),
    "B6": Size(mm(125), mm(176)),
    "Square 8in": Size(inch(8), inch(8)),
    "Square 140mm": Size(mm(140), mm(140)),
    "Digest 5.5x8.5": Size(inch(5.5), inch(8.5)),
    "Half Letter": Size(inch(5.5), inch(8.5)),
    "Quarter Letter": Size(inch(4.25), inch(5.5)),
    "Mini 2.75x4.25": Size(inch(2.75), inch(4.25)),
    "Pocket 4x6": Size(inch(4), inch(6)),
}

# Sheets that a desktop laser printer will normally accept.
PRINTABLE_SHEETS: List[str] = [
    "Letter", "Legal", "Tabloid", "A4", "A3", "B4", "B5", "Executive", "Statement",
]


def paper(name: str) -> Size:
    return PAPER_SIZES[name]


def fit_rect(content: Size, box: Tuple[float, float, float, float],
             mode: str = "fit") -> Tuple[float, float, float, float]:
    """Fit ``content`` into ``box`` (x0, y0, x1, y1).

    mode: 'fit' (contain), 'fill' (cover, centred), 'stretch', 'actual'.
    Returns the placement rectangle, which may exceed the box for 'fill'.
    """
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if content.width <= 0 or content.height <= 0:
        return box
    if mode == "stretch":
        return box
    if mode == "actual":
        scale = 1.0
    else:
        sx, sy = bw / content.width, bh / content.height
        scale = min(sx, sy) if mode == "fit" else max(sx, sy)
    w, h = content.width * scale, content.height * scale
    cx, cy = x0 + bw / 2.0, y0 + bh / 2.0
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
