"""Reading a printed test sheet back in from a scan.

Finds the four corner fiducials, works out the homography from the sheet's
normalised coordinates to the scan, and samples every patch.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .testsheet import Patch, SheetMap

Point = Tuple[float, float]


def load_scan(path) -> Image.Image:
    img = Image.open(path)
    if img.mode not in ("L", "I;16"):
        img = img.convert("L")
    return img.convert("L")


def _to_array(img: Image.Image, max_px: int = 2400) -> Tuple[np.ndarray, float]:
    scale = 1.0
    if max(img.size) > max_px:
        scale = max_px / float(max(img.size))
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32), scale


def detect_paper(arr: np.ndarray) -> Tuple[int, int, int, int]:
    """Bounding box of the sheet inside a scan that may show the lid."""
    bright = arr > (0.55 * float(arr.max()) if arr.max() > 0 else 128)
    cols = np.where(bright.sum(axis=0) > bright.shape[0] * 0.25)[0]
    rows = np.where(bright.sum(axis=1) > bright.shape[1] * 0.25)[0]
    if len(cols) < 2 or len(rows) < 2:
        return (0, 0, arr.shape[1] - 1, arr.shape[0] - 1)
    return (int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1]))


def find_fiducials(img: Image.Image,
                   expected: Optional[Sequence[Point]] = None
                   ) -> Optional[List[Point]]:
    """Locate the four solid corner squares. Returns TL, TR, BR, BL in pixels."""
    arr, scale = _to_array(img)
    if arr.size == 0:
        return None
    x0, y0, x1, y1 = detect_paper(arr)
    sub = arr[y0:y1 + 1, x0:x1 + 1]
    if sub.size == 0:
        return None
    h, w = sub.shape
    paper_level = float(np.percentile(sub, 90))
    ink_level = float(np.percentile(sub, 2))
    thr = ink_level + 0.35 * (paper_level - ink_level)

    win = 0.22
    corners_norm = expected or [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    out: List[Point] = []
    for (nx, ny) in corners_norm:
        cx, cy = nx * (w - 1), ny * (h - 1)
        wx0 = int(max(0, cx - win * w))
        wx1 = int(min(w, cx + win * w))
        wy0 = int(max(0, cy - win * h))
        wy1 = int(min(h, cy + win * h))
        region = sub[wy0:wy1, wx0:wx1]
        dark = region < thr
        if dark.sum() < 12:
            return None
        ys, xs = np.nonzero(dark)
        d = (xs + wx0 - cx) ** 2 + (ys + wy0 - cy) ** 2
        seed = int(np.argmin(d))
        sx, sy = xs[seed], ys[seed]
        radius = max(6.0, 0.02 * max(w, h))
        near = ((xs - sx) ** 2 + (ys - sy) ** 2) <= radius ** 2
        if near.sum() < 8:
            return None
        px = float(xs[near].mean() + wx0 + x0) / scale
        py = float(ys[near].mean() + wy0 + y0) / scale
        out.append((px, py))
    return out


def homography(src: Sequence[Point], dst: Sequence[Point]) -> np.ndarray:
    """3x3 matrix mapping src -> dst, from four point pairs."""
    A = []
    b = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    h = np.linalg.solve(np.asarray(A, dtype=float), np.asarray(b, dtype=float))
    return np.append(h, 1.0).reshape(3, 3)


def apply_h(H: np.ndarray, p: Point) -> Point:
    v = H @ np.array([p[0], p[1], 1.0])
    return (float(v[0] / v[2]), float(v[1] / v[2]))


@dataclass
class ScanResult:
    nominal: List[float]
    values: List[float]
    corners: List[Point]
    patch_boxes: List[Tuple[float, float, float, float]]
    warnings: List[str]


def measure_sheet(img: Image.Image, smap: SheetMap,
                  corners: Optional[Sequence[Point]] = None,
                  inset: float = 0.30) -> ScanResult:
    """Sample every patch in ``smap`` out of the scan."""
    warnings: List[str] = []
    if corners is None:
        corners = find_fiducials(img, smap.fiducials or None)
    if corners is None:
        raise ValueError("Could not find the four corner marks. Crop the scan "
                         "to the sheet, or place the corners by hand.")
    src = [(f[0], f[1]) for f in smap.fiducials] or \
          [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    H = homography(src, list(corners))

    arr = np.asarray(img.convert("L"), dtype=np.float32)
    nominal: List[float] = []
    values: List[float] = []
    boxes: List[Tuple[float, float, float, float]] = []
    for patch in smap.patches:
        x0, y0, x1, y1 = patch.box
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hw, hh = (x1 - x0) / 2.0 * (1 - inset), (y1 - y0) / 2.0 * (1 - inset)
        pts = [apply_h(H, (cx - hw, cy - hh)), apply_h(H, (cx + hw, cy - hh)),
               apply_h(H, (cx + hw, cy + hh)), apply_h(H, (cx - hw, cy + hh))]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ix0, ix1 = int(max(0, min(xs))), int(min(arr.shape[1], max(xs)))
        iy0, iy1 = int(max(0, min(ys))), int(min(arr.shape[0], max(ys)))
        if ix1 - ix0 < 2 or iy1 - iy0 < 2:
            warnings.append(f"patch {patch.id} fell outside the scan")
            continue
        region = arr[iy0:iy1, ix0:ix1]
        nominal.append(float(patch.nominal))
        values.append(float(np.median(region)))
        boxes.append((ix0, iy0, ix1, iy1))

    if len(values) < 4:
        raise ValueError("Too few patches could be read.")
    order = np.argsort(nominal)
    n = np.asarray(nominal)[order]
    v = np.asarray(values)[order]
    if v[0] < v[-1]:
        warnings.append("The scan looks inverted: the 0% patch is darker than "
                        "the solid. Check that you scanned the right side up.")
    if abs(float(v[0]) - float(v[-1])) < 30:
        warnings.append("Very little contrast between paper and solid - the "
                        "scan may be over-exposed.")
    return ScanResult(nominal=[float(x) for x in n], values=[float(x) for x in v],
                      corners=list(corners), patch_boxes=boxes,
                      warnings=warnings)


def overlay(img: Image.Image, result: ScanResult) -> Image.Image:
    """Show where the sampler looked, so the user can sanity check it."""
    from PIL import ImageDraw
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for (x0, y0, x1, y1) in result.patch_boxes:
        d.rectangle([x0, y0, x1, y1], outline=(220, 40, 40), width=2)
    for (x, y) in result.corners:
        r = max(6, out.width // 150)
        d.ellipse([x - r, y - r, x + r, y + r], outline=(20, 120, 220), width=3)
    return out
