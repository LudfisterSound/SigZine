"""Turning a plan into PDFs: the print run, the digital copy and the dummy."""
from __future__ import annotations

import io
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import fitz
import numpy as np
from PIL import Image

from . import binding as bindings
from . import draw
from .imposition import (Plan, Slot, SheetPlan, duplex_axis,
                         duplex_correction, imposition_area, order_sheets,
                         shift_sheet)
from .sources import PageItem, Source
from .tone import ToneSettings, apply_tone
from .units import MM, Size

Rect = Tuple[float, float, float, float]
Progress = Optional[Callable[[int, int, str], None]]


class Cancelled(Exception):
    pass


def _rect(r: Rect) -> fitz.Rect:
    return fitz.Rect(*r)


def place_geometry(src_size: Size, target: Rect, mode: str, rotation: int,
                   scale: float = 1.0, off_x: float = 0.0, off_y: float = 0.0
                   ) -> Tuple[Rect, Optional[Tuple[float, float, float, float]]]:
    """Work out where the source goes and how much of it is used.

    Returns (destination rect on the sheet, crop fractions in source space
    as (fx0, fy0, fx1, fy1) or None for the whole page).
    """
    tw, th = target[2] - target[0], target[3] - target[1]
    swapped = rotation % 180 == 90
    ew = src_size.height if swapped else src_size.width
    eh = src_size.width if swapped else src_size.height
    if ew <= 0 or eh <= 0 or tw <= 0 or th <= 0:
        return target, None

    if mode == "stretch":
        return target, None
    if mode == "fill":
        s = max(tw / ew, th / eh)
    elif mode == "actual":
        s = 1.0
    else:
        s = min(tw / ew, th / eh)
    s *= max(0.01, scale)

    dw, dh = ew * s, eh * s
    fx = min(1.0, tw / dw) if dw > 0 else 1.0
    fy = min(1.0, th / dh) if dh > 0 else 1.0
    dw, dh = dw * fx, dh * fy

    cx = (target[0] + target[2]) / 2.0 + off_x
    cy = (target[1] + target[3]) / 2.0 + off_y
    dest = (cx - dw / 2, cy - dh / 2, cx + dw / 2, cy + dh / 2)

    if fx >= 0.9999 and fy >= 0.9999:
        return dest, None
    # crop fractions, expressed in the unrotated source frame
    cfx, cfy = (fy, fx) if swapped else (fx, fy)
    x0 = (1 - cfx) / 2.0
    y0 = (1 - cfy) / 2.0
    return dest, (x0, y0, x0 + cfx, y0 + cfy)


