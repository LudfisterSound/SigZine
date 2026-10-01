"""Building a linearisation profile out of measured patches.

calibration.py holds the profile and knows how to invert a response curve.
This module is the step in between: turning a page of raw readings into a
curve worth inverting, then proving on paper that the correction worked.

Why not just interpolate the readings
    A full linearisation target carries ninety-odd patches, several of them
    at the same requested value, read off a scanner whose own noise is
    worth a couple of percent. Threading a curve through every point
    faithfully reproduces that noise, and a wobbly correction curve prints
    as banding in smooth gradients - the exact fault linearisation is meant
    to cure. So the readings are pooled by level, fitted with a smooth
    monotone curve, and the points that disagree with their neighbours are
    dropped rather than honoured.

The three passes
    build      measure the printer, fit its response, invert it
    verify     print a target THROUGH that correction and measure again;
               what you read should now be what you asked for
    refine     feed the verification back in, because a second measurement
               taken through the correction pins the response down far
               better than the first one did

A single build pass gets most printers inside a couple of percent. Refining
once closes most of what is left.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .calibration import (PrinterProfile, measurements_to_coverage,
                          to_luminance)
from .tone import pchip

Point = Tuple[float, float]

# Knots for a full target: close together where the highlight and shadow
# wedges put their patches, wider through the midtones where the response
# is smooth and the eye is forgiving.
DEFAULT_KNOTS: Tuple[float, ...] = (
    0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.14, 0.20, 0.28, 0.38,
    0.50, 0.62, 0.72, 0.80, 0.86, 0.90, 0.93, 0.96, 0.98, 1.0,
)

# How hard to smooth, per unit of weight. Larger smooths harder. The right
# amount depends on how noisy the readings are, which the fit measures for
# itself, so leaving smooth=None is better than any fixed number: the rule
# below lands within a fraction of a percent of the best fixed choice at
# every noise level from a clean densitometer to a bad flatbed scan.
SMOOTH_GAIN = 0.7           # multiplies the estimated noise variance
SMOOTH_MIN = 1e-5           # clean readings still want their edges rounded
SMOOTH_MAX = 1.2e-4         # past here the curve is bent more than the data

OUTLIER_SIGMA = 3.0         # residual beyond this many sigma is dropped
MAX_OUTLIER_FRACTION = 0.12
# Clean readings scatter by almost nothing, and three times almost nothing
# would condemn every patch. The outlier threshold never drops below this
# fraction of the paper-to-solid range.
MIN_SIGMA_FRACTION = 0.004


# ---------------------------------------------------------------------------
# Pooling repeated readings
# ---------------------------------------------------------------------------

@dataclass
class Pooled:
    """Readings grouped by requested level."""
    nominal: np.ndarray
    value: np.ndarray            # pooled reading per level
    spread: np.ndarray           # half the range of the readings at that level
    count: np.ndarray            # how many patches contributed
    raw_patches: int = 0

    def repeats(self) -> int:
        return int(np.sum(self.count > 1))


def pool_readings(nominal: Sequence[float], values: Sequence[float],
                  tolerance: float = 1e-6) -> Pooled:
    """Group readings that asked for the same ink and average them.

    A full target repeats several levels - 2% and 10% appear in both the
    main ramp and the highlight wedge - and those repeats are free
    information about how noisy the measurement is. Taking the median of
    each group uses them instead of letting the last one win.
    """
    n = np.asarray(nominal, dtype=float)
    v = np.asarray(values, dtype=float)
    if n.size != v.size:
        raise ValueError("nominal and values must be the same length")
    if n.size == 0:
        empty = np.zeros(0)
        return Pooled(empty, empty, empty, empty.astype(int), 0)

    order = np.argsort(n, kind="stable")
    n, v = n[order], v[order]
    groups: List[Tuple[float, List[float]]] = []
    for ni, vi in zip(n.tolist(), v.tolist()):
        if groups and abs(ni - groups[-1][0]) <= tolerance:
            groups[-1][1].append(vi)
        else:
            groups.append((ni, [vi]))

    out_n, out_v, out_s, out_c = [], [], [], []
    for level, vals in groups:
        a = np.asarray(vals, dtype=float)
        out_n.append(level)
        out_v.append(float(np.median(a)))
        out_s.append(float((a.max() - a.min()) / 2.0) if a.size > 1 else 0.0)
        out_c.append(int(a.size))
    return Pooled(np.asarray(out_n), np.asarray(out_v), np.asarray(out_s),
                  np.asarray(out_c, dtype=int), raw_patches=int(len(n)))


# ---------------------------------------------------------------------------
# Monotone smoothing
# ---------------------------------------------------------------------------

def isotonic(y: Sequence[float], w: Optional[Sequence[float]] = None
             ) -> np.ndarray:
    """Nearest non-decreasing sequence, by weighted least squares (PAVA).

    Pool adjacent violators: walk left to right, and whenever a value
    undercuts the block before it, merge the two into their weighted mean
    and check again. The result is the exact L2 projection onto the
    monotone cone, so it changes as little as it can get away with.
    """
    yv = np.asarray(y, dtype=float)
    wv = np.ones_like(yv) if w is None else np.asarray(w, dtype=float)
    wv = np.where(wv > 0, wv, 1e-12)
    # blocks of (weighted sum, weight, length)
    blocks: List[List[float]] = []
    for yi, wi in zip(yv.tolist(), wv.tolist()):
        blocks.append([yi * wi, wi, 1.0])
        while len(blocks) >= 2 and \
                blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            a = blocks.pop()
            blocks.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    out: List[float] = []
    for sw, tw, length in blocks:
        out.extend([sw / tw] * int(length))
    return np.asarray(out, dtype=float)


def _neighbour_residuals(x: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Each interior reading minus a straight line through the two beside it.

    On a smooth curve this leaves almost nothing but the measurement noise,
    which makes it both a noise estimator and an outlier detector. The
    interpolation is weighted by the actual spacing, so the uneven steps of
    a real target - fine at the ends, coarse through the middle - do not
    show up as error.
    """
    if x.size < 3:
        return np.zeros(0)
    h = x[2:] - x[:-2]
    t = np.where(h > 0, (x[1:-1] - x[:-2]) / np.where(h > 0, h, 1.0), 0.5)
    return c[1:-1] - (c[:-2] + (c[2:] - c[:-2]) * t)


