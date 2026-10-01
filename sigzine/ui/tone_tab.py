"""The Tone tab: the curve, the press controls and a before/after proof."""
from __future__ import annotations

from typing import List, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSplitter, QVBoxLayout, QWidget)

from ..core.tone import (GRAY_MODES, PRESETS, SCREENS, ToneSettings,
                         apply_preset, apply_tone, combined_lut, ramp_preview,
                         simulate_print, to_gray, tone_lut)
from .widgets import CurveEditor, ImageView, SliderSpin, hline, pil_to_pixmap, section


class ToneTab(QWidget):
    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._updating = False
        self._source_index = 0
        self._cached_src: Optional[Image.Image] = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(140)
        self._debounce.timeout.connect(self._render)
        self._build()

    @property
    def project(self):
        return self.app.project

    @property
    def tone(self) -> ToneSettings:
        return self.project.tone

    # -- ui ---------------------------------------------------------------
    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Orientation.Horizontal)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(430)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        panel = QWidget()
        scroll.setWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(2, 2, 10, 2)

        row = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS.keys()))
        row.addWidget(QLabel("Preset"))
        row.addWidget(self.preset, 1)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_preset)
        row.addWidget(apply_btn)
        lay.addLayout(row)

        self.curve = CurveEditor()
        self.curve.setMinimumHeight(260)
        self.curve.curveChanged.connect(self._curve_changed)
        lay.addWidget(self.curve)
        cr = QHBoxLayout()
        reset = QPushButton("Straight line")
        reset.clicked.connect(self.curve.reset)
        cr.addWidget(reset)
        cr.addWidget(QLabel("click to add a point, right-click to remove"))
        cr.addStretch(1)
        lay.addLayout(cr)

        self.ramp = QLabel()
        self.ramp.setFixedHeight(34)
        self.ramp.setToolTip("The 0-100% ramp after correction and screening")
        lay.addWidget(self.ramp)

        lay.addWidget(section("Photograph"))
        form = QFormLayout()
        self.gray = QComboBox()
        self.gray.addItems(list(GRAY_MODES.keys()))
        self.gray.currentTextChanged.connect(self._commit)
        form.addRow("Convert to grey by", self.gray)
        self.auto_levels = QCheckBox("Auto black and white point")
        self.auto_levels.toggled.connect(self._commit)
        form.addRow(self.auto_levels)
        self.black = SliderSpin(0, 0.5, 0.0, 0.005, 3)
        self.white = SliderSpin(0.5, 1.0, 1.0, 0.005, 3)
        self.gamma = SliderSpin(0.3, 2.5, 1.0, 0.01, 2)
        self.contrast = SliderSpin(-1, 1, 0.0, 0.01, 2)
        self.clarity = SliderSpin(0, 1, 0.0, 0.01, 2)
        self.sharpen = SliderSpin(0, 3, 0.0, 0.05, 2)
        self.sharp_r = SliderSpin(0.3, 4, 1.2, 0.1, 1, " px")
        for label, w in (("Black point", self.black), ("White point", self.white),
                         ("Gamma", self.gamma), ("Contrast", self.contrast),
                         ("Local contrast", self.clarity),
                         ("Sharpen", self.sharpen), ("Sharpen radius", self.sharp_r)):
            w.valueChanged.connect(self._commit)
            form.addRow(label, w)
        lay.addLayout(form)

        lay.addWidget(section("Press"))
        form = QFormLayout()
        self.use_profile = QCheckBox("Use the measured printer profile")
        self.use_profile.toggled.connect(self._commit)
        form.addRow(self.use_profile)
        self.profile_note = QLabel("No profile loaded")
        self.profile_note.setStyleSheet("color: palette(mid);")
        self.profile_note.setWordWrap(True)
        form.addRow("", self.profile_note)
        self.dot_gain = SliderSpin(0, 0.5, 0.18, 0.01, 2)
        self.dot_gain.setToolTip("Extra coverage the printer adds at 50%, used "
                                 "when there is no measured profile")
        self.min_dot = SliderSpin(0, 0.15, 0.02, 0.005, 3)
        self.max_ink = SliderSpin(0.5, 1.0, 1.0, 0.01, 2)
        for label, w in (("Dot gain at 50%", self.dot_gain),
                         ("Minimum dot", self.min_dot),
                         ("Maximum ink", self.max_ink)):
            w.valueChanged.connect(self._commit)
            form.addRow(label, w)
        self.paper_white = QCheckBox("Keep paper white free of toner")
        self.paper_white.toggled.connect(self._commit)
        form.addRow(self.paper_white)
        lay.addLayout(form)

        lay.addWidget(section("Screening"))
        form = QFormLayout()
        self.screen = QComboBox()
        self.screen.addItems(SCREENS)
        self.screen.currentTextChanged.connect(self._commit)
        form.addRow("Halftone", self.screen)
        self.lpi = SliderSpin(20, 150, 85, 1, 0, " lpi")
        self.angle = SliderSpin(0, 90, 45, 1, 0, "°")
        self.device_dpi = QComboBox()
        self.device_dpi.addItems(["300", "600", "1200"])
        self.render_dpi = QComboBox()
        self.render_dpi.addItems(["150", "200", "300", "400", "600"])
        self.lpi.valueChanged.connect(self._commit)
        self.angle.valueChanged.connect(self._commit)
        self.device_dpi.currentTextChanged.connect(self._commit)
        self.render_dpi.currentTextChanged.connect(self._commit)
        form.addRow("Screen ruling", self.lpi)
        form.addRow("Screen angle", self.angle)
        form.addRow("Printer resolution", self.device_dpi)
        form.addRow("Contone resolution", self.render_dpi)
        lay.addLayout(form)
        lay.addStretch(1)
        self._scroll = scroll
        self._panel = panel
        self._split = split
        self._sized = False
        split.addWidget(scroll)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Proof"))
        self.source = QComboBox()
        self.source.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source, 1)
        self.sim = QCheckBox("Simulate print")
        self.sim.setChecked(True)
        self.sim.toggled.connect(lambda v: (setattr(self.app, "simulate_print", v),
                                            self._debounce.start()))
        top.addWidget(self.sim)
        rl.addLayout(top)

        views = QSplitter(Qt.Orientation.Horizontal)
        left_box = QWidget()
        lb = QVBoxLayout(left_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.addWidget(QLabel("As imported"))
        self.before = ImageView()
        lb.addWidget(self.before, 1)
        right_box = QWidget()
        rb = QVBoxLayout(right_box)
        rb.setContentsMargins(0, 0, 0, 0)
        self.after_label = QLabel("On paper")
        rb.addWidget(self.after_label)
        self.after = ImageView()
        rb.addWidget(self.after, 1)
        views.addWidget(left_box)
        views.addWidget(right_box)
        views.setSizes([400, 400])
        rl.addWidget(views, 1)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: palette(mid);")
        rl.addWidget(self.note)
        split.addWidget(right)
        split.setSizes([460, 700])
        outer.addWidget(split)

    def showEvent(self, ev) -> None:
        """Give the control column exactly the width its contents need.

        Size hints are only meaningful once the widget has been laid out, so
        this cannot be done while building it.
        """
        super().showEvent(ev)
        if self._sized:
            return
        self._sized = True
        want = self._panel.minimumSizeHint().width() + 30
        want = max(430, min(want, int(self.width() * 0.55) or want))
        self._scroll.setMinimumWidth(want)
        self._split.setSizes([want, max(420, self.width() - want)])

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        self._updating = True
        t = self.tone
        self.curve.set_points(t.curve or [(0, 0), (1, 1)])
        self.gray.setCurrentText(t.gray_mode)
        self.auto_levels.setChecked(t.auto_levels)
        self.black.set_value(t.black_point)
        self.white.set_value(t.white_point)
        self.gamma.set_value(t.gamma)
        self.contrast.set_value(t.contrast)
        self.clarity.set_value(t.clarity)
        self.sharpen.set_value(t.sharpen)
        self.sharp_r.set_value(t.sharpen_radius)
        self.use_profile.setChecked(t.use_profile)
        self.dot_gain.set_value(t.dot_gain)
        self.min_dot.set_value(t.min_dot)
        self.max_ink.set_value(t.max_ink)
        self.paper_white.setChecked(t.paper_white)
        self.screen.setCurrentText(t.screen)
        self.lpi.set_value(t.screen_lpi)
        self.angle.set_value(t.screen_angle)
        self.device_dpi.setCurrentText(str(t.device_dpi))
        self.render_dpi.setCurrentText(str(t.render_dpi))
        prof = self.project.profile
        if prof is not None:
            a = prof.analyse()
            if a.get("status") == "ok":
                self.profile_note.setText(
                    f"{prof.name}: gain {a['gain_50'] * 100:+.0f}% at midtone, "
                    f"smallest dot {a['min_printable'] * 100:.0f}%, shadows "
                    f"merge above {a['shadow_merge'] * 100:.0f}%")
            else:
                self.profile_note.setText(f"{prof.name}: not measured yet")
        else:
            self.profile_note.setText(
                "No profile. Measure one in the Printer tab, or set the dot "
                "gain by hand below.")
        self._fill_sources()
        self._updating = False
        self._debounce.start()

    def _fill_sources(self) -> None:
        cur = self.source.currentText()
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("Built-in test photograph", -1)
        for i, item in enumerate(self.project.pages):
            if item.is_blank():
                continue
            src = self.project.library.get(item.source_id)
            if src is None:
                continue
            self.source.addItem(f"{i + 1}. {item.label or src.title}", i)
        idx = self.source.findText(cur)
        self.source.setCurrentIndex(max(0, idx))
        self.source.blockSignals(False)

    def _source_changed(self) -> None:
        self._cached_src = None
        self._debounce.start()

    def _apply_preset(self) -> None:
        self.project.tone = apply_preset(self.project.tone,
                                         self.preset.currentText())
        self.project.library.invalidate()
        self.app.renderer.invalidate()
        self.refresh()
        self.app.layout_changed()

    def _curve_changed(self, points) -> None:
        if self._updating:
            return
        self.tone.curve = [tuple(p) for p in points]
        self._after_change()

    def _commit(self, *args) -> None:
        if self._updating:
            return
        t = self.tone
        t.gray_mode = self.gray.currentText()
        t.auto_levels = self.auto_levels.isChecked()
        t.black_point = self.black.value()
        t.white_point = max(self.white.value(), t.black_point + 0.01)
        t.gamma = self.gamma.value()
        t.contrast = self.contrast.value()
        t.clarity = self.clarity.value()
        t.sharpen = self.sharpen.value()
        t.sharpen_radius = self.sharp_r.value()
        t.use_profile = self.use_profile.isChecked()
        t.dot_gain = self.dot_gain.value()
        t.min_dot = self.min_dot.value()
        t.max_ink = self.max_ink.value()
        t.paper_white = self.paper_white.isChecked()
        t.screen = self.screen.currentText()
        t.screen_lpi = self.lpi.value()
        t.screen_angle = self.angle.value()
        t.device_dpi = int(self.device_dpi.currentText())
        t.render_dpi = int(self.render_dpi.currentText())
        self._after_change()

    def _after_change(self) -> None:
        self.project.dirty = True
        self.project.library.invalidate()
        self.app.renderer.invalidate()
        self._debounce.start()
        self.app.tone_changed()

    # -- rendering --------------------------------------------------------
    def _source_image(self) -> Optional[Image.Image]:
        if self._cached_src is not None:
            return self._cached_src
        idx = self.source.currentData()
        if idx is None or idx < 0:
            from ..core.testsheet import synthetic_photo
            img = synthetic_photo(760, 560)
        else:
            item = self.project.pages[idx]
            src = self.project.library.get(item.source_id)
            if src is None:
                return None
            img = src.render(item.source_page, dpi=110, tone=None,
                             for_screen=False, max_px=1100)
        self._cached_src = img
        return img

    def _render(self) -> None:
        t = self.tone
        prof = self.project.profile if t.use_profile else None
        self.ramp.setPixmap(pil_to_pixmap(
            ramp_preview(t, prof, width=max(200, self.ramp.width()), height=30)))
        self.curve.set_effective(combined_lut(t, prof))

        img = self._source_image()
        if img is None:
            self.before.set_image(None)
            self.after.set_image(None)
            return
        gray = to_gray(img, t.gray_mode)
        self.curve.set_histogram(np.asarray(gray.histogram()[:256], dtype=float))
        self.before.set_image(img)
        try:
            out = apply_tone(img, t, prof)
        except Exception as exc:
            self.note.setText(f"Tone error: {exc}")
            return
        if self.app.simulate_print:
            shown = simulate_print(out, t, prof)
            self.after_label.setText("On paper (simulated)")
        else:
            shown = out
            self.after_label.setText("Data sent to the printer")
        self.after.set_image(shown)
        lut = combined_lut(t, prof)
        self.note.setText(
            f"25% grey leaves as {lut[int(0.75 * 255)] * 100:.0f}% · "
            f"50% as {lut[int(0.5 * 255)] * 100:.0f}% · "
            f"75% as {lut[int(0.25 * 255)] * 100:.0f}% · "
            f"screen: {t.screen} · output {out.size[0]}x{out.size[1]} px")
