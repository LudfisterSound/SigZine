"""Printer profiles: measure what the printer actually does, then undo it.

Workflow
    1. Print the linearisation test sheet (testsheet.py) with all driver
       "enhancement" switched off.
    2. Read the patches back - by eye, with a densitometer, or by scanning
       the sheet and letting scan.py sample it.
    3. This module fits the printer's response curve and inverts it into the
       linearisation LUT that tone.py applies to every image.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .paths import printers_dir
from .tone import pchip, invert_lut

MEASURE_KINDS = ["L*", "density", "scan grey 0-255", "reflectance %"]


def _lab_l_to_y(L: np.ndarray) -> np.ndarray:
    L = np.asarray(L, dtype=float)
    y = np.where(L > 8.0, ((L + 16.0) / 116.0) ** 3, L / 903.3)
    return np.clip(y, 1e-6, 1.0)


def _srgb_to_y(v8: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(v8, dtype=float) / 255.0, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def to_luminance(values: Sequence[float], kind: str) -> np.ndarray:
    """Raw readings of any supported kind -> relative luminance, 0..1.

    Bigger is lighter, whatever the instrument. This is the only place that
    knows what a reading means.
    """
    v = np.asarray(values, dtype=float)
    if kind == "L*":
        return _lab_l_to_y(v)
    if kind == "density":
        return np.clip(10.0 ** (-v), 1e-6, 1.0)
    if kind == "scan grey 0-255":
        return _srgb_to_y(v)
    if kind == "reflectance %":
        return np.clip(v / 100.0, 1e-6, 1.0)
    raise ValueError(kind)


def measurements_to_coverage(nominal: Sequence[float], values: Sequence[float],
                             kind: str) -> np.ndarray:
    """Normalise raw readings to relative ink coverage 0..1.

    Paper white and the solid are taken from the lightest and darkest
    requested patch. That is fine for clean readings; linearise.py uses a
    more careful estimate when it is fitting a curve to a noisy scan.
    """
    y = to_luminance(values, kind)
    n = np.asarray(nominal, dtype=float)
    order = np.argsort(n)
    y_paper = float(y[order[0]])
    y_solid = float(y[order[-1]])
    if abs(y_paper - y_solid) < 1e-6:
        return np.zeros_like(y)
    cov = (y_paper - y) / (y_paper - y_solid)
    return np.clip(cov, 0.0, 1.0)


@dataclass
class PrinterProfile:
    name: str = "Untitled printer"
    model: str = ""
    paper: str = ""
    driver_notes: str = ""
    dpi: int = 600
    created: float = field(default_factory=time.time)
    measure_kind: str = "scan grey 0-255"
    nominal: List[float] = field(default_factory=list)   # requested ink 0..1
    measured: List[float] = field(default_factory=list)  # raw readings
    # The fitted response curve, as (requested, coverage) knots. Built by
    # linearise.py; when it is present it is preferred over threading a
    # curve through the raw readings, because it has had the measurement
    # noise taken out of it. Older profiles have none and still work.
    response_points: List[Tuple[float, float]] = field(default_factory=list)
    passes: int = 0                                      # build + refine count
    fit_rms: float = 0.0                                 # how well it fitted
    fit_outliers: int = 0
    measurement_noise: float = 0.0                       # from repeat patches
    built: float = 0.0
    duplex_offset_x_mm: float = 0.0
    duplex_offset_y_mm: float = 0.0
    scale_x: float = 1.0                                 # printer scaling error
    scale_y: float = 1.0
    min_dot: float = 0.0                                 # discovered limits
    max_ink: float = 1.0
    notes: str = ""

    # -- state ------------------------------------------------------------
    def has_linearisation(self) -> bool:
        if len(self.response_points) >= 2:
            return True
        return len(self.nominal) >= 4 and len(self.nominal) == len(self.measured)

    def is_fitted(self) -> bool:
        """True when the curve came from linearise.py rather than raw points."""
        return len(self.response_points) >= 2

    def copy(self) -> "PrinterProfile":
        import copy as _c
        return _c.deepcopy(self)

    def coverage(self) -> np.ndarray:
        return measurements_to_coverage(self.nominal, self.measured,
                                        self.measure_kind)

    # -- curves -----------------------------------------------------------
    def response_lut(self, size: int = 256) -> np.ndarray:
        """Requested ink -> coverage the printer actually produces."""
        if self.is_fitted():
            pts = [(float(a), float(b)) for a, b in self.response_points]
            pts.sort()
            if pts[0][0] > 0:
                pts.insert(0, (0.0, 0.0))
            if pts[-1][0] < 1:
                pts.append((1.0, 1.0))
            return pchip(pts, np.linspace(0.0, 1.0, size))
        if not self.has_linearisation():
            return np.linspace(0.0, 1.0, size)
        n = np.asarray(self.nominal, dtype=float)
        c = self.coverage()
        order = np.argsort(n)
        n, c = n[order], c[order]
        c = np.maximum.accumulate(c)          # measurement noise is not signal
        if c[-1] > 1e-6:
            c = c / c[-1]
        pts = list(zip(n.tolist(), c.tolist()))
        if pts[0][0] > 0:
            pts.insert(0, (0.0, 0.0))
        if pts[-1][0] < 1:
            pts.append((1.0, 1.0))
        return pchip(pts, np.linspace(0.0, 1.0, size))

    def linearisation_lut(self, size: int = 256) -> np.ndarray:
        """Wanted coverage -> value to send so the print matches."""
        return invert_lut(self.response_lut(size), size)

    # -- analysis ---------------------------------------------------------
    def analyse(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        if not self.has_linearisation():
            return {"status": "no measurements"}
        resp = self.response_lut(512)
        x = np.linspace(0.0, 1.0, 512)
        out["gain_25"] = float(np.interp(0.25, x, resp) - 0.25)
        out["gain_50"] = float(np.interp(0.50, x, resp) - 0.50)
        out["gain_75"] = float(np.interp(0.75, x, resp) - 0.75)
        # smallest requested dot that produces a visible change
        above = np.where(resp > 0.02)[0]
        out["min_printable"] = float(x[above[0]]) if len(above) else 0.0
        # where the shadows stop separating: last 5% of range
        near_solid = np.where(resp > 0.98)[0]
        out["shadow_merge"] = float(x[near_solid[0]]) if len(near_solid) else 1.0
        if len(self.nominal) >= 2 and len(self.nominal) == len(self.measured):
            c = self.coverage()
            out["max_coverage_raw"] = float(np.max(c))
            n = np.asarray(self.nominal)
            steps = np.diff(np.sort(c[np.argsort(n)]))
            out["flat_steps"] = int(np.sum(steps < 0.004))
        else:
            # A fitted curve with no raw readings kept: read the flat stretches
            # off the curve itself.
            out["max_coverage_raw"] = float(resp[-1])
            out["flat_steps"] = int(np.sum(np.diff(resp[::8]) < 0.004))
        out["patches"] = len(self.nominal)
        out["status"] = "ok"
        return out

    def suggested_limits(self) -> Tuple[float, float]:
        a = self.analyse()
        if a.get("status") != "ok":
            return (0.0, 1.0)
        lo = float(a.get("min_printable", 0.0))
        hi = float(a.get("shadow_merge", 1.0))
        return (round(min(0.12, lo), 3), round(max(0.80, hi), 3))

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "PrinterProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def filename(self) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_"
                       for ch in self.name).strip() or "printer"
        return safe + ".json"

    def save(self, directory: Optional[Path] = None) -> Path:
        directory = Path(directory) if directory else printers_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.filename()
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "PrinterProfile":
        return cls.from_dict(json.loads(Path(path).read_text()))


def list_profiles(directory: Optional[Path] = None) -> List[PrinterProfile]:
    directory = Path(directory) if directory else printers_dir()
    out = []
    for p in sorted(directory.glob("*.json")):
        try:
            out.append(PrinterProfile.load(p))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Quick visual calibration - no instrument needed
# ---------------------------------------------------------------------------

def from_visual_reading(name: str, lightest_visible: float,
                        darkest_separated: float,
                        looks_like_mid_grey: float) -> PrinterProfile:
    """Build a usable profile from three numbers read off the test sheet.

    lightest_visible    the lightest patch (0..1) that shows any tone at all
    darkest_separated   the darkest patch still distinguishable from solid
    looks_like_mid_grey the patch that reads as a visual 50% grey
    """
    lv = float(np.clip(lightest_visible, 0.0, 0.3))
    ds = float(np.clip(darkest_separated, 0.5, 1.0))
    mid = float(np.clip(looks_like_mid_grey, 0.1, 0.9))
    # The patch that looks 50% is producing 50% coverage, so the response
    # curve passes through (mid, 0.5). The visible/separated readings give
    # the toe and the shoulder.
    nominal = [0.0, lv, mid, ds, 1.0]
    coverage = [0.0, 0.02, 0.5, 0.98, 1.0]
    # store as reflectance so the maths round-trips
    reflect = [(1.0 - c) * 100.0 for c in coverage]
    p = PrinterProfile(name=name, measure_kind="reflectance %",
                       nominal=nominal, measured=reflect,
                       notes="Visual three-point calibration.")
    p.min_dot, p.max_ink = p.suggested_limits()
    return p


def blend_profiles(profiles: Sequence[PrinterProfile], name: str = "Average") -> PrinterProfile:
    """Average several runs of the same printer to beat measurement noise."""
    usable = [p for p in profiles if p.has_linearisation()]
    if not usable:
        raise ValueError("nothing to blend")
    grid = np.linspace(0.0, 1.0, 64)
    stack = np.stack([np.interp(grid, np.linspace(0, 1, 256), p.response_lut(256))
                      for p in usable])
    mean = stack.mean(axis=0)
    out = PrinterProfile(name=name, measure_kind="reflectance %",
                         nominal=grid.tolist(),
                         measured=[(1.0 - c) * 100.0 for c in mean.tolist()],
                         dpi=usable[0].dpi, model=usable[0].model,
                         paper=usable[0].paper,
                         notes=f"Average of {len(usable)} measurement runs.")
    out.min_dot, out.max_ink = out.suggested_limits()
    return out


# ---------------------------------------------------------------------------
# Front to back registration
# ---------------------------------------------------------------------------

def predict_reading(station: Tuple[float, float], sheet,
                    dx: float = 0.0, dy: float = 0.0, theta: float = 0.0,
                    sx: float = 1.0, sy: float = 1.0) -> Tuple[float, float]:
    """What a station would read, given how the back is displaced.

    The model, for a sheet turned left to right between passes: the back
    image is translated by (dx, dy) in its own coordinates, rotated by
    ``theta`` radians about the page centre and scaled by (sx, sy). The
    reading is where the back's pointer lands on the front's scale, seen
    from the front, right and down positive.
    """
    fx, fy = station
    cx, cy = sheet.width / 2.0, sheet.height / 2.0
    rx = -dx + theta * (fy - cy) + (1.0 - sx) * (cx - fx)
    ry = dy - theta * (fx - cx) + (sy - 1.0) * (fy - cy)
    return (rx, ry)


def solve_registration(readings: Dict[str, Tuple[float, float]],
                       stations: Dict[str, Tuple[float, float]],
                       sheet) -> Dict[str, float]:
    """Recover offset, skew and scale from readings at stations A, B and C.

    Station B sits at the centre and gives the offset on its own. A and C are
    level with each other and symmetric about the centre line: the difference
    between their DOWN readings is rotation, the difference between their
    ACROSS readings is a width difference.
    """
    ax, ay = readings["A"]
    bx, by = readings["B"]
    cx_, cy_ = readings["C"]
    fax, fay = stations["A"]
    fcx, _fcy = stations["C"]
    centre_y = sheet.height / 2.0

    d = (fcx - fax) / 2.0
    e = centre_y - fay
    out: Dict[str, float] = {}
    out["dx"] = -bx
    out["dy"] = by
    out["theta"] = (ay - cy_) / (2.0 * d) if d else 0.0
    out["scale_x"] = 1.0 - (ax - cx_) / (2.0 * d) if d else 1.0
    out["scale_y"] = (1.0 - (ay + cy_ - 2.0 * out["dy"]) / (2.0 * e)
                      if e else 1.0)
    out["theta_deg"] = math.degrees(out["theta"])
    # how far the skew throws a corner, which is the number that matters
    half_diag = math.hypot(sheet.width, sheet.height) / 2.0
    out["skew_at_corner"] = abs(out["theta"]) * half_diag
    return out


def describe_registration(r: Dict[str, float], unit: float) -> List[str]:
    """Plain sentences about what the numbers mean. ``unit`` is mm in points."""
    lines = []
    lines.append(f"Offset: {r['dx'] / unit:+.2f} mm across, "
                 f"{r['dy'] / unit:+.2f} mm down. This one is correctable.")
    deg = r["theta_deg"]
    corner = r["skew_at_corner"] / unit
    if abs(corner) < 0.3:
        lines.append("Skew: none worth measuring.")
    else:
        lines.append(f"Skew: {deg:+.3f} degrees, which throws a corner by "
                     f"{corner:.1f} mm. Usually the paper guides or the "
                     f"duplex path, and it varies sheet to sheet.")
    for axis, key in (("width", "scale_x"), ("height", "scale_y")):
        pct = (r[key] - 1.0) * 100.0
        if abs(pct) > 0.05:
            lines.append(f"Back {axis} differs from the front by {pct:+.2f}%.")
    return lines
