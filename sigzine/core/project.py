"""The document model: sources, page order, layout and output settings."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import binding as bindings
from .calibration import PrinterProfile, list_profiles
from .imposition import ImpositionSettings, Plan, build_plan, padded_page_count
from .sources import Library, PageItem, Source
from .tone import PRESETS, ToneSettings, apply_preset
from .typeset import TextStyle
from .units import MM, PAPER_SIZES, Size, paper

PROJECT_EXT = ".sigzine"


@dataclass
class DocumentPreset:
    """A starting point: what you are making, and the settings that imply."""
    key: str
    mode: str                       # zine | book
    title: str
    blurb: str
    binding: str
    sheet: str = "Letter"
    landscape: bool = True
    folds: int = 1
    sheets_per_signature: int = 4
    up: int = 2
    single_sided: bool = False
    sheet_margin_mm: float = 0.0

    def page_size_hint(self) -> Size:
        base = paper(self.sheet)
        base = base.landscape() if self.landscape else base.portrait()
        if self.binding == "mini8":
            cols, rows = 4, 2
        elif bindings.get(self.binding).family == bindings.FOLDED:
            cols = 2 ** math.ceil(self.folds / 2)
            rows = 2 ** (self.folds // 2)
        elif bindings.get(self.binding).family == bindings.FUKUROTOJI:
            cols, rows = 2, 1
        else:
            cols, rows = (self.up, 1) if self.up <= 2 else (2, self.up // 2)
        m = self.sheet_margin_mm * MM
        return Size((base.width - 2 * m) / cols, (base.height - 2 * m) / rows)

    def describe(self) -> str:
        p = self.page_size_hint()
        pages = ""
        b = bindings.get(self.binding)
        if b.family == bindings.FOLDED:
            pages = f"{2 ** self.folds * 2} pages a sheet"
        elif b.family == bindings.MINI8:
            pages = "8 pages from one sheet"
        elif b.family == bindings.FUKUROTOJI:
            pages = "2 pages a sheet, single sided"
        else:
            pages = f"{self.up * (1 if self.single_sided else 2)} pages a sheet"
        return f"{p.describe()} · {self.sheet} · {pages}"


DOCUMENT_PRESETS: List[DocumentPreset] = [
    DocumentPreset(
        "half-letter", "zine", "Half-letter, saddle stitched",
        "The standard photocopied zine: a letter sheet folded once and "
        "stapled through the fold.",
        binding="saddle", sheet="Letter", landscape=True, folds=1,
        sheets_per_signature=4),
    DocumentPreset(
        "a5-zine", "zine", "A5, saddle stitched",
        "The same thing in A sizes: A4 folded to A5.",
        binding="saddle", sheet="A4", landscape=True, folds=1,
        sheets_per_signature=4),
    DocumentPreset(
        "quarter-letter", "zine", "Quarter-letter, saddle stitched",
        "Pocket sized. Each sheet is folded twice and holds eight pages.",
        binding="saddle", sheet="Letter", landscape=False, folds=2,
        sheets_per_signature=3),
    DocumentPreset(
        "mini8", "zine", "Eight-page mini zine",
        "One sheet, one cut, no staples. Fold it and it is finished.",
        binding="mini8", sheet="Letter", landscape=True, single_sided=True),
    DocumentPreset(
        "side-stapled", "zine", "Side stapled, full page",
        "Full-size single leaves stapled down the left edge. No folding, no "
        "imposition to get wrong.",
        binding="sidestaple", sheet="Letter", landscape=False, up=1),
    DocumentPreset(
        "digest-sewn", "book", "Digest, sewn signatures",
        "5.5 x 8.5 inch pages in sewn sections. The everyday hand-bound book.",
        binding="sewn", sheet="Letter", landscape=True, folds=1,
        sheets_per_signature=4, sheet_margin_mm=6),
    DocumentPreset(
        "a5-sewn", "book", "A5, sewn signatures",
        "A4 folded to A5, sewn in sections of four sheets.",
        binding="sewn", sheet="A4", landscape=True, folds=1,
        sheets_per_signature=4, sheet_margin_mm=6),
    DocumentPreset(
        "a6-quarto", "book", "A6, quarto signatures",
        "Small format: A4 folded twice, sixteen pages to the sheet.",
        binding="sewn", sheet="A4", landscape=False, folds=2,
        sheets_per_signature=2, sheet_margin_mm=5),
    DocumentPreset(
        "coptic", "book", "Coptic, opens flat",
        "Sewn sections with an exposed chain stitch. Good for sketchbooks and "
        "anything with pictures across the gutter.",
        binding="coptic", sheet="Letter", landscape=True, folds=1,
        sheets_per_signature=4, sheet_margin_mm=6),
    DocumentPreset(
        "perfect", "book", "Perfect bound",
        "Single leaves, cut two-up and glued into a wrapper.",
        binding="perfect", sheet="Letter", landscape=True, up=2,
        sheet_margin_mm=5),
    DocumentPreset(
        "stab", "book", "Japanese stab binding",
        "Single-sided sheets folded at the fore edge and sewn through the "
        "open edge.",
        binding="stab", sheet="Letter", landscape=True, single_sided=True,
        sheet_margin_mm=5),
]


def presets_for(mode: str) -> List[DocumentPreset]:
    return [p for p in DOCUMENT_PRESETS if p.mode == mode]


def preset(key: str) -> Optional[DocumentPreset]:
    return next((p for p in DOCUMENT_PRESETS if p.key == key), None)


@dataclass
class DigitalSettings:
    enabled: bool = True
    layout: str = "single"          # single | spreads | both
    cover_alone: bool = True        # first page on its own in spread view
    dpi: int = 150
    tone_preset: str = "Screen / digital copy"
    rasterise: bool = False         # keep vectors unless asked
    include_blanks: bool = False
    bookmarks: bool = True


@dataclass
class OutputSettings:
    duplex_mode: str = "duplex"     # duplex | fronts | backs | separate
    include_instructions: bool = True
    include_templates: bool = True
    include_dummy: bool = False
    rasterise_pdf_sources: bool = False
    print_dpi: int = 600
    contone_dpi: int = 300


class Project:
    def __init__(self) -> None:
        self.name = "Untitled"
        self.mode = "zine"                     # zine | book
        self.library = Library()
        self.pages: List[PageItem] = []
        self.imposition = ImpositionSettings()
        self.tone = ToneSettings()
        self.text_style = TextStyle()
        self.digital = DigitalSettings()
        self.output = OutputSettings()
        self.profile: Optional[PrinterProfile] = None
        self.profile_name: str = ""
        self.auto_pad = True
        self.start_sources_on_recto = False
        self.path: Optional[Path] = None
        self.preset_key: str = ""
        self.dirty = False
        self.apply_mode_defaults("zine")

    # -- presets ----------------------------------------------------------
    def apply_mode_defaults(self, mode: str) -> None:
        self.mode = mode
        s = self.imposition
        if mode == "zine":
            s.binding_key = "saddle"
            s.sheet_size = paper("Letter")
            s.sheet_landscape = True
            s.folds_per_sheet = 1
            s.sheets_per_signature = 4
            s.margin_inner = 10 * MM
            s.margin_outer = 8 * MM
            s.margin_top = 8 * MM
            s.margin_bottom = 10 * MM
            s.sheet_margin = 0.0
            self.digital.enabled = True
            self.tone = apply_preset(self.tone, "Laser photo (default)")
        else:
            s.binding_key = "sewn"
            s.sheet_size = paper("Letter")
            s.sheet_landscape = True
            s.folds_per_sheet = 1
            s.sheets_per_signature = 4
            s.margin_inner = 14 * MM
            s.margin_outer = 10 * MM
            s.margin_top = 12 * MM
            s.margin_bottom = 14 * MM
            s.sheet_margin = 6 * MM
            self.digital.enabled = False
            self.tone = apply_preset(self.tone, "Laser photo (default)")
        self.dirty = True

    def apply_document_preset(self, preset_key: str) -> None:
        """Start from one of the ready-made document types."""
        pre = preset(preset_key)
        if pre is None:
            return
        self.apply_mode_defaults(pre.mode)
        self.set_binding(pre.binding)
        s = self.imposition
        s.sheet_size = paper(pre.sheet)
        s.sheet_landscape = pre.landscape
        s.folds_per_sheet = pre.folds
        s.sheets_per_signature = pre.sheets_per_signature
        s.up = pre.up
        s.sheet_margin = pre.sheet_margin_mm * MM
        if pre.single_sided:
            s.single_sided = True
        self.preset_key = pre.key
        self.name = f"Untitled {pre.title.split(',')[0].lower()}"
        self.dirty = True

    @property
    def binding(self) -> bindings.Binding:
        return bindings.get(self.imposition.binding_key)

    def set_binding(self, key: str) -> None:
        b = bindings.get(key)
        s = self.imposition
        s.binding_key = key
        s.single_sided = not b.duplex
        s.creep_enabled = b.creep
        if b.family == bindings.FOLDED:
            s.sheets_per_signature = b.default_sheets_per_signature
        if b.spine_allowance_mm:
            s.gutter_extra = b.spine_allowance_mm * MM
        else:
            s.gutter_extra = 0.0
        self.dirty = True

    # -- page list --------------------------------------------------------
    def add_file(self, path) -> List[PageItem]:
        src, items = self.library.add_file(path)
        if src.kind == "text":
            src.build_text(self.page_size(), self.text_margins(),
                           self.text_style)
            items = self.library.pages_for(src)
        self._append(items)
        return items

    def add_folder(self, folder, recursive: bool = False) -> List[PageItem]:
        out: List[PageItem] = []
        for src, items in self.library.add_folder(folder, recursive):
            self._append(items)
            out.extend(items)
        return out

    def add_text(self, text: str, title: str = "Text") -> List[PageItem]:
        src = self.library.add(Source.from_text(text, title))
        src.build_text(self.page_size(), self.text_margins(), self.text_style)
        items = self.library.pages_for(src)
        self._append(items)
        return items

    def add_blanks(self, count: int = 1, at: Optional[int] = None) -> List[PageItem]:
        src, items = self.library.add_blanks(count)
        if at is None:
            self.pages.extend(items)
        else:
            self.pages[at:at] = items
        self.dirty = True
        return items

    def _append(self, items: Sequence[PageItem]) -> None:
        """Add a source's pages, opening on a recto if that is asked for."""
        if (self.start_sources_on_recto and self.pages and items
                and len(self.user_pages()) % 2 == 1):
            _src, blanks = self.library.add_blanks(1)
            for b in blanks:
                b.label = "blank (recto break)"
            self.pages.extend(blanks)
        self.pages.extend(items)
        self.dirty = True

    def user_pages(self) -> List[PageItem]:
        return [p for p in self.pages if not p.locked_blank]

    def move(self, index: int, to: int) -> None:
        if 0 <= index < len(self.pages):
            item = self.pages.pop(index)
            self.pages.insert(max(0, min(len(self.pages), to)), item)
            self.dirty = True

    def remove(self, indices: Sequence[int]) -> None:
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self.pages):
                del self.pages[i]
        self.dirty = True

    def duplicate(self, indices: Sequence[int]) -> None:
        import copy
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self.pages):
                self.pages.insert(i + 1, copy.deepcopy(self.pages[i]))
        self.dirty = True

    def rotate(self, indices: Sequence[int], degrees: int = 90) -> None:
        for i in indices:
            if 0 <= i < len(self.pages):
                self.pages[i].rotation = (self.pages[i].rotation + degrees) % 360
        self.library.invalidate()
        self.dirty = True

    def reflow_text(self) -> None:
        changed = False
        for src in self.library.sources.values():
            if src.kind != "text":
                continue
            before = src.page_count
            src.build_text(self.page_size(), self.text_margins(), self.text_style)
            if src.page_count != before:
                changed = True
                self._resync_source_pages(src)
        if changed:
            self.library.invalidate()
            self.dirty = True

    def _resync_source_pages(self, src: Source) -> None:
        """Keep the page list in step when a text source reflows."""
        idx = [i for i, p in enumerate(self.pages) if p.source_id == src.id]
        if not idx:
            return
        first = idx[0]
        template = self.pages[first]
        for i in reversed(idx):
            del self.pages[i]
        import copy
        new = []
        for i in range(src.page_count):
            item = copy.deepcopy(template)
            item.source_page = i
            item.label = f"{src.title} {i + 1}"
            new.append(item)
        self.pages[first:first] = new

    # -- padding ----------------------------------------------------------
    def clear_auto_blanks(self) -> None:
        self.pages = [p for p in self.pages if not p.locked_blank]

    def apply_padding(self) -> int:
        """Append the blanks the binding needs. Returns how many were added."""
        self.clear_auto_blanks()
        if not self.auto_pad or not self.pages:
            return 0
        n = len(self.pages)
        target = padded_page_count(max(1, n), self.imposition)
        if target <= n:
            return 0
        src, items = self.library.add_blanks(target - n)
        for it in items:
            it.locked_blank = True
            it.label = "blank (auto)"
        self.pages.extend(items)
        self.dirty = True
        return target - n

    # -- geometry ---------------------------------------------------------
    def page_size(self) -> Size:
        s = self.imposition
        if s.trim_size is not None:
            return s.trim_size
        sheet = s.effective_sheet()
        if self.binding.family == bindings.FOLDED:
            cols, rows = (2 ** math.ceil(s.folds_per_sheet / 2),
                          2 ** (s.folds_per_sheet // 2))
        elif self.binding.family == bindings.MINI8:
            cols, rows = 4, 2
        elif self.binding.family == bindings.ACCORDION:
            cols, rows = max(2, s.panels_per_sheet), 1
        elif self.binding.family == bindings.FUKUROTOJI:
            cols, rows = 2, 1
        else:
            cols, rows = (s.up, 1) if s.up <= 2 else (2, s.up // 2)
        from .imposition import imposition_area
        a = imposition_area(sheet, s)
        return Size((a[2] - a[0]) / cols, (a[3] - a[1]) / rows)

    def text_margins(self) -> Tuple[float, float, float, float]:
        s = self.imposition
        return (s.margin_top, s.margin_bottom, s.margin_inner + s.gutter_extra,
                s.margin_outer)

    # -- tone -------------------------------------------------------------
    def tone_for(self, item: PageItem) -> ToneSettings:
        if not item.tone_enabled:
            t = self.tone.copy()
            t.enabled = False
            return t
        if item.tone:
            t = ToneSettings(**{k: v for k, v in item.tone.items()
                                if k in ToneSettings.__dataclass_fields__})
            return t
        if item.tone_preset:
            return apply_preset(self.tone, item.tone_preset)
        return self.tone

    def set_profile(self, profile: Optional[PrinterProfile]) -> None:
        self.profile = profile
        self.profile_name = profile.name if profile else ""
        if profile is not None:
            lo, hi = profile.suggested_limits()
            self.tone.min_dot = max(self.tone.min_dot, lo)
            self.tone.max_ink = min(self.tone.max_ink, hi) if hi < 1.0 else self.tone.max_ink
            self.tone.device_dpi = profile.dpi
        self.library.invalidate()
        self.dirty = True

    # -- plan -------------------------------------------------------------
    def build_plan(self, pad: bool = True) -> Plan:
        if pad:
            self.apply_padding()
        return build_plan(max(1, len(self.pages)), self.imposition)

    def summary(self) -> Dict[str, object]:
        plan = self.build_plan()
        b = self.binding
        sheets = plan.sheet_count
        return {
            "pages": len(self.pages),
            "user_pages": len(self.user_pages()),
            "blanks": len(self.pages) - len(self.user_pages()),
            "sheets": sheets,
            "sides": len(plan.sheets),
            "signatures": len(plan.signatures),
            "signature_sheets": plan.signatures,
            "binding": b.name,
            "page_size": self.page_size().describe(),
            "sheet_size": plan.sheet_size.describe(),
            "notes": plan.notes,
        }

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> Dict:
        imp = asdict(self.imposition)
        imp["sheet_size"] = [self.imposition.sheet_size.width,
                             self.imposition.sheet_size.height]
        imp["trim_size"] = ([self.imposition.trim_size.width,
                             self.imposition.trim_size.height]
                            if self.imposition.trim_size else None)
        return {
            "format": 1,
            "app": "Signature Zine",
            "saved": time.time(),
            "name": self.name,
            "mode": self.mode,
            "preset_key": self.preset_key,
            "auto_pad": self.auto_pad,
            "start_sources_on_recto": self.start_sources_on_recto,
            "imposition": imp,
            "tone": asdict(self.tone),
            "text_style": asdict(self.text_style),
            "digital": asdict(self.digital),
            "output": asdict(self.output),
            "profile_name": self.profile_name,
            "sources": [s.to_dict() for s in self.library.sources.values()],
            "pages": [p.to_dict() for p in self.pages],
        }

    def save(self, path) -> Path:
        path = Path(path)
        if path.suffix != PROJECT_EXT:
            path = path.with_suffix(PROJECT_EXT)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        self.path = path
        self.name = path.stem
        self.dirty = False
        return path

    @classmethod
    def load(cls, path) -> "Project":
        path = Path(path)
        d = json.loads(path.read_text())
        p = cls()
        p.name = d.get("name", path.stem)
        p.mode = d.get("mode", "zine")
        p.preset_key = d.get("preset_key", "")
        p.auto_pad = d.get("auto_pad", True)
        p.start_sources_on_recto = d.get("start_sources_on_recto", False)

        imp = dict(d.get("imposition", {}))
        ss = imp.pop("sheet_size", None)
        ts = imp.pop("trim_size", None)
        known = set(ImpositionSettings.__dataclass_fields__)
        p.imposition = ImpositionSettings(**{k: v for k, v in imp.items()
                                             if k in known})
        if ss:
            p.imposition.sheet_size = Size(*ss)
        p.imposition.trim_size = Size(*ts) if ts else None

        t = d.get("tone", {})
        p.tone = ToneSettings(**{k: v for k, v in t.items()
                                 if k in ToneSettings.__dataclass_fields__})
        if isinstance(p.tone.curve, list):
            p.tone.curve = [tuple(c) for c in p.tone.curve]
        ts2 = d.get("text_style", {})
        p.text_style = TextStyle(**{k: v for k, v in ts2.items()
                                    if k in TextStyle.__dataclass_fields__})
        dg = d.get("digital", {})
        p.digital = DigitalSettings(**{k: v for k, v in dg.items()
                                       if k in DigitalSettings.__dataclass_fields__})
        og = d.get("output", {})
        p.output = OutputSettings(**{k: v for k, v in og.items()
                                     if k in OutputSettings.__dataclass_fields__})

        for sd in d.get("sources", []):
            src = Source.from_dict(sd)
            p.library.add(src)
        p.pages = [PageItem.from_dict(pd) for pd in d.get("pages", [])]

        name = d.get("profile_name", "")
        if name:
            for prof in list_profiles():
                if prof.name == name:
                    p.profile = prof
                    p.profile_name = name
                    break
        for src in p.library.sources.values():
            if src.kind == "text":
                src.build_text(p.page_size(), p.text_margins(), p.text_style)
        p.path = path
        p.dirty = False
        return p

    def missing_files(self) -> List[str]:
        out = []
        for s in self.library.sources.values():
            if s.path and not Path(s.path).exists():
                out.append(s.path)
        return out
