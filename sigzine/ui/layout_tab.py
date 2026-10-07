"""The Layout tab: binding, sheet, margins, marks and the imposition preview."""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

from ..core import binding as bindings
from ..core.imposition import (cut_fold_boundaries, duplex_driver_setting,
                               fold_grid)
from ..core.units import MM, PAPER_SIZES, PRINTABLE_SHEETS, Size
from .widgets import ImageView, SliderSpin, hline, pil_to_pixmap, section

TRIM_CHOICES = ["Auto (fill the sheet)"] + list(PAPER_SIZES.keys())


class SheetView(ImageView):
    """Sheet preview with the slot outlines and page numbers drawn on top."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.slots: List = []
        self.sheet_size: Optional[Size] = None
        self.show_overlay = True
        self.placeholder = "Add some pages to see the imposition"

    def set_plan_sheet(self, img, slots, sheet_size) -> None:
        self.slots = list(slots or [])
        self.sheet_size = sheet_size
        self.set_image(img)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        t = self._target()
        if t is None or not self.show_overlay or not self.sheet_size:
            return
        p = QPainter(self)
        try:
            self._paint_overlay(p, t)
        finally:
            p.end()

    def _paint_overlay(self, p, t) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        sx = t.width() / self.sheet_size.width
        sy = t.height() / self.sheet_size.height
        for slot in self.slots:
            r = slot.trim_rect
            rect = QRectF(t.left() + r[0] * sx, t.top() + r[1] * sy,
                          (r[2] - r[0]) * sx, (r[3] - r[1]) * sy)
            p.setPen(QPen(QColor(40, 120, 220, 150), 1, Qt.PenStyle.DashLine))
            p.drawRect(rect)
            if slot.content_rect != slot.trim_rect:
                c = slot.content_rect
                p.setPen(QPen(QColor(220, 120, 40, 90), 1, Qt.PenStyle.DotLine))
                p.drawRect(QRectF(t.left() + c[0] * sx, t.top() + c[1] * sy,
                                  (c[2] - c[0]) * sx, (c[3] - c[1]) * sy))
            label = str(slot.page) if slot.page else "blank"
            f = p.font()
            f.setPointSizeF(max(9.0, min(20.0, rect.height() * 0.12)))
            f.setBold(True)
            p.setFont(f)
            box = QRectF(rect.center().x() - 60, rect.center().y() - 16, 120, 32)
            p.setPen(QPen(QColor(255, 255, 255, 210), 4))
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), label)
            p.setPen(QPen(QColor(40, 120, 220, 230)))
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), label)
            if slot.rotation:
                p.setPen(QPen(QColor(40, 120, 220, 160)))
                f2 = p.font()
                f2.setPointSizeF(8.0)
                f2.setBold(False)
                p.setFont(f2)
                p.drawText(QRectF(rect.left() + 3, rect.top() + 2, 80, 14),
                           int(Qt.AlignmentFlag.AlignLeft),
                           f"rotated {slot.rotation}")


class LayoutTab(QWidget):
    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._updating = False
        self._sheet_index = 0
        self._plan = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(140)
        self._debounce.timeout.connect(lambda: self._render_preview(48))
        # a second, sharper pass once the user stops fiddling
        self._refine = QTimer(self)
        self._refine.setSingleShot(True)
        self._refine.setInterval(450)
        self._refine.timeout.connect(lambda: self._render_preview(110))
        self._build()

    @property
    def project(self):
        return self.app.project

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

        lay.addWidget(section("Binding"))
        form = QFormLayout()
        self.binding = QComboBox()
        for key, b in bindings.BINDINGS.items():
            self.binding.addItem(b.name, key)
        self.binding.currentIndexChanged.connect(self._binding_changed)
        form.addRow("Method", self.binding)
        self.binding_note = QLabel()
        self.binding_note.setWordWrap(True)
        self.binding_note.setStyleSheet("color: palette(mid);")
        form.addRow(self.binding_note)
        lay.addLayout(form)

        lay.addWidget(section("Sheet"))
        form = QFormLayout()
        self.sheet = QComboBox()
        self.sheet.addItems(PRINTABLE_SHEETS)
        self.sheet.currentTextChanged.connect(self._commit)
        form.addRow("Paper", self.sheet)
        self.orientation = QComboBox()
        self.orientation.addItems(["Landscape", "Portrait"])
        self.orientation.currentTextChanged.connect(self._commit)
        form.addRow("Feed as", self.orientation)
        self.sheet_margin = SliderSpin(0, 25, 0, 0.5, 1, " mm")
        self.sheet_margin.valueChanged.connect(self._commit)
        form.addRow("Edge waste", self.sheet_margin)
        self.trim = QComboBox()
        self.trim.addItems(TRIM_CHOICES)
        self.trim.currentTextChanged.connect(self._commit)
        form.addRow("Finished page", self.trim)
        lay.addLayout(form)

        lay.addWidget(section("Folding and signatures"))
        form = QFormLayout()
        self.folds = QSpinBox()
        self.folds.setRange(1, 4)
        self.folds.valueChanged.connect(self._commit)
        form.addRow("Folds per sheet", self.folds)
        self.fold_trim = SliderSpin(0, 10, 1.5, 0.5, 1, " mm")
        self.fold_trim.valueChanged.connect(self._commit)
        form.addRow("Cut-fold waste", self.fold_trim)
        self.folds_note = QLabel()
        self.folds_note.setStyleSheet("color: palette(mid);")
        form.addRow("", self.folds_note)
        self.sheets_per_sig = QSpinBox()
        self.sheets_per_sig.setRange(1, 16)
        self.sheets_per_sig.valueChanged.connect(self._commit)
        form.addRow("Sheets per signature", self.sheets_per_sig)
        self.up = QSpinBox()
        self.up.setRange(1, 4)
        self.up.valueChanged.connect(self._commit)
        form.addRow("Leaves across (flat)", self.up)
        self.panels = QSpinBox()
        self.panels.setRange(2, 12)
        self.panels.valueChanged.connect(self._commit)
        form.addRow("Panels per strip", self.panels)
        self.balance = QCheckBox("Even out the last signature")
        self.balance.toggled.connect(self._commit)
        form.addRow(self.balance)
        self.autopad = QCheckBox("Add blank pages automatically")
        self.autopad.toggled.connect(self._commit)
        form.addRow(self.autopad)
        self.recto = QCheckBox("Start each import on a right-hand page")
        self.recto.toggled.connect(self._commit)
        form.addRow(self.recto)
        lay.addLayout(form)

        lay.addWidget(section("Margins"))
        form = QFormLayout()
        self.m_top = SliderSpin(0, 40, 8, 0.5, 1, " mm")
        self.m_bottom = SliderSpin(0, 40, 10, 0.5, 1, " mm")
        self.m_inner = SliderSpin(0, 60, 12, 0.5, 1, " mm")
        self.m_outer = SliderSpin(0, 40, 8, 0.5, 1, " mm")
        self.gutter = SliderSpin(0, 40, 0, 0.5, 1, " mm")
        for label, w in (("Head", self.m_top), ("Tail", self.m_bottom),
                         ("Spine", self.m_inner), ("Fore edge", self.m_outer),
                         ("Extra gutter", self.gutter)):
            w.valueChanged.connect(self._commit)
            form.addRow(label, w)
        lay.addLayout(form)

        lay.addWidget(section("Press behaviour"))
        form = QFormLayout()
        self.creep_on = QCheckBox("Compensate for creep")
        self.creep_on.toggled.connect(self._commit)
        form.addRow(self.creep_on)
        self.creep = SliderSpin(0, 0.6, 0.1, 0.01, 2, " mm")
        self.creep.valueChanged.connect(self._commit)
        form.addRow("Per sheet", self.creep)
        self.duplex = QComboBox()
        self.duplex.addItems(["Automatic (recommended)", "Flip on long edge",
                              "Flip on short edge"])
        self.duplex.currentTextChanged.connect(self._commit)
        form.addRow("Duplex", self.duplex)
        self.duplex_note = QLabel()
        self.duplex_note.setWordWrap(True)
        self.duplex_note.setStyleSheet("color: palette(mid);")
        form.addRow("", self.duplex_note)
        self.single = QCheckBox("Print one side only")
        self.single.toggled.connect(self._commit)
        form.addRow(self.single)
        lay.addLayout(form)

        lay.addWidget(section("Marks"))
        form = QFormLayout()
        self.mk_crop = QCheckBox("Crop marks")
        self.mk_fold = QCheckBox("Fold marks")
        self.mk_reg = QCheckBox("Registration targets")
        self.mk_coll = QCheckBox("Collation staircase on the spine")
        self.mk_slug = QCheckBox("Sheet caption in the waste")
        for w in (self.mk_crop, self.mk_fold, self.mk_reg, self.mk_coll,
                  self.mk_slug):
            w.toggled.connect(self._commit)
            form.addRow(w)
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
        nav = QHBoxLayout()
        self.prev = QPushButton("◀")
        self.next = QPushButton("▶")
        self.prev.setFixedWidth(40)
        self.next.setFixedWidth(40)
        self.prev.clicked.connect(lambda: self.goto(self._sheet_index - 1))
        self.next.clicked.connect(lambda: self.goto(self._sheet_index + 1))
        self.sheet_label = QLabel("")
        self.sheet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_cb = QCheckBox("Page numbers")
        self.overlay_cb.setChecked(True)
        self.overlay_cb.toggled.connect(self._toggle_overlay)
        self.sim_cb = QCheckBox("Simulate print")
        self.sim_cb.setChecked(True)
        self.sim_cb.setToolTip(
            "Show what the paper will look like rather than the pale, "
            "pre-compensated data that is sent to the printer.")
        self.sim_cb.toggled.connect(self._sim_toggled)
        nav.addWidget(self.prev)
        nav.addWidget(self.sheet_label, 1)
        nav.addWidget(self.next)
        nav.addSpacing(12)
        nav.addWidget(self.overlay_cb)
        nav.addWidget(self.sim_cb)
        rl.addLayout(nav)
        self.view = SheetView()
        rl.addWidget(self.view, 1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: palette(mid);")
        rl.addWidget(self.summary)
        split.addWidget(right)
        split.setSizes([450, 760])
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
        p = self.project
        s = p.imposition
        i = self.binding.findData(s.binding_key)
        if i >= 0:
            self.binding.setCurrentIndex(i)
        b = p.binding
        self.binding_note.setText(b.blurb)

        name = next((k for k, v in PAPER_SIZES.items()
                     if abs(v.width - s.sheet_size.width) < 0.6
                     and abs(v.height - s.sheet_size.height) < 0.6), None)
        if name and self.sheet.findText(name) >= 0:
            self.sheet.setCurrentText(name)
        self.orientation.setCurrentText("Landscape" if s.sheet_landscape
                                        else "Portrait")
        self.sheet_margin.set_value(s.sheet_margin / MM)
        if s.trim_size is None:
            self.trim.setCurrentIndex(0)
        else:
            tn = next((k for k, v in PAPER_SIZES.items()
                       if abs(v.width - s.trim_size.width) < 0.6
                       and abs(v.height - s.trim_size.height) < 0.6), None)
            self.trim.setCurrentText(tn or TRIM_CHOICES[0])
        self.folds.setValue(s.folds_per_sheet)
        self.fold_trim.set_value(s.fold_trim / MM)
        self.sheets_per_sig.setValue(s.sheets_per_signature)
        self.up.setValue(s.up)
        self.panels.setValue(s.panels_per_sheet)
        self.balance.setChecked(s.balance_last_signature)
        self.autopad.setChecked(p.auto_pad)
        self.recto.setChecked(p.start_sources_on_recto)
        self.m_top.set_value(s.margin_top / MM)
        self.m_bottom.set_value(s.margin_bottom / MM)
        self.m_inner.set_value(s.margin_inner / MM)
        self.m_outer.set_value(s.margin_outer / MM)
        self.gutter.set_value(s.gutter_extra / MM)
        self.creep_on.setChecked(s.creep_enabled)
        self.creep.set_value(s.creep_per_sheet / MM)
        self.duplex.setCurrentIndex({"auto": 0, "long": 1, "short": 2}
                                    .get(s.back_flip, 0))
        self.single.setChecked(s.single_sided)
        self.mk_crop.setChecked(s.crop_marks)
        self.mk_fold.setChecked(s.fold_marks)
        self.mk_reg.setChecked(s.registration_marks)
        self.mk_coll.setChecked(s.collation_marks)
        self.mk_slug.setChecked(s.sheet_slugs)
        self._updating = False
        self._enable_for_binding()
        self.rebuild()

    def _binding_changed(self) -> None:
        if self._updating:
            return
        key = self.binding.currentData()
        self.project.set_binding(key)
        self.refresh()
        self.app.project_changed()

    def _enable_for_binding(self) -> None:
        b = self.project.binding
        folded = b.family == bindings.FOLDED
        self.folds.setEnabled(folded)
        self.sheets_per_sig.setEnabled(folded and b.signatures)
        self.balance.setEnabled(folded and b.signatures)
        self.up.setEnabled(b.family == bindings.FLAT)
        self.panels.setEnabled(b.family == bindings.ACCORDION)
        self.creep_on.setEnabled(b.creep and folded)
        self.creep.setEnabled(b.creep and folded and self.creep_on.isChecked())
        self.single.setEnabled(b.duplex)
        self.duplex.setEnabled(b.duplex and not self.single.isChecked())
        cols, rows = fold_grid(self.folds.value())
        cut_cols, cut_rows = cut_fold_boundaries(self.folds.value())
        self.fold_trim.setEnabled(folded and bool(cut_cols or cut_rows))
        self.folds_note.setText(
            f"{cols} x {rows} pages a side, {cols * rows * 2} pages per sheet"
            if folded else "not used by this binding")

    def _commit(self, *args) -> None:
        if self._updating:
            return
        p = self.project
        s = p.imposition
        s.sheet_size = PAPER_SIZES[self.sheet.currentText()]
        s.sheet_landscape = self.orientation.currentText() == "Landscape"
        s.sheet_margin = self.sheet_margin.value() * MM
        t = self.trim.currentText()
        s.trim_size = None if t == TRIM_CHOICES[0] else PAPER_SIZES[t]
        s.folds_per_sheet = self.folds.value()
        s.fold_trim = self.fold_trim.value() * MM
        s.sheets_per_signature = self.sheets_per_sig.value()
        s.up = self.up.value()
        s.panels_per_sheet = self.panels.value()
        s.balance_last_signature = self.balance.isChecked()
        p.auto_pad = self.autopad.isChecked()
        p.start_sources_on_recto = self.recto.isChecked()
        s.margin_top = self.m_top.value() * MM
        s.margin_bottom = self.m_bottom.value() * MM
        s.margin_inner = self.m_inner.value() * MM
        s.margin_outer = self.m_outer.value() * MM
        s.gutter_extra = self.gutter.value() * MM
        s.creep_enabled = self.creep_on.isChecked()
        s.creep_per_sheet = self.creep.value() * MM
        s.back_flip = ["auto", "long", "short"][self.duplex.currentIndex()]
        s.single_sided = self.single.isChecked()
        s.crop_marks = self.mk_crop.isChecked()
        s.fold_marks = self.mk_fold.isChecked()
        s.registration_marks = self.mk_reg.isChecked()
        s.collation_marks = self.mk_coll.isChecked()
        s.sheet_slugs = self.mk_slug.isChecked()
        p.dirty = True
        self._enable_for_binding()
        p.reflow_text()
        self.app.layout_changed()

    def _toggle_overlay(self, on: bool) -> None:
        self.view.show_overlay = on
        self.view.update()

    def _sim_toggled(self, on: bool) -> None:
        self.app.simulate_print = on
        self._debounce.start()

    def goto(self, index: int) -> None:
        if self._plan and self._plan.sheets:
            self._sheet_index = max(0, min(index, len(self._plan.sheets) - 1))
        self._debounce.start()
        self._update_labels()

    # -- preview ----------------------------------------------------------
    def rebuild(self) -> None:
        try:
            self._plan = self.project.build_plan()
        except Exception as exc:
            self.summary.setText(f"Cannot build the layout: {exc}")
            self._plan = None
            self.view.set_plan_sheet(None, [], None)
            return
        self._sheet_index = min(self._sheet_index,
                                max(0, len(self._plan.sheets) - 1))
        self._update_labels()
        self._debounce.start()

    def _update_labels(self) -> None:
        plan = self._plan
        if not plan or not plan.sheets:
            self.sheet_label.setText("")
            return
        sp = plan.sheets[self._sheet_index]
        self.sheet_label.setText(
            f"Sheet side {self._sheet_index + 1} of {len(plan.sheets)}   ·   "
            f"{sp.label}")
        s = self.project.imposition
        drv = duplex_driver_setting(s, plan.sheet_size)
        self.duplex_note.setText(
            f"In the print dialog choose <b>flip on the {drv}</b>."
            if not s.single_sided else "Single sided.")
        note = "  ".join(plan.notes)
        self.summary.setText(
            f"{plan.total_pages} pages · {plan.sheet_count} sheets · "
            f"{len(plan.sheets)} sides · signatures "
            f"{', '.join(str(x) for x in plan.signatures)} sheet(s) · "
            f"page {plan.page_size.describe()}    {note}")

    def _render_preview(self, dpi: int = 96) -> None:
        plan = self._plan
        if not plan or not plan.sheets:
            self.view.set_plan_sheet(None, [], None)
            return
        try:
            img = self.app.renderer.preview_sheet(
                plan, self._sheet_index, dpi=dpi,
                simulate=self.app.simulate_print)
        except Exception as exc:
            self.summary.setText(f"Preview failed: {exc}")
            return
        sp = plan.sheets[self._sheet_index]
        self.view.set_plan_sheet(img, sp.slots, plan.sheet_size)
        if dpi < 100:
            self._refine.start()