class Renderer:
    def __init__(self, project) -> None:
        self.project = project
        self._image_cache: Dict[tuple, bytes] = {}
        self.cancel = False

    # -- helpers ----------------------------------------------------------
    def _check(self) -> None:
        if self.cancel:
            raise Cancelled()

    def _item(self, page_number: Optional[int]) -> Optional[PageItem]:
        if page_number is None:
            return None
        i = page_number - 1
        if 0 <= i < len(self.project.pages):
            return self.project.pages[i]
        return None

    def _should_rasterise(self, item: PageItem, src: Source) -> bool:
        if src.kind == "image":
            return True
        if src.kind == "blank":
            return False
        if not item.tone_enabled:
            return False
        if item.tone or item.tone_preset:
            return True
        return bool(self.project.output.rasterise_pdf_sources)

    def _target_dpi(self, tone: ToneSettings) -> float:
        out = self.project.output
        if tone.screen not in ("none", "", None):
            return float(tone.device_dpi or out.print_dpi)
        return float(out.contone_dpi or tone.render_dpi)

    def _rendered_bytes(self, item: PageItem, src: Source, dest: Rect,
                        crop, tone: ToneSettings, for_screen: bool) -> bytes:
        dpi = self._target_dpi(tone) if not for_screen else \
            float(self.project.digital.dpi)
        w_px = max(1, int(round((dest[2] - dest[0]) / 72.0 * dpi)))
        h_px = max(1, int(round((dest[3] - dest[1]) / 72.0 * dpi)))
        key = (item.source_id, item.source_page, w_px, h_px, item.rotation,
               crop, repr(tone), for_screen, id(self.project.profile))
        hit = self._image_cache.get(key)
        if hit is not None:
            return hit

        if src.kind == "image":
            img = src.pil_page(item.source_page,
                               max_px=int(max(w_px, h_px) * 2.2))
        else:
            base_dpi = min(600.0, max(dpi, 72.0))
            img = src.render(item.source_page, dpi=base_dpi, tone=None,
                             for_screen=False,
                             max_px=int(max(w_px, h_px) * 1.4))
        if img is None:
            return b""
        if crop:
            W, H = img.size
            box = (int(crop[0] * W), int(crop[1] * H),
                   max(1, int(crop[2] * W)), max(1, int(crop[3] * H)))
            img = img.crop(box)
        if item.rotation:
            img = img.rotate(-item.rotation, expand=True)
            w_px, h_px = (w_px, h_px)
        out = apply_tone(img, tone, self.project.profile,
                         target_px=(w_px, h_px), for_screen=for_screen)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(self._image_cache) > 120:
            self._image_cache.clear()
        self._image_cache[key] = data
        return data

    # -- placing one page --------------------------------------------------
    def place_page(self, page: fitz.Page, page_number: Optional[int],
                   target: Rect, rotation: int, for_screen: bool = False) -> None:
        item = self._item(page_number)
        if item is None or item.is_blank():
            return
        src = self.project.library.get(item.source_id)
        if src is None:
            return
        if src.kind == "text" and src._doc is None:
            src.build_text(self.project.page_size(),
                           self.project.text_margins(),
                           self.project.text_style)
        if src.kind in ("pdf", "text") and src._doc is None:
            src.refresh()

        size = src.page_size(item.source_page)
        if size is None:
            if src.kind == "image":
                img = src.pil_page(item.source_page, max_px=512)
                if img is None:
                    return
                size = Size(float(img.width), float(img.height))
            else:
                return
        total_rot = (rotation + item.rotation) % 360
        mode = item.fit if item.fit != "auto" else "fit"
        dest, crop = place_geometry(size, target, mode, total_rot, item.scale,
                                    item.offset_x, item.offset_y)
        tone = self.project.tone_for(item)
        if for_screen:
            tone = tone.copy()
            tone.screen = "none"
            if self.project.digital.tone_preset:
                from .tone import apply_preset
                tone = apply_preset(tone, self.project.digital.tone_preset)

        if self._should_rasterise(item, src) or src.kind == "image":
            data = self._rendered_bytes(item, src, dest,
                                        crop if src.kind == "image" else crop,
                                        tone, for_screen)
            if not data:
                return
            page.insert_image(_rect(dest), stream=data, keep_proportion=False,
                              rotate=0 if src.kind == "image" else 0)
            return

        # vector pass-through
        clip = None
        if crop and src._doc is not None:
            pr = src._doc[item.source_page].rect
            clip = fitz.Rect(pr.x0 + crop[0] * pr.width,
                             pr.y0 + crop[1] * pr.height,
                             pr.x0 + crop[2] * pr.width,
                             pr.y0 + crop[3] * pr.height)
        page.show_pdf_page(_rect(dest), src._doc, item.source_page,
                           clip=clip, rotate=total_rot, keep_proportion=True)

    # -- duplex registration ----------------------------------------------
    def back_offset(self, plan: Plan) -> Tuple[float, float]:
        """How far to move the back of every sheet, from the printer profile."""
        prof = self.project.profile
        if prof is None:
            return (0.0, 0.0)
        rx = float(getattr(prof, "duplex_offset_x_mm", 0.0) or 0.0) * MM
        ry = float(getattr(prof, "duplex_offset_y_mm", 0.0) or 0.0) * MM
        if rx == 0.0 and ry == 0.0:
            return (0.0, 0.0)
        axis = duplex_axis(self.project.imposition, plan.sheet_size)
        return duplex_correction(rx, ry, axis)

    def _registered(self, sp: SheetPlan, plan: Plan) -> SheetPlan:
        if sp.side != "back":
            return sp
        dx, dy = self.back_offset(plan)
        return shift_sheet(sp, dx, dy)

    # -- marks -------------------------------------------------------------
    def _waste(self) -> float:
        return self.project.imposition.sheet_margin

    def draw_marks(self, page: fitz.Page, sp: SheetPlan, plan: Plan) -> None:
        s = self.project.imposition
        waste = self._waste()
        sheet = plan.sheet_size

        if s.crop_marks and waste >= 2 * MM:
            done = set()
            for slot in sp.slots:
                key = tuple(round(v, 2) for v in slot.trim_rect)
                if key in done:
                    continue
                done.add(key)
                draw.crop_marks(page, slot.trim_rect,
                                length=min(4 * MM, waste * 0.8),
                                offset=min(1.5 * MM, waste * 0.3), width=0.25)
        if s.fold_marks:
            for (x0, y0, x1, y1, kind) in sp.fold_lines:
                if waste >= 2 * MM:
                    draw.fold_mark(page, x0, y0, x1, y1,
                                   bleed=min(waste * 0.8, 4 * MM))
                else:
                    draw.fold_mark(page, x0, y0, x1, y1, bleed=2.5 * MM)
        for (x0, y0, x1, y1, kind) in sp.cut_lines:
            draw.line(page, x0, y0, x1, y1, width=0.3, dashes="[3 2] 0")
            mid = ((x0 + x1) / 2, (y0 + y1) / 2)
            draw.text(page, mid[0], mid[1] - 2, "cut", size=5, align="center")

        if s.registration_marks and waste >= 5 * MM:
            r = min(2.5 * MM, waste * 0.45)
            for (x, y) in ((waste / 2, waste / 2),
                           (sheet.width - waste / 2, waste / 2),
                           (waste / 2, sheet.height - waste / 2),
                           (sheet.width - waste / 2, sheet.height - waste / 2)):
                draw.registration_target(page, x, y, r, width=0.25)

        if s.collation_marks and waste >= 4 * MM and \
                plan.family == bindings.FOLDED and len(plan.signatures) > 1:
            span = min(60 * MM, sheet.height * 0.5)
            x = sheet.width - waste * 0.85
            draw.collation_mark(page, x, (sheet.height - span) / 2,
                                sp.signature - 1, len(plan.signatures),
                                height=span, width_=waste * 0.55)

        if s.sheet_slugs and waste >= 4 * MM:
            order = ", ".join(str(sl.page) for sl in sp.slots if sl.page)
            txt = (f"{self.project.name}  |  {sp.label}  |  pages {order}"
                   f"  |  {time.strftime('%Y-%m-%d %H:%M')}")
            draw.text(page, waste + 2 * MM, sheet.height - waste * 0.25, txt,
                      size=min(5.5, waste * 0.5), color=(0.35,))

    # -- the print run -----------------------------------------------------
    def render_print(self, out_path, plan: Optional[Plan] = None,
                     mode: Optional[str] = None, progress: Progress = None
                     ) -> List[Path]:
        self.cancel = False
        p = self.project
        plan = plan or p.build_plan()
        mode = mode or p.output.duplex_mode
        out_path = Path(out_path)

        runs: List[Tuple[str, List[SheetPlan]]] = []
        if mode == "separate":
            runs.append(("fronts", order_sheets(plan, "fronts")))
            backs = order_sheets(plan, "backs")
            if backs:
                runs.append(("backs", backs))
        else:
            runs.append(("", order_sheets(plan, mode)))

        written: List[Path] = []
        total = sum(len(r[1]) for r in runs) or 1
        done = 0
        for suffix, sheets in runs:
            if not sheets:
                continue
            doc = fitz.open()
            for sp in sheets:
                self._check()
                sp = self._registered(sp, plan)
                page = doc.new_page(width=plan.sheet_size.width,
                                    height=plan.sheet_size.height)
                for slot in sp.slots:
                    self._check()
                    self.place_page(page, slot.page, slot.trim_rect,
                                    slot.rotation)
                self.draw_marks(page, sp, plan)
                done += 1
                if progress:
                    progress(done, total, f"{sp.label}")
            path = out_path if not suffix else \
                out_path.with_name(f"{out_path.stem} - {suffix}{out_path.suffix}")
            doc.set_metadata({
                "title": f"{p.name} - print run",
                "producer": "Signature Zine",
                "subject": f"{p.binding.name}, {plan.sheet_size.describe()}",
            })
            doc.save(str(path), garbage=3, deflate=True)
            doc.close()
            written.append(path)
        return written

    # -- digital copy ------------------------------------------------------
    def render_digital(self, out_path, progress: Progress = None) -> Path:
        self.cancel = False
        p = self.project
        d = p.digital
        page_size = p.page_size()
        pages = [(i + 1, it) for i, it in enumerate(p.pages)
                 if d.include_blanks or not it.is_blank()]
        doc = fitz.open()
        spreads = d.layout in ("spreads", "both")

        def add_single():
            for n, (num, item) in enumerate(pages):
                self._check()
                page = doc.new_page(width=page_size.width,
                                    height=page_size.height)
                self.place_page(page, num, (0, 0, page_size.width,
                                            page_size.height), 0,
                                for_screen=True)
                if progress:
                    progress(n + 1, len(pages), f"page {num}")

        def add_spreads():
            w2 = page_size.width * 2
            seq = list(pages)
            if d.cover_alone and seq:
                page = doc.new_page(width=w2, height=page_size.height)
                self.place_page(page, seq[0][0],
                                (page_size.width / 2, 0,
                                 page_size.width * 1.5, page_size.height), 0,
                                for_screen=True)
                seq = seq[1:]
            for i in range(0, len(seq), 2):
                self._check()
                page = doc.new_page(width=w2, height=page_size.height)
                left = seq[i]
                self.place_page(page, left[0],
                                (0, 0, page_size.width, page_size.height), 0,
                                for_screen=True)
                if i + 1 < len(seq):
                    self.place_page(page, seq[i + 1][0],
                                    (page_size.width, 0, w2, page_size.height),
                                    0, for_screen=True)
                if progress:
                    progress(i + 1, len(seq), "spread")

        if d.layout == "spreads":
            add_spreads()
        elif d.layout == "both":
            add_single()
            add_spreads()
        else:
            add_single()

        if d.bookmarks:
            toc = []
            seen = set()
            for idx, (num, item) in enumerate(pages):
                if item.source_id in seen or item.is_blank():
                    continue
                seen.add(item.source_id)
                src = p.library.get(item.source_id)
                if src:
                    toc.append([1, src.title, idx + 1])
            if toc:
                try:
                    doc.set_toc(toc)
                except Exception:
                    pass
        doc.set_metadata({"title": p.name, "producer": "Signature Zine",
                          "subject": "digital edition"})
        out_path = Path(out_path)
        doc.save(str(out_path), garbage=3, deflate=True)
        doc.close()
        return out_path

    # -- folding dummy -----------------------------------------------------
    def render_dummy(self, out_path, plan: Optional[Plan] = None) -> Path:
        p = self.project
        plan = plan or p.build_plan()
        doc = fitz.open()
        for sp in plan.sheets:
            sp = self._registered(sp, plan)
            page = doc.new_page(width=plan.sheet_size.width,
                                height=plan.sheet_size.height)
            for slot in sp.slots:
                r = slot.trim_rect
                draw.rect(page, r, fill=None, color=(0.75,), width=0.4)
                label = str(slot.page) if slot.page else "-"
                size = min(r[2] - r[0], r[3] - r[1]) * 0.35
                box = (r[0], (r[1] + r[3]) / 2 - size, r[2],
                       (r[1] + r[3]) / 2 + size)
                draw.centered_text(page, box, label, size=size,
                                   font=draw.FONT_BOLD,
                                   rotate=slot.rotation % 360)
                # a bar across the head of the page, in page-local terms
                th = min(r[2] - r[0], r[3] - r[1]) * 0.03
                if slot.rotation % 360 == 0:
                    bar = (r[0], r[1], r[2], r[1] + th)
                elif slot.rotation % 360 == 180:
                    bar = (r[0], r[3] - th, r[2], r[3])
                elif slot.rotation % 360 == 90:
                    bar = (r[2] - th, r[1], r[2], r[3])
                else:
                    bar = (r[0], r[1], r[0] + th, r[3])
                draw.rect(page, bar, fill=(0.8,), color=None, width=0)
            self.draw_marks(page, sp, plan)
            draw.text(page, 3 * MM, 4 * MM,
                      f"DUMMY - {sp.label} - fold and check the order",
                      size=7, color=(0.4,))
        out_path = Path(out_path)
        doc.save(str(out_path))
        doc.close()
        return out_path

    # -- on screen preview -------------------------------------------------
    def preview_sheet(self, plan: Plan, index: int, dpi: float = 72.0,
                      fast: bool = True, simulate: bool = False
                      ) -> Optional[Image.Image]:
        if not plan.sheets:
            return None
        index = max(0, min(index, len(plan.sheets) - 1))
        sp = self._registered(plan.sheets[index], plan)
        doc = fitz.open()
        page = doc.new_page(width=plan.sheet_size.width,
                            height=plan.sheet_size.height)
        old = self.project.output.contone_dpi
        if fast:
            # a preview only ever reaches the screen: device resolution here
            # buys nothing and costs a lot
            self.project.output.contone_dpi = max(72, min(old, int(dpi)))
        try:
            for slot in sp.slots:
                self.place_page(page, slot.page, slot.trim_rect, slot.rotation)
            self.draw_marks(page, sp, plan)
            pm = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
            if simulate:
                from .tone import simulate_print
                img = simulate_print(img, self.project.tone,
                                     self.project.profile,
                                     spread=max(0.0, dpi / 300.0)).convert("RGB")
        finally:
            self.project.output.contone_dpi = old
            doc.close()
        return img

    def invalidate(self) -> None:
        self._image_cache.clear()
