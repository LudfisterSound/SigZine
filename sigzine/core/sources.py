"""Importing material: PDFs, images, text files, scans, blank stock."""
from __future__ import annotations

import io
import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import fitz
from PIL import Image, ImageSequence

from .tone import ToneSettings, apply_tone
from .typeset import TextStyle, typeset
from .units import Size

# One decoded copy of an image can be tens of megabytes, so the cache is
# shared across every source and bounded by bytes rather than entries.
_DECODED: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_DECODE_BUDGET = 96 * 1024 * 1024


def _decoded_bytes(img: Image.Image) -> int:
    return img.width * img.height * max(1, len(img.getbands()))


def _cache_get(key, want: int) -> Optional[Image.Image]:
    img = _DECODED.get(key)
    if img is None:
        return None
    _DECODED.move_to_end(key)
    return img


def _cache_put(key, img: Image.Image) -> None:
    _DECODED[key] = img
    _DECODED.move_to_end(key)
    total = sum(_decoded_bytes(v) for v in _DECODED.values())
    while total > _DECODE_BUDGET and len(_DECODED) > 1:
        _k, v = _DECODED.popitem(last=False)
        total -= _decoded_bytes(v)


def clear_decode_cache() -> None:
    _DECODED.clear()


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp",
             ".ppm", ".pgm", ".tga"}
PDF_EXT = {".pdf"}
VECTOR_EXT = {".svg", ".eps", ".ps", ".xps", ".cbz", ".epub", ".fb2", ".mobi"}
TEXT_EXT = {".txt", ".md", ".markdown", ".text", ".rst"}

PDF_LIKE = PDF_EXT | VECTOR_EXT
ALL_EXT = IMAGE_EXT | PDF_LIKE | TEXT_EXT


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def supported(path) -> bool:
    return Path(path).suffix.lower() in ALL_EXT


def classify(path) -> str:
    ext = Path(path).suffix.lower()
    if ext in PDF_LIKE:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    return "unknown"


