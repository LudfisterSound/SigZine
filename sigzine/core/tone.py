"""Tone reproduction for laser printing.

A photograph that looks right on screen prints muddy on a laser printer:
the midtones fill in (dot gain), the deep shadows block up into one flat
black and the lightest highlights drop off the paper entirely. This module
builds the correction, either from a measured printer profile (see
calibration.py) or from a parametric dot-gain model, and applies it along
with the usual photographic controls.

Pipeline
    RGB -> grey -> input levels -> gamma -> contrast -> user curve
        -> clarity / unsharp  (spatial, done at output resolution)
        -> printer linearisation -> ink limits -> screening -> device image
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageFilter

Point = Tuple[float, float]

GRAY_MODES = {
    "luminosity": (0.2126, 0.7152, 0.0722),
    "bt601": (0.299, 0.587, 0.114),
    "average": (1 / 3, 1 / 3, 1 / 3),
    "orthochromatic": (0.05, 0.55, 0.40),
    "panchromatic warm": (0.45, 0.45, 0.10),
    "red filter": (0.85, 0.12, 0.03),
    "green filter": (0.10, 0.80, 0.10),
    "blue filter": (0.05, 0.25, 0.70),
    "red channel": (1.0, 0.0, 0.0),
    "green channel": (0.0, 1.0, 0.0),
    "blue channel": (0.0, 0.0, 1.0),
}

SCREENS = ["none", "floyd-steinberg", "ordered (bayer)", "halftone dot",
           "stochastic", "threshold"]


# ---------------------------------------------------------------------------
# Monotone cubic interpolation (Fritsch-Carlson), used for every curve
# ---------------------------------------------------------------------------

def pchip(points: Sequence[Point], xs: np.ndarray) -> np.ndarray:
    pts = sorted((float(x), float(y)) for x, y in points)
    # de-duplicate x
    clean: List[Point] = []
    for x, y in pts:
        if clean and abs(x - clean[-1][0]) < 1e-9:
            clean[-1] = (x, y)
        else:
            clean.append((x, y))
    if len(clean) == 1:
        return np.full_like(xs, clean[0][1], dtype=float)
    x = np.array([p[0] for p in clean], dtype=float)
    y = np.array([p[1] for p in clean], dtype=float)
    n = len(x)
    h = np.diff(x)
    delta = np.diff(y) / h
    d = np.zeros(n)
    if n == 2:
        d[:] = delta[0]
    else:
        for i in range(1, n - 1):
            if delta[i - 1] * delta[i] <= 0:
                d[i] = 0.0
            else:
                w1 = 2 * h[i] + h[i - 1]
                w2 = h[i] + 2 * h[i - 1]
                d[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
        d[0] = _end_slope(h[0], h[1], delta[0], delta[1])
        d[-1] = _end_slope(h[-1], h[-2], delta[-1], delta[-2])
    idx = np.clip(np.searchsorted(x, xs) - 1, 0, n - 2)
    hi = h[idx]
    t = (xs - x[idx]) / hi
    t2, t3 = t * t, t * t * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    out = (h00 * y[idx] + h10 * hi * d[idx] +
           h01 * y[idx + 1] + h11 * hi * d[idx + 1])
    return np.clip(out, 0.0, 1.0)


def _end_slope(h0: float, h1: float, d0: float, d1: float) -> float:
    d = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if d * d0 <= 0:
        return 0.0
    if abs(d) > 3 * abs(d0):
        return 3 * d0
    return d


def curve_lut(points: Sequence[Point], size: int = 256) -> np.ndarray:
    """A float LUT in 0..1 sampled on a regular 0..1 grid."""
    xs = np.linspace(0.0, 1.0, size)
    return pchip(points, xs)


def invert_lut(lut: np.ndarray, size: int = 256) -> np.ndarray:
    """Numerically invert a monotonically increasing 0..1 LUT."""
    xs = np.linspace(0.0, 1.0, len(lut))
    mono = np.maximum.accumulate(np.clip(lut, 0.0, 1.0))
    ys = np.linspace(0.0, 1.0, size)
    return np.clip(np.interp(ys, mono, xs), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class ToneSettings:
    enabled: bool = True
    gray_mode: str = "luminosity"
    auto_levels: bool = False
    auto_clip: float = 0.2              # percent clipped at each end
    black_point: float = 0.0            # input level that becomes black
    white_point: float = 1.0
    gamma: float = 1.0                  # >1 lightens midtones
    brightness: float = 0.0             # -1..1
    contrast: float = 0.0               # -1..1 S-curve strength
    curve: List[Point] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    clarity: float = 0.0                # local contrast, 0..1
    sharpen: float = 0.0                # unsharp amount 0..3
    sharpen_radius: float = 1.2         # pixels at device resolution
    sharpen_threshold: int = 2
    invert: bool = False

    # press behaviour
    use_profile: bool = True            # apply the measured linearisation
    dot_gain: float = 0.18              # parametric fallback, gain at 50%
    min_dot: float = 0.02               # smallest ink the printer holds
    max_ink: float = 1.0                # darkest ink before it blocks up
    paper_white: bool = True            # keep specular white free of toner

    # screening
    screen: str = "none"
    screen_lpi: float = 85.0
    screen_angle: float = 45.0
    device_dpi: int = 600
    render_dpi: int = 300               # contone output resolution

    def copy(self) -> "ToneSettings":
        import copy as _c
        return _c.deepcopy(self)


PRESETS: Dict[str, Dict] = {
    "Laser photo (default)": dict(
        contrast=0.18, gamma=1.06, dot_gain=0.18, min_dot=0.03, max_ink=1.0,
        sharpen=0.6, sharpen_radius=1.2, clarity=0.15,
        curve=[(0.0, 0.0), (0.22, 0.26), (0.5, 0.52), (0.8, 0.83), (1.0, 1.0)]),
    "Laser photo (punchy)": dict(
        contrast=0.38, gamma=1.0, dot_gain=0.20, min_dot=0.04, max_ink=1.0,
        sharpen=1.0, clarity=0.3,
        curve=[(0.0, 0.0), (0.2, 0.15), (0.5, 0.5), (0.82, 0.9), (1.0, 1.0)]),
    "Laser photo (soft / open shadows)": dict(
        contrast=0.05, gamma=1.18, dot_gain=0.16, min_dot=0.03, max_ink=0.86,
        sharpen=0.4, clarity=0.1,
        curve=[(0.0, 0.06), (0.25, 0.34), (0.55, 0.6), (1.0, 1.0)]),
    "Newsprint / heavy dot gain": dict(
        contrast=0.1, gamma=1.25, dot_gain=0.3, min_dot=0.06, max_ink=0.85,
        sharpen=0.8, clarity=0.2,
        curve=[(0.0, 0.02), (0.3, 0.42), (0.7, 0.78), (1.0, 1.0)]),
    "Photocopy look": dict(
        contrast=0.75, gamma=0.95, dot_gain=0.1, min_dot=0.0, max_ink=1.0,
        sharpen=1.4, clarity=0.4, screen="floyd-steinberg",
        curve=[(0.0, 0.0), (0.35, 0.12), (0.62, 0.9), (1.0, 1.0)]),
    "High contrast line art": dict(
        contrast=0.0, gamma=1.0, dot_gain=0.0, min_dot=0.0, max_ink=1.0,
        sharpen=0.0, screen="threshold",
        curve=[(0.0, 0.0), (0.45, 0.0), (0.55, 1.0), (1.0, 1.0)]),
    "Halftone 85 lpi": dict(
        contrast=0.2, gamma=1.05, dot_gain=0.15, min_dot=0.02, max_ink=0.95,
        sharpen=0.5, screen="halftone dot", screen_lpi=85, screen_angle=45),
    "Halftone 53 lpi (coarse / zine)": dict(
        contrast=0.25, gamma=1.08, dot_gain=0.15, min_dot=0.0, max_ink=1.0,
        sharpen=0.7, screen="halftone dot", screen_lpi=53, screen_angle=45),
    "Dithered (Floyd-Steinberg)": dict(
        contrast=0.15, gamma=1.1, dot_gain=0.12, min_dot=0.0, max_ink=1.0,
        sharpen=0.5, screen="floyd-steinberg"),
    "Screen / digital copy": dict(
        contrast=0.0, gamma=1.0, dot_gain=0.0, min_dot=0.0, max_ink=1.0,
        sharpen=0.0, use_profile=False, screen="none", render_dpi=200),
    "Neutral (no correction)": dict(
        contrast=0.0, gamma=1.0, dot_gain=0.0, min_dot=0.0, max_ink=1.0,
        sharpen=0.0, clarity=0.0, use_profile=False, screen="none",
        curve=[(0.0, 0.0), (1.0, 1.0)]),
}


def apply_preset(settings: ToneSettings, name: str) -> ToneSettings:
    s = settings.copy()
    for k, v in PRESETS[name].items():
        setattr(s, k, list(v) if isinstance(v, list) else v)
    s.enabled = True
    return s


# ---------------------------------------------------------------------------
# LUT construction
# ---------------------------------------------------------------------------

def _contrast_points(strength: float) -> List[Point]:
    a = max(-0.98, min(0.98, strength)) * 0.25
    return [(0.0, 0.0), (0.25, 0.25 - a), (0.5, 0.5), (0.75, 0.75 + a), (1.0, 1.0)]


def tone_lut(s: ToneSettings, size: int = 256) -> np.ndarray:
    """Image-space LUT (luminance in -> luminance out), before any ink maths."""
    x = np.linspace(0.0, 1.0, size)
    bp, wp = float(s.black_point), float(s.white_point)
    if wp - bp < 1e-4:
        wp = bp + 1e-4
    v = np.clip((x - bp) / (wp - bp), 0.0, 1.0)
    if abs(s.gamma - 1.0) > 1e-4:
        v = np.power(v, 1.0 / max(0.05, s.gamma))
    if abs(s.brightness) > 1e-4:
        b = float(np.clip(s.brightness, -1, 1))
        v = np.clip(v + b * (1.0 - np.abs(2 * v - 1)) * 0.5 + b * 0.25, 0.0, 1.0)
    if abs(s.contrast) > 1e-4:
        v = pchip(_contrast_points(s.contrast), v)
    if s.curve and len(s.curve) >= 2:
        v = pchip(s.curve, v)
    if s.invert:
        v = 1.0 - v
    return np.clip(v, 0.0, 1.0)


def ink_limit_curve(s: ToneSettings) -> List[Point]:
    """Ink in -> ink out, holding paper white and capping the solid."""
    lo = float(np.clip(s.min_dot, 0.0, 0.3))
    hi = float(np.clip(s.max_ink, 0.4, 1.0))
    pts: List[Point] = [(0.0, 0.0)]
    if lo > 0.001:
        pts.append((0.02, lo))
        pts.append((0.5, lo + 0.5 * (hi - lo)))
    else:
        pts.append((0.5, 0.5 * hi))
    pts.append((1.0, hi))
    return pts


def dot_gain_response(gain: float, size: int = 256) -> np.ndarray:
    """What the printer actually lays down for a requested ink value.

    A single-parameter model: gain is the extra coverage measured at 50%.
    The curve is pinned at 0 and 1 and peaks in the midtones.
    """
    x = np.linspace(0.0, 1.0, size)
    return np.clip(x + gain * np.sin(np.pi * x), 0.0, 1.0)


def ink_lut(s: ToneSettings, profile=None, size: int = 256) -> np.ndarray:
    """Luminance in (0..1) -> luminance to send to the printer (0..1).

    Internally works in ink coverage, which is 1 - luminance.
    """
    lum = np.linspace(0.0, 1.0, size)
    ink = 1.0 - lum
    ink = pchip(ink_limit_curve(s), ink)
    if s.use_profile and profile is not None and profile.has_linearisation():
        corr = profile.linearisation_lut(size)
        ink = np.interp(ink, np.linspace(0.0, 1.0, len(corr)), corr)
    elif s.dot_gain > 1e-4:
        comp = invert_lut(dot_gain_response(s.dot_gain, size), size)
        ink = np.interp(ink, np.linspace(0.0, 1.0, size), comp)
    if s.paper_white:
        ink = np.where(lum >= 0.999, 0.0, ink)
    return np.clip(1.0 - ink, 0.0, 1.0)


def combined_lut(s: ToneSettings, profile=None, size: int = 256) -> np.ndarray:
    """Everything that is a pure per-pixel map, for previews and ramps."""
    t = tone_lut(s, size)
    ink = ink_lut(s, profile, size)
    return np.interp(t, np.linspace(0.0, 1.0, size), ink)


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=float) / 64.0

_BLUE_NOISE_CACHE: Dict[int, np.ndarray] = {}


def blue_noise(size: int = 64) -> np.ndarray:
    """Void-and-cluster blue noise threshold matrix in 0..1 (cached on disk)."""
    if size in _BLUE_NOISE_CACHE:
        return _BLUE_NOISE_CACHE[size]
    try:
        from .paths import cache_dir
        cache_file = cache_dir() / f"bluenoise{size}.npy"
        if cache_file.exists():
            m = np.load(cache_file)
            _BLUE_NOISE_CACHE[size] = m
            return m
    except Exception:
        cache_file = None
    rng = np.random.default_rng(7)
    n = size * size
    binary = np.zeros((size, size), dtype=bool)
    initial = max(1, n // 10)
    flat = rng.permutation(n)[:initial]
    binary.flat[flat] = True

    # gaussian filter on the torus, via FFT
    yy, xx = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    d = np.minimum(yy, size - yy) ** 2 + np.minimum(xx, size - xx) ** 2
    kernel = np.exp(-d / (2 * 1.5 ** 2))
    kf = np.fft.rfft2(kernel)

    def energy(b):
        return np.fft.irfft2(np.fft.rfft2(b.astype(float)) * kf, b.shape)

    # 1. break up the initial pattern
    while True:
        e = energy(binary)
        cluster = np.where(binary, e, -np.inf).argmax()
        binary.flat[cluster] = False
        e = energy(binary)
        void = np.where(~binary, e, np.inf).argmin()
        if void == cluster:
            binary.flat[cluster] = True
            break
        binary.flat[void] = True

    rank = np.full((size, size), -1, dtype=int)
    work = binary.copy()
    ones = int(work.sum())
    for r in range(ones - 1, -1, -1):
        e = energy(work)
        cluster = np.where(work, e, -np.inf).argmax()
        work.flat[cluster] = False
        rank.flat[cluster] = r
    work = binary.copy()
    for r in range(ones, n):
        e = energy(work)
        void = np.where(~work, e, np.inf).argmin()
        work.flat[void] = True
        rank.flat[void] = r
    matrix = (rank.astype(float) + 0.5) / n
    _BLUE_NOISE_CACHE[size] = matrix
    try:
        if cache_file is not None:
            np.save(cache_file, matrix)
    except Exception:
        pass
    return matrix


def _tile(matrix: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    h, w = shape
    mh, mw = matrix.shape
    return np.tile(matrix, (h // mh + 1, w // mw + 1))[:h, :w]


def halftone_threshold(shape: Tuple[int, int], dpi: float, lpi: float,
                       angle: float) -> np.ndarray:
    """Classic clustered-dot screen as a threshold field in 0..1."""
    h, w = shape
    cells = max(1.5, dpi / max(5.0, lpi))
    a = math.radians(angle)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    u = (x * math.cos(a) + y * math.sin(a)) / cells
    v = (-x * math.sin(a) + y * math.cos(a)) / cells
    spot = (np.cos(2 * np.pi * u) + np.cos(2 * np.pi * v)) * 0.5
    return (spot + 1.0) * 0.5


def screen_image(img: Image.Image, s: ToneSettings) -> Image.Image:
    """Turn an 8-bit grey image into the 1-bit bitmap the printer will image."""
    mode = s.screen
    if mode in ("none", None, ""):
        return img
    if mode == "floyd-steinberg":
        return img.convert("1", dither=Image.FLOYDSTEINBERG)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    if mode == "threshold":
        thr = np.full(arr.shape, 0.5, dtype=np.float32)
    elif mode == "ordered (bayer)":
        thr = _tile(_BAYER8, arr.shape).astype(np.float32)
    elif mode == "stochastic":
        thr = _tile(blue_noise(64), arr.shape).astype(np.float32)
    elif mode == "halftone dot":
        thr = halftone_threshold(arr.shape, s.device_dpi, s.screen_lpi,
                                 s.screen_angle).astype(np.float32)
    else:
        return img
    out = (arr > thr).astype(np.uint8) * 255
    return Image.fromarray(out).convert("1")


# ---------------------------------------------------------------------------
# Applying the pipeline
# ---------------------------------------------------------------------------

def to_gray(img: Image.Image, mode: str = "luminosity") -> Image.Image:
    if img.mode in ("L", "1", "I;16"):
        return img.convert("L")
    if img.mode in ("RGBA", "LA", "PA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    img = img.convert("RGB")
    w = GRAY_MODES.get(mode, GRAY_MODES["luminosity"])
    total = sum(w) or 1.0
    arr = np.asarray(img, dtype=np.float32)
    g = (arr[..., 0] * w[0] + arr[..., 1] * w[1] + arr[..., 2] * w[2]) / total
    return Image.fromarray(np.clip(g, 0, 255).astype(np.uint8))


def auto_levels(img: Image.Image, clip_percent: float = 0.2) -> Tuple[float, float]:
    hist = np.asarray(img.histogram()[:256], dtype=np.float64)
    total = hist.sum()
    if total <= 0:
        return (0.0, 1.0)
    cdf = np.cumsum(hist) / total
    lo = float(np.searchsorted(cdf, clip_percent / 100.0)) / 255.0
    hi = float(np.searchsorted(cdf, 1.0 - clip_percent / 100.0)) / 255.0
    if hi - lo < 0.05:
        return (0.0, 1.0)
    return (lo, hi)


def apply_lut(img: Image.Image, lut: np.ndarray) -> Image.Image:
    table = np.clip(np.interp(np.arange(256) / 255.0,
                              np.linspace(0, 1, len(lut)), lut) * 255.0,
                    0, 255).astype(np.uint8)
    arr = np.asarray(img.convert("L"))
    return Image.fromarray(table[arr])


def prescale(img: Image.Image,
             target_px: Optional[Tuple[int, int]]) -> Image.Image:
    """Throw away resolution we are about to discard anyway.

    Everything downstream is per-pixel or a filter, so working on a 24
    megapixel original to make a 600 pixel preview is pure waste. Two times
    the target is kept so the final Lanczos step still has something to
    average.
    """
    if not target_px:
        return img
    fx = img.width // max(1, target_px[0] * 2)
    fy = img.height // max(1, target_px[1] * 2)
    factor = max(1, min(fx, fy))
    if factor > 1:
        try:
            return img.reduce(factor)
        except Exception:
            return img
    return img


def apply_tone(img: Image.Image, s: ToneSettings, profile=None,
               target_px: Optional[Tuple[int, int]] = None,
               for_screen: bool = False) -> Image.Image:
    """Full pipeline. ``for_screen`` skips press correction and screening."""
    img = prescale(img, target_px)
    g = to_gray(img, s.gray_mode)
    if not s.enabled:
        if target_px:
            g = g.resize(target_px, Image.LANCZOS)
        return g

    work = s
    if s.auto_levels:
        work = s.copy()
        work.black_point, work.white_point = auto_levels(g, s.auto_clip)

    g = apply_lut(g, tone_lut(work))

    if target_px and target_px != g.size:
        resample = Image.LANCZOS if (target_px[0] * target_px[1] <
                                     g.size[0] * g.size[1]) else Image.BICUBIC
        g = g.resize(target_px, resample)

    if work.clarity > 0.01:
        radius = max(8.0, min(g.size) / 40.0)
        g = g.filter(ImageFilter.UnsharpMask(
            radius=radius, percent=int(work.clarity * 80), threshold=0))
    if work.sharpen > 0.01:
        g = g.filter(ImageFilter.UnsharpMask(
            radius=max(0.3, work.sharpen_radius),
            percent=int(min(3.0, work.sharpen) * 100),
            threshold=int(work.sharpen_threshold)))

    if for_screen:
        return g
    g = apply_lut(g, ink_lut(work, profile))
    return screen_image(g, work)


def ramp_preview(s: ToneSettings, profile=None, width: int = 512,
                 height: int = 48) -> Image.Image:
    """A 0-100% ramp pushed through the ink pipeline, for the UI."""
    x = np.linspace(255, 0, width).astype(np.uint8)
    img = Image.fromarray(np.tile(x, (height, 1)))
    out = apply_lut(img, combined_lut(s, profile))
    return screen_image(out, s) if s.screen != "none" else out


# ---------------------------------------------------------------------------
# Print simulation - what the paper will look like, for on-screen previews
# ---------------------------------------------------------------------------

def print_response_lut(s: ToneSettings, profile=None, size: int = 256) -> np.ndarray:
    """Sent luminance -> luminance you will actually see on the paper."""
    lum = np.linspace(0.0, 1.0, size)
    ink = 1.0 - lum
    if profile is not None and getattr(profile, "has_linearisation", None) \
            and profile.has_linearisation():
        resp = profile.response_lut(size)
    else:
        resp = dot_gain_response(s.dot_gain, size)
    printed = np.interp(ink, np.linspace(0.0, 1.0, size), resp)
    return np.clip(1.0 - printed, 0.0, 1.0)


def simulate_print(img: Image.Image, s: ToneSettings, profile=None,
                   spread: float = 0.0) -> Image.Image:
    """Approximate the printed result of an already-corrected image."""
    g = img.convert("L")
    if spread > 0:
        g = g.filter(ImageFilter.GaussianBlur(radius=spread))
    elif img.mode == "1":
        g = g.filter(ImageFilter.GaussianBlur(radius=0.6))
    return apply_lut(g, print_response_lut(s, profile))