def estimate_noise(x: np.ndarray, c: np.ndarray) -> float:
    """Standard deviation of the measurement, in the units of ``c``.

    From the scatter of each reading about its neighbours rather than from
    repeat patches, so it works on a target that repeats nothing. The
    1.4826 turns a median absolute deviation into a standard deviation for
    normal noise; dividing by sqrt(1.5) undoes the variance the two
    neighbours contribute to the difference.
    """
    resid = _neighbour_residuals(x, c)
    if resid.size == 0:
        return 0.0
    mad = float(np.median(np.abs(resid - np.median(resid))))
    return 1.4826 * mad / float(np.sqrt(1.5))


def choose_smooth(sigma: float, span: float) -> float:
    """How hard to smooth, given how noisy the readings turned out to be."""
    if span <= 0:
        return SMOOTH_MIN
    rel = sigma / span
    return float(np.clip(SMOOTH_GAIN * rel * rel, SMOOTH_MIN, SMOOTH_MAX))


def reject_local_outliers(x: np.ndarray, c: np.ndarray, floor: float,
                          max_drop: int = 0) -> np.ndarray:
    """Flag patches that disagree with the patches either side of them.

    A speck of dirt or a crease ruins one patch, and a least squares fit
    spreads the damage: the curve is dragged toward the bad reading until
    its honest neighbours look guilty too. Comparing each patch against a
    straight line through the two beside it localises the blame, which is
    what a person reading the sheet does by eye.

    Points are dropped one at a time, worst first, because a bad patch also
    corrupts the prediction for its own neighbours.
    """
    keep = np.ones(x.size, dtype=bool)
    if x.size < 5:
        return keep
    if max_drop <= 0:
        max_drop = max(1, int(MAX_OUTLIER_FRACTION * x.size))
    for _ in range(max_drop):
        idx = np.nonzero(keep)[0]
        if idx.size < 5:
            break
        # Interior points only: the two ends have nothing on one side, and
        # the curve is rescaled from the fit rather than from those patches
        # anyway, so there is nothing to gain by questioning them.
        resid = _neighbour_residuals(x[idx], c[idx])
        if resid.size == 0:
            break
        sigma = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
        sigma = max(sigma, floor)
        worst = int(np.argmax(np.abs(resid)))
        if abs(resid[worst]) <= OUTLIER_SIGMA * sigma:
            break
        keep[idx[worst + 1]] = False
    return keep