@dataclass
class Source:
    """One imported file (or generated block of pages)."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    kind: str = "pdf"                 # pdf | image | text | blank
    path: str = ""
    title: str = ""
    page_count: int = 1
    # text sources
    text: str = ""
    style: Dict = field(default_factory=dict)
    # blank sources
    blank_count: int = 1
    # image sources: the true pixel size, read from the header once
    pixel_width: int = 0
    pixel_height: int = 0
    image_dpi: float = 300.0
    # cached state (not serialised)
    _doc: Optional[fitz.Document] = field(default=None, repr=False, compare=False)
    _images: Optional[List[Image.Image]] = field(default=None, repr=False,
                                                 compare=False)
    _built_key: Optional[tuple] = field(default=None, repr=False, compare=False)
    _page_cache: Dict = field(default_factory=dict, repr=False, compare=False)

    # -- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path) -> "Source":
        p = Path(path)
        kind = classify(p)
        src = cls(kind=kind, path=str(p), title=p.name)
        if kind == "text":
            src.text = p.read_text(errors="replace")
        src.refresh()
        return src

    @classmethod
    def blank(cls, count: int = 1, title: str = "Blank") -> "Source":
        return cls(kind="blank", title=title, blank_count=count,
                   page_count=count)

    @classmethod
    def from_text(cls, text: str, title: str = "Text") -> "Source":
        return cls(kind="text", text=text, title=title, page_count=1)

    # -- lifecycle --------------------------------------------------------
    def refresh(self) -> None:
        self.close()
        if self.kind == "pdf":
            self._doc = fitz.open(self.path)
            self.page_count = self._doc.page_count
        elif self.kind == "image":
            with Image.open(self.path) as img:      # header only, no decode
                self.pixel_width, self.pixel_height = img.size
                d = img.info.get("dpi", (300, 300))
                d = d[0] if isinstance(d, (tuple, list)) else d
                try:
                    self.image_dpi = float(d) or 300.0
                except (TypeError, ValueError):
                    self.image_dpi = 300.0
                self.page_count = max(1, int(getattr(img, "n_frames", 1)))
            self._images = None
        elif self.kind == "blank":
            self.page_count = max(1, self.blank_count)
        elif self.kind == "text":
            self.page_count = max(1, self.page_count)

    def close(self) -> None:
        for k in [k for k in _DECODED if k[0] == self.id]:
            _DECODED.pop(k, None)
        self._page_cache.clear()
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._doc = None
        self._images = None

    # -- text sources need a page size before they exist ------------------
    def build_text(self, page_size: Size,
                   margins: Tuple[float, float, float, float],
                   style: Optional[TextStyle] = None) -> None:
        if self.kind != "text":
            return
        st = style or TextStyle(**self.style) if self.style else (style or TextStyle())
        key = (round(page_size.width, 2), round(page_size.height, 2),
               tuple(round(m, 2) for m in margins), repr(asdict(st)),
               hash(self.text))
        if self._built_key == key and self._doc is not None:
            return
        if self._doc is not None:
            self._doc.close()
        self._doc = typeset(self.text, page_size, margins, st, self.title)
        self.page_count = self._doc.page_count
        self._built_key = key

    # -- page access ------------------------------------------------------
    def pil_page(self, index: int,
                 max_px: Optional[int] = None) -> Optional[Image.Image]:
        """The page as a PIL image, for image sources only.

        ``max_px`` lets the JPEG decoder throw away detail we are never going
        to use, which is the difference between a snappy preview and two
        seconds of nothing happening.
        """
        if self.kind != "image":
            return None
        native = max(self.pixel_width, self.pixel_height)
        want = int(max_px) if max_px else (native or 4096)
        key = (self.id, self.path, index)
        hit = _cache_get(key, want)
        # a copy that is already big enough beats decoding the file again
        if hit is not None and (max(hit.size) >= want or
                                (native and max(hit.size) >= native)):
            return hit
        img = Image.open(self.path)
        if getattr(img, "n_frames", 1) > 1:
            img.seek(min(index, img.n_frames - 1))
        try:
            img.draft(None, (want, want))
        except Exception:
            pass
        img.load()
        _cache_put(key, img)
        return img

    def render(self, index: int, dpi: float = 72.0,
               tone: Optional[ToneSettings] = None, profile=None,
               for_screen: bool = True,
               max_px: Optional[int] = None) -> Optional[Image.Image]:
        """Rasterise one page. ``max_px`` caps the longest side in pixels."""
        if self.kind == "blank":
            return None
        if self.kind == "image":
            img = self.pil_page(index,
                                max_px=int(max_px * 2.2) if max_px else None)
            if img is None:
                return None
            target = None
            if max_px and max(img.size) > max_px:
                scale = max_px / float(max(img.size))
                target = (max(1, int(img.width * scale)),
                          max(1, int(img.height * scale)))
            if tone is not None and tone.enabled:
                return apply_tone(img, tone, profile, target_px=target,
                                  for_screen=for_screen)
            keep_color = tone is not None and tone.color
            out = img if (keep_color or not for_screen) else img.convert("L")
            return out.resize(target, Image.LANCZOS) if target else out
        if self._doc is None:
            self.refresh()
        if self._doc is None or index >= self._doc.page_count:
            return None
        if max_px:
            r = self._doc[index].rect
            dpi = min(dpi, max_px / max(1.0, max(r.width, r.height)) * 72.0)
        pm = self._doc[index].get_pixmap(dpi=max(12.0, dpi), alpha=False)
        img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        if tone is not None and tone.enabled:
            return apply_tone(img, tone, profile, for_screen=for_screen)
        return img

    def page_size(self, index: int = 0) -> Optional[Size]:
        if self.kind in ("pdf", "text") and self._doc is not None \
                and index < self._doc.page_count:
            r = self._doc[index].rect
            return Size(r.width, r.height)
        if self.kind == "image":
            if not self.pixel_width:
                self.refresh()
            if self.pixel_width:
                d = self.image_dpi or 300.0
                return Size(self.pixel_width / d * 72.0,
                            self.pixel_height / d * 72.0)
        return None

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> Dict:
        return {f: getattr(self, f) for f in self.__dataclass_fields__
                if not f.startswith("_")}

    @classmethod
    def from_dict(cls, d: Dict) -> "Source":
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        src = cls(**{k: v for k, v in d.items() if k in known})
        try:
            src.refresh()
        except Exception:
            pass
        return src


@dataclass
class PageItem:
    """One page of the finished book, pointing back at a source page."""
    source_id: str = ""
    source_page: int = 0
    kind: str = "pdf"
    rotation: int = 0
    fit: str = "fit"                  # fit | fill | actual | stretch
    fit_target: str = "auto"          # auto | trim | safe | bleed
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    tone_preset: Optional[str] = None
    tone: Optional[Dict] = None       # per page override
    tone_enabled: bool = True
    label: str = ""
    locked_blank: bool = False        # inserted by the imposer, not the user

    def is_blank(self) -> bool:
        return self.kind == "blank"

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "PageItem":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Library:
    """Owns the sources and hands out pages."""

    def __init__(self) -> None:
        self.sources: Dict[str, Source] = {}
        self._preview_cache: Dict[tuple, Image.Image] = {}

    # -- import -----------------------------------------------------------
    def add(self, source: Source) -> Source:
        self.sources[source.id] = source
        return source

    def add_file(self, path) -> Tuple[Source, List[PageItem]]:
        src = self.add(Source.from_file(path))
        return src, self.pages_for(src)

    def add_folder(self, folder, recursive: bool = False
                   ) -> List[Tuple[Source, List[PageItem]]]:
        root = Path(folder)
        it = root.rglob("*") if recursive else root.glob("*")
        files = sorted((p for p in it if p.is_file() and supported(p)),
                       key=lambda p: natural_key(p.name))
        return [self.add_file(p) for p in files]

    def add_blanks(self, count: int = 1) -> Tuple[Source, List[PageItem]]:
        src = self.add(Source.blank(count))
        return src, self.pages_for(src)

    def pages_for(self, src: Source) -> List[PageItem]:
        return [PageItem(source_id=src.id, source_page=i, kind=src.kind,
                         label=f"{src.title} {i + 1}" if src.page_count > 1
                         else src.title)
                for i in range(src.page_count)]

    def get(self, source_id: str) -> Optional[Source]:
        return self.sources.get(source_id)

    def remove(self, source_id: str) -> None:
        src = self.sources.pop(source_id, None)
        if src:
            src.close()

    # -- previews ---------------------------------------------------------
    def preview(self, item: PageItem, max_px: int = 420,
                tone: Optional[ToneSettings] = None, profile=None
                ) -> Optional[Image.Image]:
        src = self.get(item.source_id)
        if src is None or item.is_blank():
            return None
        key = (item.source_id, item.source_page, max_px,
               repr(tone) if tone else None, item.rotation)
        hit = self._preview_cache.get(key)
        if hit is not None:
            return hit
        img = src.render(item.source_page, dpi=150.0, tone=tone,
                         profile=profile, for_screen=True, max_px=max_px)
        if img is None:
            return None
        if item.rotation:
            img = img.rotate(-item.rotation, expand=True)
        if len(self._preview_cache) > 200:
            self._preview_cache.clear()
        self._preview_cache[key] = img
        return img

    def invalidate(self) -> None:
        self._preview_cache.clear()

    def close(self) -> None:
        for s in self.sources.values():
            s.close()
        self._preview_cache.clear()
