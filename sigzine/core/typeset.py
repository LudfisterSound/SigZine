"""A small text typesetter, so plain text and light markdown can become pages.

Greedy line breaking with optional justification, using the PDF base-14
fonts so nothing has to be embedded or installed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import fitz

from .units import MM, Size

BASE_FONTS = {
    "Times": ("tiro", "tibo", "tiit", "tibi"),
    "Helvetica": ("helv", "hebo", "heit", "hebi"),
    "Courier": ("cour", "cobo", "coit", "cobi"),
}


@dataclass
class TextStyle:
    family: str = "Times"
    size: float = 10.0
    leading: float = 1.38
    align: str = "justify"          # left | center | right | justify
    paragraph_indent: float = 0.0
    paragraph_space: float = 0.35   # in multiples of the line height
    heading_scale: float = 1.6
    page_numbers: bool = True
    first_page_number: int = 1
    hyphenate: bool = False

    def font_names(self) -> Tuple[str, str, str, str]:
        return BASE_FONTS.get(self.family, BASE_FONTS["Times"])


@dataclass
class Block:
    kind: str          # 'p' | 'h1' | 'h2' | 'quote' | 'break' | 'rule'
    text: str = ""


def parse_blocks(text: str) -> List[Block]:
    blocks: List[Block] = []
    buf: List[str] = []

    def flush(kind: str = "p"):
        if buf:
            blocks.append(Block(kind, " ".join(x.strip() for x in buf).strip()))
            buf.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped in ("---", "***", "\f") or stripped.lower() == "[newpage]":
            flush()
            blocks.append(Block("break"))
            continue
        if stripped.startswith("## "):
            flush()
            blocks.append(Block("h2", stripped[3:].strip()))
            continue
        if stripped.startswith("# "):
            flush()
            blocks.append(Block("h1", stripped[2:].strip()))
            continue
        if stripped.startswith("> "):
            flush()
            blocks.append(Block("quote", stripped[2:].strip()))
            continue
        buf.append(line)
    flush()
    return blocks


def _wrap(font: fitz.Font, size: float, text: str, width: float) -> List[List[str]]:
    words = text.split()
    lines: List[List[str]] = []
    cur: List[str] = []
    for w in words:
        trial = (" ".join(cur + [w])) if cur else w
        if font.text_length(trial, size) <= width or not cur:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    if cur:
        lines.append(cur)
    return lines


def typeset(text: str, page_size: Size, margins: Tuple[float, float, float, float],
            style: Optional[TextStyle] = None,
            title: str = "") -> fitz.Document:
    """Flow ``text`` into a new PDF. margins = (top, bottom, inner, outer)."""
    style = style or TextStyle()
    reg, bold, ital, _bi = style.font_names()
    f_reg = fitz.Font(reg)
    f_bold = fitz.Font(bold)
    f_ital = fitz.Font(ital)
    top, bottom, inner, outer = margins

    doc = fitz.open()
    blocks = parse_blocks(text)
    line_h = style.size * style.leading

    page = None
    writer = None
    y = 0.0
    page_no = style.first_page_number
    recto = True

    def new_page():
        nonlocal page, writer, y, recto, page_no
        if writer is not None and page is not None:
            writer.write_text(page)
            if style.page_numbers:
                _page_number(page, page_no, page_size, margins, f_reg, style)
            page_no += 1
        page = doc.new_page(width=page_size.width, height=page_size.height)
        writer = fitz.TextWriter(page.rect)
        recto = (doc.page_count % 2 == 1)
        y = top + style.size

    def left_right() -> Tuple[float, float]:
        if recto:
            return (inner, page_size.width - outer)
        return (outer, page_size.width - inner)

    new_page()
    bottom_limit = page_size.height - bottom

    def place(words: Sequence[str], font: fitz.Font, size: float,
              align: str, x0: float, x1: float, last: bool) -> None:
        nonlocal y
        if not words:
            return
        width = x1 - x0
        natural = font.text_length(" ".join(words), size)
        if align == "justify" and not last and len(words) > 1:
            extra = (width - natural) / (len(words) - 1)
            x = x0
            for w in words:
                writer.append(fitz.Point(x, y), w, font=font, fontsize=size)
                x += font.text_length(w + " ", size) + extra
        else:
            if align == "center":
                x = x0 + (width - natural) / 2.0
            elif align == "right":
                x = x1 - natural
            else:
                x = x0
            writer.append(fitz.Point(x, y), " ".join(words), font=font,
                          fontsize=size)

    for block in blocks:
        if block.kind == "break":
            new_page()
            continue
        if block.kind == "h1":
            font, size, align = f_bold, style.size * style.heading_scale, "left"
        elif block.kind == "h2":
            font, size, align = f_bold, style.size * 1.22, "left"
        elif block.kind == "quote":
            font, size, align = f_ital, style.size, "left"
        else:
            font, size, align = f_reg, style.size, style.align
        lh = size * style.leading
        x0, x1 = left_right()
        if block.kind == "quote":
            x0 += 8 * MM
            x1 -= 8 * MM
        if block.kind in ("h1", "h2") and y > top + style.size * 2:
            y += lh * 0.5
        lines = _wrap(font, size, block.text, x1 - x0)
        for i, words in enumerate(lines):
            if y > bottom_limit:
                new_page()
                x0, x1 = left_right()
                if block.kind == "quote":
                    x0 += 8 * MM
                    x1 -= 8 * MM
            place(words, font, size, align, x0, x1, last=(i == len(lines) - 1))
            y += lh
        y += lh * style.paragraph_space

    if writer is not None and page is not None:
        writer.write_text(page)
        if style.page_numbers:
            _page_number(page, page_no, page_size, margins, f_reg, style)
    if title:
        doc.set_metadata({"title": title})
    return doc


def _page_number(page: fitz.Page, number: int, page_size: Size,
                 margins: Tuple[float, float, float, float],
                 font: fitz.Font, style: TextStyle) -> None:
    top, bottom, inner, outer = margins
    y = page_size.height - bottom * 0.45
    s = str(number)
    w = font.text_length(s, style.size * 0.85)
    x = (page_size.width - w) / 2.0
    tw = fitz.TextWriter(page.rect)
    tw.append(fitz.Point(x, y), s, font=font, fontsize=style.size * 0.85)
    tw.write_text(page)