def choose_knots(levels: Sequence[float]) -> np.ndarray:
    """Knot positions for the fit, matched to how much data there is."""
    lv = np.unique(np.asarray(levels, dtype=float))
    if lv.size == 0:
        return np.asarray([0.0, 1.0])
    if lv.size <= 10:
        # Too little to smooth: let the readings themselves be the knots.
        return lv
    knots = np.asarray(DEFAULT_KNOTS, dtype=float)
    if lv.size < 2 * knots.size:
        k = max(5, lv.size // 2)
        knots = np.unique(np.quantile(lv, np.linspace(0.0, 1.0, k)))
    inside = knots[(knots >= lv[0]) & (knots <= lv[-1])]
    return np.unique(np.concatenate(([lv[0]], inside, [lv[-1]])))


def _basis(x: np.ndarray, knots: np.ndarray) -> np.ndarray:
    """Linear-interpolation design matrix: row i spreads x_i over two knots."""
    n, k = x.size, knots.size
    B = np.zeros((n, k), dtype=float)
    if k == 1:
        B[:, 0] = 1.0
        return B
    idx = np.clip(np.searchsorted(knots, x) - 1, 0, k - 2)
    h = np.diff(knots)[idx]
    t = np.where(h > 0, (x - knots[idx]) / np.where(h > 0, h, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    B[np.arange(n), idx] = 1.0 - t
    B[np.arange(n), idx + 1] += t
    return B


def _curvature(knots: np.ndarray) -> np.ndarray:
    """Second-difference operator approximating f'' at the interior knots.

    Weighted so that ``sum (D y)^2`` approximates the integral of f''
    squared even when the knots are unevenly spaced.
    """
    k = knots.size
    if k < 3:
        return np.zeros((0, k))
    D = np.zeros((k - 2, k), dtype=float)
    for i in range(1, k - 1):
        h1 = knots[i] - knots[i - 1]
        h2 = knots[i + 1] - knots[i]
        if h1 <= 0 or h2 <= 0:
            continue
        span = (h1 + h2) / 2.0
        scale = np.sqrt(span) * 2.0 / (h1 + h2)
        D[i - 1, i - 1] = scale / h1
        D[i - 1, i] = -scale * (1.0 / h1 + 1.0 / h2)
        D[i - 1, i + 1] = scale / h2
    return D


def _solve_knots(x: np.ndarray, y: np.ndarray, w: np.ndarray,
                 knots: np.ndarray, smooth: float) -> np.ndarray:
    """Penalised least squares for the knot values, then made monotone.

    The result is not forced through any particular end value: the caller
    rescales afterwards, which is what lets the ends be read off the fitted
    curve instead of off one noisy patch.
    """
    B = _basis(x, knots)
    weight = w.copy()
    W = np.diag(weight)
    D = _curvature(knots)
    lam = float(smooth) * float(weight.sum())
    A = B.T @ W @ B + lam * (D.T @ D)
    rhs = B.T @ W @ y
    # A ridge term keeps the system solvable when a knot interval holds no
    # data at all and the curvature penalty alone cannot pin it down.
    A = A + np.eye(knots.size) * 1e-9 * max(1.0, float(np.trace(A)))
    try:
        values = np.linalg.solve(A, rhs)
    except np.linalg.LinAlgError:
        values = np.linalg.lstsq(A, rhs, rcond=None)[0]
    # Smoothing can leave small reversals; project onto the monotone cone.
    knot_weight = B.T @ weight
    return isotonic(values, knot_weight)


# ---------------------------------------------------------------------------
# The fit, and what to say about it
# ---------------------------------------------------------------------------

@dataclass
class FitReport:
    patches: int = 0
    levels: int = 0
    repeats: int = 0
    noise: float = 0.0           # measurement noise, as a fraction of range
    repeat_spread: float = 0.0   # disagreement between repeated patches
    smooth: float = 0.0          # how hard it ended up smoothing
    residual_rms: float = 0.0
    residual_max: float = 0.0
    outliers: int = 0
    reversals: int = 0           # non-monotone steps in the raw readings
    status: str = "ok"

    def trustworthy(self) -> bool:
        return (self.status == "ok" and self.levels >= 8
                and self.residual_rms < 0.03)

    def summary(self) -> List[str]:
        if self.status != "ok":
            return [self.status]
        out = [f"{self.patches} patches at {self.levels} levels"
               + (f", {self.repeats} of them measured more than once"
                  if self.repeats else "")]
        out.append(f"Measurement noise looks like {self.noise * 100:.1f}% of "
                   f"the paper-to-solid range. The fit will not be asked to "
                   f"beat that.")
        if self.repeats and self.repeat_spread:
            out.append(f"Patches printed twice at the same value disagree by "
                       f"{self.repeat_spread * 100:.1f}%, which is an "
                       f"independent check on that figure.")
        out.append(f"The fitted curve sits {self.residual_rms * 100:.2f}% "
                   f"from the readings on average, "
                   f"{self.residual_max * 100:.2f}% at worst.")
        if self.outliers:
            out.append(f"{self.outliers} patch(es) disagreed with their "
                       f"neighbours and were left out. Usually a speck of "
                       f"dirt or a crease on the sheet.")
        if self.reversals:
            out.append(f"{self.reversals} step(s) in the raw readings ran "
                       f"backwards - darker ink reading lighter. Noise if "
                       f"there are a few, a scanning problem if there are "
                       f"many.")
        if self.residual_rms > 0.03:
            out.append("The readings do not describe a smooth curve. Check "
                       "the scan before trusting this profile.")
        return out


def fit_response(nominal: Sequence[float], rising: Sequence[float],
                 spread: Optional[Sequence[float]] = None,
                 counts: Optional[Sequence[float]] = None,
                 smooth: Optional[float] = None,
                 robust: bool = True,
                 raw_patches: int = 0) -> Tuple[List[Point], FitReport]:
    """Fit a smooth, monotone response curve and scale it to 0..1.

    ``rising`` is any quantity that grows with ink - coverage, or darkness
    straight off the instrument. The fit is unconstrained at the ends and
    the curve is rescaled afterwards so that no ink reads 0 and the solid
    reads 1. That matters: taking those two references off the fitted curve
    uses every nearby patch to place them, where reading them off the single
    0% and 100% patches lets two noisy numbers tilt the whole profile.

    Returns the curve as knot points ready for pchip, plus a report on how
    well the readings agreed with it.
    """
    x = np.asarray(nominal, dtype=float)
    c = np.asarray(rising, dtype=float)
    report = FitReport(patches=int(raw_patches or x.size), levels=int(x.size))
    if x.size < 2:
        report.status = "not enough measurements"
        return [(0.0, 0.0), (1.0, 1.0)], report

    report.reversals = int(np.sum(np.diff(c) < -1e-9))
    cnt = np.ones_like(x) if counts is None else np.asarray(counts, dtype=float)
    sp = np.zeros_like(x) if spread is None else np.asarray(spread, dtype=float)
    report.repeats = int(np.sum(cnt > 1))

    span = float(np.max(c) - np.min(c))
    if span < 1e-9:
        report.status = ("The readings are all the same - paper and solid "
                         "cannot be told apart.")
        return [(0.0, 0.0), (1.0, 1.0)], report

    # Everything from here is in fractions of the paper-to-solid range, so a
    # number means the same thing whatever the instrument was.
    sigma = estimate_noise(x, c)
    report.noise = sigma / span
    multi = cnt > 1
    report.repeat_spread = ((float(np.mean(sp[multi])) / span)
                            if np.any(multi) else 0.0)
    weight = cnt / (1.0 + (sp / (span * 0.02)) ** 2)
    if smooth is None:
        smooth = choose_smooth(sigma, span)
    report.smooth = float(smooth)

    knots = choose_knots(x)
    keep = np.ones(x.size, dtype=bool)
    if robust and x.size >= 8:
        keep = reject_local_outliers(x, c, max(sigma,
                                               span * MIN_SIGMA_FRACTION))
        if keep.sum() < max(6, knots.size // 2):
            keep = np.ones(x.size, dtype=bool)
        report.outliers = int(np.sum(~keep))
    values = _solve_knots(x[keep], c[keep], weight[keep], knots, smooth)

    lo, hi = float(values[0]), float(values[-1])
    if hi - lo < 1e-9:
        report.status = "The fitted curve is flat - check the measurements."
        return [(0.0, 0.0), (1.0, 1.0)], report
    scaled = np.clip((values - lo) / (hi - lo), 0.0, 1.0)
    points = list(zip(knots.tolist(), scaled.tolist()))

    # Report the residuals in the same 0..1 units the curve ends up in, so
    # the number means "percent coverage" and can be compared with the
    # verification tolerance.
    fitted = pchip(points, x[keep])
    resid = (c[keep] - lo) / (hi - lo) - fitted
    report.residual_rms = float(np.sqrt(np.mean(resid ** 2)))
    report.residual_max = float(np.max(np.abs(resid))) if resid.size else 0.0
    return points, report


def fit_measurements(nominal: Sequence[float], values: Sequence[float],
                     kind: str, smooth: Optional[float] = None,
                     robust: bool = True) -> Tuple[List[Point], FitReport]:
    """Fit straight from raw instrument readings. The main entry point.

    Going through coverage first would mean dividing by a paper-white and a
    solid taken from one patch each, and clamping the result to 0..1 - which
    throws away the sign of the noise in exactly the highlights where it
    matters most. Fitting the luminance and normalising the finished curve
    avoids both.

    Repeated levels are pooled here rather than by the caller, so that the
    disagreement between repeats is measured in the same units the fit
    works in and can be used to weight them.
    """
    y = to_luminance(values, kind)
    # Darkness rises with ink, so the fit sees a non-decreasing quantity.
    pooled = pool_readings(nominal, (-y).tolist())
    return fit_response(pooled.nominal, pooled.value, spread=pooled.spread,
                        counts=pooled.count, smooth=smooth, robust=robust,
                        raw_patches=pooled.raw_patches)


def build_profile(nominal: Sequence[float], values: Sequence[float],
                  kind: str, name: str = "Measured printer",
                  smooth: Optional[float] = None,
                  template: Optional[PrinterProfile] = None,
                  ) -> Tuple[PrinterProfile, FitReport]:
    """Raw readings in, a profile carrying a fitted response curve out."""
    if len(nominal) < 2:
        raise ValueError("At least two patches are needed.")
    points, report = fit_measurements(nominal, values, kind, smooth=smooth)
    p = template.copy() if template is not None else PrinterProfile()
    p.name = name
    p.measure_kind = kind
    p.nominal = [float(v) for v in nominal]
    p.measured = [float(v) for v in values]
    p.response_points = [(float(a), float(b)) for a, b in points]
    p.fit_rms = report.residual_rms
    p.fit_outliers = report.outliers
    p.measurement_noise = report.noise
    p.passes = 1
    p.built = time.time()
    p.min_dot, p.max_ink = p.suggested_limits()
    return p, report


# ---------------------------------------------------------------------------
# Verification: did the correction actually work
# ---------------------------------------------------------------------------

@dataclass
class VerifyReport:
    wanted: List[float] = field(default_factory=list)
    achieved: List[float] = field(default_factory=list)
    error_rms: float = 0.0
    error_max: float = 0.0
    worst_at: float = 0.0
    tolerance: float = 0.03
    levels: int = 0
    status: str = "ok"

    @property
    def passed(self) -> bool:
        return self.status == "ok" and self.error_max <= self.tolerance

    def table(self) -> List[Tuple[float, float, float]]:
        return [(w, a, a - w) for w, a in zip(self.wanted, self.achieved)]

    def summary(self) -> List[str]:
        if self.status != "ok":
            return [self.status]
        out = [f"Asked for {self.levels} levels; what came back is "
               f"{self.error_rms * 100:.2f}% out on average and "
               f"{self.error_max * 100:.2f}% out at worst "
               f"(near {self.worst_at * 100:.0f}% ink)."]
        if self.passed:
            out.append(f"That is inside the {self.tolerance * 100:.0f}% "
                       f"tolerance: the printer is linear and the profile is "
                       f"doing its job.")
        else:
            out.append(f"That is outside the {self.tolerance * 100:.0f}% "
                       f"tolerance. Refining folds this measurement back "
                       f"into the profile, which normally fixes it in one "
                       f"more pass.")
        worst_end = ("highlights" if self.worst_at < 0.35
                     else "shadows" if self.worst_at > 0.65 else "midtones")
        if not self.passed:
            out.append(f"The error is worst in the {worst_end}.")
        return out


def verify(profile: PrinterProfile, wanted: Sequence[float],
           values: Sequence[float], kind: Optional[str] = None,
           tolerance: float = 0.03) -> VerifyReport:
    """Measure a target that was printed through ``profile``'s correction.

    ``wanted`` is the coverage each patch asked for - what the verification
    sheet aimed at, not what it sent to the printer. If the profile is
    right, the measured coverage comes back equal to it.
    """
    pooled = pool_readings(wanted, values)
    report = VerifyReport(tolerance=float(tolerance),
                          levels=int(pooled.nominal.size))
    if pooled.nominal.size < 3:
        report.status = "not enough patches to verify"
        return report
    kind = kind or profile.measure_kind
    achieved = measurements_to_coverage(pooled.nominal.tolist(),
                                        pooled.value.tolist(), kind)
    achieved = isotonic(achieved, pooled.count)
    report.wanted = [float(v) for v in pooled.nominal]
    report.achieved = [float(v) for v in achieved]
    err = achieved - pooled.nominal
    # The two ends are pinned by the normalisation itself, so they cannot
    # disagree and should not be allowed to flatter the result.
    interior = np.ones(err.size, dtype=bool)
    interior[0] = interior[-1] = False
    if interior.sum() >= 1:
        e = err[interior]
        report.error_rms = float(np.sqrt(np.mean(e ** 2)))
        worst = int(np.argmax(np.abs(e)))
        report.error_max = float(np.abs(e[worst]))
        report.worst_at = float(pooled.nominal[interior][worst])
    return report


def refine(profile: PrinterProfile, wanted: Sequence[float],
           values: Sequence[float], kind: Optional[str] = None,
           smooth: Optional[float] = None,
           name: Optional[str] = None) -> Tuple[PrinterProfile, FitReport]:
    """Fold a verification measurement back into the profile.

    The first pass measures the printer raw and inverts it, which is only
    as good as the fit. A verification sheet measures it again through that
    correction, and those readings say something stronger: for each patch we
    know both what was actually sent to the printer - the old correction
    applied to the wanted value - and what came back. That is a fresh
    sample of the response curve, taken where it matters, so refitting on
    it converges rather than just repeating the first estimate.
    """
    kind = kind or profile.measure_kind
    want = np.asarray(wanted, dtype=float)
    vals = np.asarray(values, dtype=float)
    if want.size != vals.size:
        raise ValueError("wanted and values must be the same length")
    if np.unique(want).size < 3:
        raise ValueError("Not enough patches to refine from.")

    # What each patch actually asked the printer for.
    lut = profile.linearisation_lut(1024)
    grid = np.linspace(0.0, 1.0, lut.size)
    sent = np.interp(want, grid, lut)

    points, report = fit_measurements(sent.tolist(), vals.tolist(), kind,
                                      smooth=smooth)
    out = profile.copy()
    out.name = name or profile.name
    out.measure_kind = kind
    # Keep the record honest: nominal is what was sent, measured is what
    # came back, which is exactly the pairing the response curve describes.
    order = np.argsort(sent)
    out.nominal = [float(v) for v in sent[order]]
    out.measured = [float(v) for v in vals[order]]
    out.response_points = [(float(a), float(b)) for a, b in points]
    out.fit_rms = report.residual_rms
    out.fit_outliers = report.outliers
    out.measurement_noise = report.noise
    out.passes = int(getattr(profile, "passes", 1) or 1) + 1
    out.built = time.time()
    out.min_dot, out.max_ink = out.suggested_limits()
    return out, report
