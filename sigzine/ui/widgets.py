"""Reusable widgets: the curve editor, small plots and image views."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------

def pil_to_qimage(img: Image.Image) -> QImage:
    if img.mode == "1":
        img = img.convert("L")
    if img.mode == "L":
        data = img.tobytes()
        qi = QImage(data, img.width, img.height, img.width,
                    QImage.Format.Format_Grayscale8)
    else:
        img = img.convert("RGB")
        data = img.tobytes()
        qi = QImage(data, img.width, img.height, img.width * 3,
                    QImage.Format.Format_RGB888)
    return qi.copy()


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    return QPixmap.fromImage(pil_to_qimage(img))


# ---------------------------------------------------------------------------
# helpers for building forms quickly
# ---------------------------------------------------------------------------

class SliderSpin(QWidget):
    """A slider and a spin box that stay in step, for a float value."""
    valueChanged = Signal(float)

    def __init__(self, minimum: float, maximum: float, value: float,
                 step: float = 0.01, decimals: int = 2, suffix: str = "",
                 parent=None) -> None:
        super().__init__(parent)
        self._min, self._max, self._step = minimum, maximum, step
        self._guard = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimumWidth(110)
        self.slider.setRange(0, max(1, int(round((maximum - minimum) / step))))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)
        self.spin.setFixedWidth(88)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin, 0)
        self.set_value(value)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v: int) -> None:
        if self._guard:
            return
        self._guard = True
        val = self._min + v * self._step
        self.spin.setValue(val)
        self._guard = False
        self.valueChanged.emit(val)

    def _from_spin(self, val: float) -> None:
        if self._guard:
            return
        self._guard = True
        self.slider.setValue(int(round((val - self._min) / self._step)))
        self._guard = False
        self.valueChanged.emit(val)

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, val: float) -> None:
        self._guard = True
        self.spin.setValue(val)
        self.slider.setValue(int(round((val - self._min) / self._step)))
        self._guard = False


def section(title: str) -> QLabel:
    lab = QLabel(title.upper())
    f = lab.font()
    f.setBold(True)
    f.setPointSizeF(max(8.0, f.pointSizeF() - 1.0))
    lab.setFont(f)
    lab.setStyleSheet("color: palette(mid); margin-top: 8px;")
    return lab


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


# ---------------------------------------------------------------------------
# the tone curve editor
# ---------------------------------------------------------------------------

class CurveEditor(QWidget):
    """Draggable monotone curve. x and y run 0..1 with y drawn upward."""
    curveChanged = Signal(list)
    editingFinished = Signal()

    def __init__(self, points: Optional[Sequence[Point]] = None, parent=None):
        super().__init__(parent)
        self.points: List[Point] = [tuple(p) for p in
                                    (points or [(0.0, 0.0), (1.0, 1.0)])]
        self.histogram: Optional[np.ndarray] = None
        self.effective: Optional[np.ndarray] = None   # composed LUT overlay
        self._drag = -1
        self._hover = -1
        self.setMinimumSize(240, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- data ------------------------------------------------------------
    def set_points(self, points: Sequence[Point]) -> None:
        self.points = sorted((float(x), float(y)) for x, y in points)
        # indices from the old curve are meaningless now
        self._drag = -1
        self._hover = -1
        self.update()

    def set_histogram(self, hist: Optional[np.ndarray]) -> None:
        if hist is not None and hist.max() > 0:
            self.histogram = hist.astype(float) / float(hist.max())
        else:
            self.histogram = None
        self.update()

    def set_effective(self, lut: Optional[np.ndarray]) -> None:
        self.effective = lut
        self.update()

    def reset(self) -> None:
        self.points = [(0.0, 0.0), (1.0, 1.0)]
        self._drag = -1
        self._hover = -1
        self.curveChanged.emit(list(self.points))
        self.editingFinished.emit()
        self.update()

    # -- geometry ---------------------------------------------------------
    def _plot_rect(self) -> QRectF:
        m = 10.0
        return QRectF(m, m, max(10.0, self.width() - 2 * m),
                      max(10.0, self.height() - 2 * m))

    def _to_px(self, p: Point) -> QPointF:
        r = self._plot_rect()
        return QPointF(r.left() + p[0] * r.width(),
                       r.bottom() - p[1] * r.height())

    def _to_val(self, pos) -> Point:
        r = self._plot_rect()
        x = (pos.x() - r.left()) / r.width()
        y = (r.bottom() - pos.y()) / r.height()
        return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))

    def _nearest(self, pos, radius: float = 11.0) -> int:
        best, bd = -1, radius ** 2
        for i, p in enumerate(self.points):
            q = self._to_px(p)
            d = (q.x() - pos.x()) ** 2 + (q.y() - pos.y()) ** 2
            if d < bd:
                best, bd = i, d
        return best

    # -- events -----------------------------------------------------------
    def mousePressEvent(self, ev) -> None:
        pos = ev.position()
        idx = self._nearest(pos)
        if ev.button() == Qt.MouseButton.RightButton:
            if 0 < idx < len(self.points) - 1:
                del self.points[idx]
                self.curveChanged.emit(list(self.points))
                self.editingFinished.emit()
                self.update()
            return
        if idx < 0:
            v = self._to_val(pos)
            self.points.append(v)
            self.points.sort()
            idx = self.points.index(v)
        self._drag = idx
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        if self._drag < 0:
            h = self._nearest(pos)
            if h != self._hover:
                self._hover = h
                self.setCursor(Qt.CursorShape.SizeAllCursor if h >= 0
                               else Qt.CursorShape.CrossCursor)
                self.update()
            return
        x, y = self._to_val(pos)
        i = self._drag
        if i == 0:
            x = 0.0
        elif i == len(self.points) - 1:
            x = 1.0
        else:
            lo = self.points[i - 1][0] + 0.005
            hi = self.points[i + 1][0] - 0.005
            x = min(max(x, lo), hi)
        self.points[i] = (x, y)
        self.curveChanged.emit(list(self.points))
        self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if self._drag >= 0:
            self._drag = -1
            self.editingFinished.emit()
            self.update()

    def mouseDoubleClickEvent(self, ev) -> None:
        idx = self._nearest(ev.position())
        if 0 < idx < len(self.points) - 1:
            del self.points[idx]
            self.curveChanged.emit(list(self.points))
            self.editingFinished.emit()
            self.update()

    # -- painting ---------------------------------------------------------
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            # a painter left active by an exception takes the whole app down
            p.end()

    def _paint(self, p: QPainter) -> None:
        from ..core.tone import pchip
        if self._drag >= len(self.points):
            self._drag = -1
        if self._hover >= len(self.points):
            self._hover = -1
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._plot_rect()
        p.fillRect(self.rect(), self.palette().base())
        p.fillRect(r, QColor(255, 255, 255, 12))

        if self.histogram is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(128, 128, 128, 70))
            n = len(self.histogram)
            w = r.width() / n
            for i, v in enumerate(self.histogram):
                h = v ** 0.45 * r.height()
                p.drawRect(QRectF(r.left() + i * w, r.bottom() - h, w + 0.6, h))

        pen = QPen(QColor(128, 128, 128, 90), 1.0, Qt.PenStyle.DotLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(1, 4):
            x = r.left() + r.width() * i / 4
            y = r.top() + r.height() * i / 4
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(QColor(128, 128, 128, 120), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(self._to_px((0.0, 0.0)), self._to_px((1.0, 1.0)))
        p.setPen(QPen(self.palette().mid().color(), 1.0))
        p.drawRect(r)

        xs = np.linspace(0.0, 1.0, 256)
        if self.effective is not None:
            path = QPainterPath()
            for i, x in enumerate(xs):
                q = self._to_px((float(x), float(self.effective[i])))
                path.moveTo(q) if i == 0 else path.lineTo(q)
            p.setPen(QPen(QColor(210, 120, 40, 190), 1.4, Qt.PenStyle.DashLine))
            p.drawPath(path)

        ys = pchip(self.points, xs)
        path = QPainterPath()
        for i, x in enumerate(xs):
            q = self._to_px((float(x), float(ys[i])))
            path.moveTo(q) if i == 0 else path.lineTo(q)
        p.setPen(QPen(self.palette().highlight().color(), 2.0))
        p.drawPath(path)

        for i, pt in enumerate(self.points):
            q = self._to_px(pt)
            active = (i == self._drag or i == self._hover)
            p.setBrush(QBrush(self.palette().highlight().color() if active
                              else self.palette().base().color()))
            p.setPen(QPen(self.palette().highlight().color(), 1.6))
            rad = 5.5 if active else 4.0
            p.drawEllipse(q, rad, rad)

        if self._hover >= 0 or self._drag >= 0:
            i = self._drag if self._drag >= 0 else self._hover
            x, y = self.points[i]
            p.setPen(self.palette().text().color())
            f = p.font()
            f.setPointSizeF(8.5)
            p.setFont(f)
            p.drawText(QRectF(r.left() + 4, r.top() + 2, r.width() - 8, 16),
                       int(Qt.AlignmentFlag.AlignLeft),
                       f"in {x * 100:.0f}   out {y * 100:.0f}")


# ---------------------------------------------------------------------------
# small line plot, used for the printer response
# ---------------------------------------------------------------------------

class MiniPlot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.series: List[Tuple[np.ndarray, QColor, str, bool]] = []
        self.scatter: List[Tuple[np.ndarray, np.ndarray, QColor]] = []
        self.xlabel = ""
        self.ylabel = ""
        self.setMinimumHeight(180)

    def clear(self) -> None:
        self.series.clear()
        self.scatter.clear()
        self.update()

    def add(self, y: np.ndarray, color: QColor, label: str = "",
            dashed: bool = False) -> None:
        self.series.append((np.asarray(y, dtype=float), color, label, dashed))
        self.update()

    def add_points(self, x: np.ndarray, y: np.ndarray, color: QColor) -> None:
        self.scatter.append((np.asarray(x, dtype=float),
                             np.asarray(y, dtype=float), color))
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        m = 22.0
        r = QRectF(m, 8, max(10.0, self.width() - m - 10),
                   max(10.0, self.height() - m - 8))
        p.fillRect(self.rect(), self.palette().base())
        p.setPen(QPen(QColor(128, 128, 128, 80), 1.0, Qt.PenStyle.DotLine))
        for i in range(1, 4):
            x = r.left() + r.width() * i / 4
            y = r.top() + r.height() * i / 4
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(QColor(128, 128, 128, 130), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.top()))
        p.setPen(QPen(self.palette().mid().color(), 1.0))
        p.drawRect(r)

        def px(xv: float, yv: float) -> QPointF:
            return QPointF(r.left() + xv * r.width(), r.bottom() - yv * r.height())

        legend_y = r.top() + 4
        for (y, color, label, dashed) in self.series:
            path = QPainterPath()
            n = len(y)
            for i in range(n):
                q = px(i / (n - 1), float(np.clip(y[i], 0, 1)))
                path.moveTo(q) if i == 0 else path.lineTo(q)
            pen = QPen(color, 1.8)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPath(path)
            if label:
                p.drawText(QPointF(r.right() - 150, legend_y + 10), label)
                legend_y += 13
        for (xs, ys, color) in self.scatter:
            p.setPen(QPen(color, 1.0))
            p.setBrush(QBrush(color))
            for xv, yv in zip(xs, ys):
                p.drawEllipse(px(float(xv), float(np.clip(yv, 0, 1))), 2.0, 2.0)
        f = p.font()
        f.setPointSizeF(8.0)
        p.setFont(f)
        p.setPen(self.palette().mid().color())
        if self.xlabel:
            p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 16),
                       int(Qt.AlignmentFlag.AlignHCenter), self.xlabel)


# ---------------------------------------------------------------------------
# an image panel that scales to fit
# ---------------------------------------------------------------------------

class ImageView(QWidget):
    clicked = Signal(float, float)      # normalised coordinates

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pix: Optional[QPixmap] = None
        self.placeholder = "Nothing to show yet"
        self.checkered = False
        self.setMinimumSize(200, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_image(self, img) -> None:
        if img is None:
            self._pix = None
        elif isinstance(img, QPixmap):
            self._pix = img
        else:
            self._pix = pil_to_pixmap(img)
        self.update()

    def _target(self) -> Optional[QRectF]:
        if self._pix is None or self._pix.isNull():
            return None
        aw, ah = self.width() - 8, self.height() - 8
        pw, ph = self._pix.width(), self._pix.height()
        s = min(aw / pw, ah / ph)
        w, h = pw * s, ph * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def mousePressEvent(self, ev) -> None:
        t = self._target()
        if t is None:
            return
        x = (ev.position().x() - t.left()) / t.width()
        y = (ev.position().y() - t.top()) / t.height()
        if 0 <= x <= 1 and 0 <= y <= 1:
            self.clicked.emit(x, y)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), self.palette().window())
            t = self._target()
            if t is None:
                p.setPen(self.palette().mid().color())
                p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter),
                           self.placeholder)
                return
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.fillRect(t.adjusted(-1, -1, 1, 1), QColor(255, 255, 255))
            p.drawPixmap(t, self._pix, QRectF(self._pix.rect()))
            p.setPen(QPen(QColor(0, 0, 0, 60), 1))
            p.drawRect(t)
        finally:
            p.end()
