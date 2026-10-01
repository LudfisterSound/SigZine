"""Sewing templates, hole positions and the binding instruction sheet."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import fitz

from . import binding as bindings
from . import draw
from .imposition import Plan
from .units import MM, Size


def hole_positions(binding_key: str, spine: float,
                   count: Optional[int] = None) -> List[float]:
    """Distances from the head, along a spine of length ``spine``."""
    b = bindings.get(binding_key)
    n = count or b.default_holes or 3
    kettle = min(15 * MM, spine * 0.08)
    key = b.sewing_template or binding_key

    if key in ("pamphlet3",):
        return [kettle, spine / 2, spine - kettle]
    if key in ("pamphlet5",):
        return [kettle, spine * 0.25 + kettle * 0.3, spine / 2,
                spine * 0.75 - kettle * 0.3, spine - kettle]
    if key in ("saddle",):
        n = max(2, n)
        span = spine - 2 * kettle * 1.6
        return [kettle * 1.6 + span * i / (n - 1) for i in range(n)]
    if key in ("sewn", "coptic", "longstitch"):
        n = max(3, n)
        inner = spine - 2 * kettle
        return [kettle + inner * i / (n - 1) for i in range(n)]
    if key in ("stab4",):
        # yotsume toji: four holes, outer pair one eighth in
        n = max(4, n)
        margin = spine / (2.0 * n)
        inner = spine - 2 * margin
        return [margin + inner * i / (n - 1) for i in range(n)]
    if key in ("sidestaple",):
        n = max(1, n)
        if n == 1:
            return [spine * 0.5]
        margin = spine * 0.22
        inner = spine - 2 * margin
        return [margin + inner * i / (n - 1) for i in range(n)]
    if key in ("screwpost",):
        n = max(2, n)
        margin = max(20 * MM, spine * 0.12)
        inner = spine - 2 * margin
        return [margin + inner * i / (n - 1) for i in range(n)]
    inner = spine - 2 * kettle
    n = max(2, n)
    return [kettle + inner * i / (n - 1) for i in range(n)]


def stab_offset(binding_key: str, page_width: float) -> float:
    """How far in from the spine edge the stab holes sit."""
    b = bindings.get(binding_key)
    if b.family == bindings.FUKUROTOJI:
        return min(15 * MM, page_width * 0.09)
    if b.sewing_template == "screwpost":
        return min(12 * MM, page_width * 0.08)
    if b.sewing_template == "sidestaple":
        return min(10 * MM, page_width * 0.07)
    return 0.0


def sewing_template(path, binding_key: str, page_size: Size,
                    sheet: Optional[Size] = None,
                    holes: Optional[int] = None,
                    signatures: int = 1) -> Path:
    """A punching template: full size, with the stations marked."""
    b = bindings.get(binding_key)
    sheet = sheet or Size(max(page_size.width + 40 * MM, 210 * MM),
                          max(page_size.height + 50 * MM, 297 * MM))
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W, H = sheet.width, sheet.height

    draw.text(page, 14 * MM, 14 * MM, f"PUNCHING TEMPLATE - {b.name}",
              size=12, font=draw.FONT_BOLD)
    draw.text(page, 14 * MM, 19 * MM,
              "Print at 100% with scaling off. Check the 100 mm bar before you "
              "punch anything.", size=7.5)

    # scale check bar
    bar = 100 * MM
    draw.line(page, 14 * MM, 25 * MM, 14 * MM + bar, 25 * MM, width=0.5)
    for i in range(11):
        x = 14 * MM + i * 10 * MM
        draw.line(page, x, 23.5 * MM, x, 26.5 * MM, width=0.4)
    draw.text(page, 14 * MM + bar + 4 * MM, 26.5 * MM, "100 mm", size=7)

    top = 36 * MM
    spine = page_size.height
    off = stab_offset(binding_key, page_size.width)
    x_page = (W - page_size.width) / 2.0
    box = (x_page, top, x_page + page_size.width, top + spine)
    if box[3] > H - 20 * MM:               # rotate the template to fit
        page.set_rotation(0)
        spine = min(spine, H - top - 20 * MM)
        box = (x_page, top, x_page + page_size.width, top + spine)
        draw.text(page, 14 * MM, H - 12 * MM,
                  "Page is taller than the sheet: the spine is shown cut short, "
                  "measure the stations from the head.", size=6.5)

    draw.rect(page, box, fill=None, color=(0.7,), width=0.4)
    draw.text(page, box[0], top - 3 * MM,
              f"page {page_size.describe()} - spine on the left", size=7)

    x_holes = box[0] + off
    if off > 0:
        draw.line(page, x_holes, box[1] - 4 * MM, x_holes, box[3] + 4 * MM,
                  color=(0.7,), width=0.3, dashes="[2 2] 0")
        draw.text(page, x_holes + 1.5 * MM, box[3] + 8 * MM,
                  f"stab line, {off / MM:.0f} mm in from the spine", size=6.5)

    positions = hole_positions(binding_key, spine, holes)
    for i, d in enumerate(positions):
        y = box[1] + d
        s = page.new_shape()
        s.draw_circle(fitz.Point(x_holes, y), 1.1 * MM)
        s.finish(color=draw.BLACK, width=0.4)
        s.commit()
        s2 = page.new_shape()
        s2.draw_line(fitz.Point(x_holes - 6 * MM, y), fitz.Point(x_holes - 2 * MM, y))
        s2.finish(color=draw.BLACK, width=0.3)
        s2.commit()
        draw.text(page, x_holes + 3.5 * MM, y + 2, f"{i + 1}   {d / MM:.1f} mm",
                  size=6.5)

    y0 = box[3] + 16 * MM
    draw.text(page, 14 * MM, y0, "Stations, measured from the head", size=9,
              font=draw.FONT_BOLD)
    y0 += 5 * MM
    draw.text(page, 14 * MM, y0,
              "   ".join(f"{i + 1}: {d / MM:.1f}" for i, d in enumerate(positions)),
              size=7.5, font=draw.FONT_MONO)
    y0 += 7 * MM
    if b.family == bindings.FOLDED:
        draw.text(page, 14 * MM, y0,
                  "Fold the signature first, then punch from the inside of the "
                  "fold with an awl held square to the paper.", size=7)
    elif b.family == bindings.FUKUROTOJI:
        draw.text(page, 14 * MM, y0,
                  "Clamp the whole block between boards and drill or punch "
                  "straight through all the leaves at once.", size=7)
    elif binding_key == "sidestaple":
        draw.text(page, 14 * MM, y0,
                  "These are staple positions, not holes. Jog the stack "
                  "square first and staple through the whole block at once.",
                  size=7)
    if signatures > 1:
        y0 += 5 * MM
        draw.text(page, 14 * MM, y0,
                  f"Punch all {signatures} signatures against this one template "
                  f"so the stations line up.", size=7)

    doc.save(str(path))
    doc.close()
    return Path(path)


def instruction_sheet(path, project, plan: Plan) -> Path:
    """One page telling you how to print it, fold it and bind it."""
    b = project.binding
    s = project.imposition
    sheet = Size(595.28, 841.89)          # A4 for the instructions themselves
    doc = fitz.open()
    page = doc.new_page(width=sheet.width, height=sheet.height)
    W = sheet.width
    m = 16 * MM
    y = 18 * MM

    draw.text(page, m, y, project.name or "Untitled", size=16,
              font=draw.FONT_BOLD)
    y += 6 * MM
    draw.text(page, m, y, f"{b.name} - {b.blurb}", size=8.5)
    y += 8 * MM

    def row(label: str, value: str) -> None:
        nonlocal y
        draw.text(page, m, y, label, size=8, font=draw.FONT_BOLD)
        draw.text(page, m + 45 * MM, y, value, size=8)
        y += 4.6 * MM

    draw.text(page, m, y, "THE NUMBERS", size=10, font=draw.FONT_BOLD)
    y += 5.5 * MM
    auto_blanks = sum(1 for it in project.pages if it.locked_blank)
    row("Pages", f"{plan.total_pages}" +
        (f"   ({auto_blanks} blank added automatically)" if auto_blanks else ""))
    row("Finished page", plan.page_size.describe())
    row("Sheet", f"{plan.sheet_size.describe()} "
                 f"({'landscape' if s.sheet_landscape else 'portrait'})")
    row("Sheets of paper", str(plan.sheet_count))
    row("Sides to print", str(len(plan.sheets)))
    if plan.family == bindings.FOLDED:
        row("Signatures", f"{len(plan.signatures)}  "
                          f"(sheets each: {', '.join(str(x) for x in plan.signatures)})")
        row("Folds per sheet", str(plan.folds))
    if s.creep_enabled and plan.family == bindings.FOLDED:
        row("Creep", f"{s.creep_per_sheet / MM:.2f} mm per sheet")
    from .imposition import duplex_driver_setting
    driver = duplex_driver_setting(s, plan.sheet_size)
    row("Duplex flip", f"{driver}  (in the print dialog)")
    if project.profile_name:
        row("Printer profile", project.profile_name)
    prof = getattr(project, "profile", None)
    if prof is not None and (prof.duplex_offset_x_mm or prof.duplex_offset_y_mm):
        row("Duplex correction",
            f"backs moved to match the fronts "
            f"({prof.duplex_offset_x_mm:+.1f}, {prof.duplex_offset_y_mm:+.1f}) mm "
            f"as measured")
    y += 3 * MM

    draw.text(page, m, y, "AT THE PRINTER", size=10, font=draw.FONT_BOLD)
    y += 5.5 * MM
    lines = [
        "Scaling must be 100% / Actual size. Never 'fit to page'.",
        "Turn off toner save, auto contrast and any photo enhancement.",
        f"Load {plan.sheet_size.describe()} paper "
        f"{'landscape' if s.sheet_landscape else 'portrait'}.",
    ]
    if not s.single_sided:
        lines.append(f"Print both sides, flipping on the {driver}. "
                     f"Run two sheets first and check the page numbers.")
    else:
        lines.append("Print one side only.")
    if project.profile_name:
        lines.append("The tone correction is already baked into this PDF - do "
                     "not apply another one in the driver.")
    for t in lines:
        draw.text(page, m, y, "- " + t, size=8)
        y += 4.4 * MM
    y += 3 * MM

    if plan.family == bindings.FOLDED and plan.folds:
        from .imposition import fold_instructions
        draw.text(page, m, y, "FOLDING", size=10, font=draw.FONT_BOLD)
        y += 5.5 * MM
        for i, t in enumerate(fold_instructions(plan.folds), 1):
            draw.text(page, m, y, f"{i}. {t}", size=8)
            y += 4.4 * MM
        y += 3 * MM

    draw.text(page, m, y, "BINDING", size=10, font=draw.FONT_BOLD)
    y += 5.5 * MM
    for i, t in enumerate(b.steps, 1):
        h = page.insert_textbox(fitz.Rect(m, y - 3, W - m, y + 20),
                                f"{i}. {t}", fontsize=8, fontname=draw.FONT,
                                align=fitz.TEXT_ALIGN_LEFT)
        y += 4.4 * MM * (1 if h >= 0 else 2)
    y += 2 * MM

    if b.sewing_template:
        holes = hole_positions(s.binding_key, plan.page_size.height,
                               b.default_holes)
        draw.text(page, m, y, "STATIONS", size=10, font=draw.FONT_BOLD)
        y += 5.5 * MM
        draw.text(page, m, y,
                  "From the head: " +
                  "   ".join(f"{d / MM:.1f} mm" for d in holes),
                  size=8, font=draw.FONT_MONO)
        y += 6 * MM

    if b.tips:
        draw.text(page, m, y, "NOTES", size=10, font=draw.FONT_BOLD)
        y += 5.5 * MM
        for t in b.tips:
            n = page.insert_textbox(fitz.Rect(m, y - 3, W - m, y + 24),
                                    "- " + t, fontsize=8, fontname=draw.FONT)
            y += 4.4 * MM * (1 if n >= 0 else 2)

    if plan.notes:
        y += 3 * MM
        draw.text(page, m, y, "FROM THE LAYOUT", size=10, font=draw.FONT_BOLD)
        y += 5.5 * MM
        for t in plan.notes:
            page.insert_textbox(fitz.Rect(m, y - 3, W - m, y + 24), "- " + t,
                                fontsize=8, fontname=draw.FONT)
            y += 4.4 * MM

    draw.text(page, m, sheet.height - 12 * MM,
              "Made with Signature Zine", size=7, color=(0.5,))
    doc.save(str(path))
    doc.close()
    return Path(path)
