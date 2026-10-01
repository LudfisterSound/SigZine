"""The Pages tab: import material, put it in order, tweak individual pages."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QToolButton, QVBoxLayout,
                               QWidget)

from ..core.sources import ALL_EXT, PageItem
from ..core.tone import PRESETS
from .widgets import SliderSpin, pil_to_pixmap, section

THUMB = QSize(104, 138)


class TextDialog(QDialog):
    def __init__(self, parent=None, text: str = "", title: str = "Text") -> None:
        super().__init__(parent)
        self.setWindowTitle("Add text pages")
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        self.title = QLineEdit(title)
        form = QFormLayout()
        form.addRow("Title", self.title)
        lay.addLayout(form)
        lay.addWidget(QLabel(
            "Plain text. '# ' and '## ' make headings, '> ' a quote, "
            "'---' on its own line forces a page break."))
        self.edit = QPlainTextEdit(text)
        self.edit.setStyleSheet("font-family: Menlo, monospace;")
        lay.addWidget(self.edit, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class PagesTab(QWidget):
    changed = Signal()

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._thumb_queue: List[int] = []
        self._updating = False
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(12)
        self._timer.timeout.connect(self._make_thumbs)

    @property
    def project(self):
        return self.app.project

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        bar = QHBoxLayout()
        for label, slot, tip in (
            ("Import files…", self.import_files, "PDF, images, text"),
            ("Import folder…", self.import_folder, "Every supported file in a folder"),
            ("Add text…", self.add_text, "Type or paste text and have it typeset"),
            ("Add blank", self.add_blank, "Insert a blank page after the selection"),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        for label, slot in (("Rotate ↺", lambda: self.rotate(-90)),
                            ("Rotate ↻", lambda: self.rotate(90)),
                            ("Duplicate", self.duplicate),
                            ("Remove", self.remove)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            bar.addWidget(b)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(THUMB)
        self.list.setGridSize(QSize(THUMB.width() + 26, THUMB.height() + 44))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setSpacing(4)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        split.addWidget(self.list)

        self.inspector = self._build_inspector()
        split.addWidget(self.inspector)
        split.setStretchFactor(0, 1)
        split.setSizes([760, 300])
        outer.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.status)

    def _build_inspector(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)

        self.info = QLabel("No page selected")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        lay.addWidget(section("Placement"))
        form = QFormLayout()
        self.fit = QComboBox()
        self.fit.addItems(["fit", "fill", "actual", "stretch"])
        self.fit.currentTextChanged.connect(lambda v: self._apply("fit", v))
        form.addRow("Fit", self.fit)
        self.rotation = QComboBox()
        self.rotation.addItems(["0", "90", "180", "270"])
        self.rotation.currentTextChanged.connect(
            lambda v: self._apply("rotation", int(v)))
        form.addRow("Rotation", self.rotation)
        self.scale = SliderSpin(0.1, 3.0, 1.0, 0.01, 2, "x")
        self.scale.valueChanged.connect(lambda v: self._apply("scale", v))
        form.addRow("Scale", self.scale)
        self.off_x = SliderSpin(-100, 100, 0, 1, 0, " pt")
        self.off_x.valueChanged.connect(lambda v: self._apply("offset_x", v))
        form.addRow("Shift across", self.off_x)
        self.off_y = SliderSpin(-100, 100, 0, 1, 0, " pt")
        self.off_y.valueChanged.connect(lambda v: self._apply("offset_y", v))
        form.addRow("Shift down", self.off_y)
        lay.addLayout(form)

        lay.addWidget(section("Tone"))
        form2 = QFormLayout()
        self.tone_on = QCheckBox("Apply tone correction")
        self.tone_on.toggled.connect(lambda v: self._apply("tone_enabled", v))
        form2.addRow(self.tone_on)
        self.preset = QComboBox()
        self.preset.addItem("(use document default)")
        self.preset.addItems(list(PRESETS.keys()))
        self.preset.currentIndexChanged.connect(self._preset_changed)
        form2.addRow("Override", self.preset)
        lay.addLayout(form2)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(210)
        self.preview_label.setStyleSheet(
            "background: palette(base); border: 1px solid palette(mid);")
        lay.addWidget(self.preview_label, 1)
        lay.addStretch(0)
        return w

    # -- importing --------------------------------------------------------
    def import_files(self) -> None:
        pats = " ".join("*" + e for e in sorted(ALL_EXT))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import", "", f"Supported ({pats});;All files (*)")
        if not files:
            return
        bad = []
        for f in files:
            try:
                self.project.add_file(f)
            except Exception as exc:
                bad.append(f"{Path(f).name}: {exc}")
        if bad:
            QMessageBox.warning(self, "Some files could not be imported",
                                "\n".join(bad[:10]))
        self.app.project_changed()

    def import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import folder")
        if not folder:
            return
        n = len(self.project.add_folder(folder))
        if n == 0:
            QMessageBox.information(self, "Nothing imported",
                                    "No supported files in that folder.")
        self.app.project_changed()

    def add_text(self) -> None:
        dlg = TextDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = dlg.edit.toPlainText()
        if text.strip():
            self.project.add_text(text, dlg.title.text() or "Text")
            self.app.project_changed()

    def add_blank(self) -> None:
        rows = self._selected_rows()
        at = (max(rows) + 1) if rows else None
        self.project.add_blanks(1, at)
        self.app.project_changed()

    # -- editing ----------------------------------------------------------
    def _selected_rows(self) -> List[int]:
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def remove(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        self.project.remove(rows)
        self.app.project_changed()

    def duplicate(self) -> None:
        rows = self._selected_rows()
        if rows:
            self.project.duplicate(rows)
            self.app.project_changed()

    def rotate(self, deg: int) -> None:
        rows = self._selected_rows()
        if rows:
            self.project.rotate(rows, deg)
            self.app.project_changed()

    def _rows_moved(self, *args) -> None:
        if self._updating:
            return
        order = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            order.append(item.data(Qt.ItemDataRole.UserRole))
        pages = self.project.pages
        if len(order) == len(pages):
            self.project.pages = [pages[i] for i in order]
            self.project.dirty = True
            self.app.project_changed()

    def _apply(self, attr: str, value) -> None:
        if self._updating:
            return
        rows = self._selected_rows()
        if not rows:
            return
        for r in rows:
            setattr(self.project.pages[r], attr, value)
        self.project.dirty = True
        self.project.library.invalidate()
        self.app.renderer.invalidate()
        self._refresh_preview()
        self.app.layout_changed()

    def _preset_changed(self, index: int) -> None:
        if self._updating:
            return
        value = None if index <= 0 else self.preset.currentText()
        self._apply("tone_preset", value)

    # -- refresh ----------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.project.apply_padding()
        except Exception:
            pass
        self._updating = True
        keep = set(self._selected_rows())
        self.list.clear()
        for i, item in enumerate(self.project.pages):
            label = item.label or ("blank" if item.is_blank() else "page")
            li = QListWidgetItem(f"{i + 1}. {label}")
            li.setData(Qt.ItemDataRole.UserRole, i)
            li.setIcon(QIcon(self._placeholder(item)))
            li.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            li.setToolTip(label)
            self.list.addItem(li)
            if i in keep:
                li.setSelected(True)
        self._updating = False
        self._thumb_queue = list(range(len(self.project.pages)))
        self._timer.start()
        self._update_status()
        self._selection_changed()

    def _placeholder(self, item: PageItem) -> QPixmap:
        pm = QPixmap(THUMB)
        pm.fill(QColor(245, 245, 245))
        p = QPainter(pm)
        try:
            inner = pm.rect().adjusted(14, 4, -14, -4)
            p.fillRect(inner, QColor(255, 255, 255))
            p.setPen(QPen(QColor(190, 190, 190)))
            p.drawRect(inner.adjusted(0, 0, -1, -1))
            if item.is_blank():
                p.setPen(QColor(155, 155, 155))
                p.drawText(inner, int(Qt.AlignmentFlag.AlignCenter), "blank")
        finally:
            p.end()
        return pm

    def _framed(self, img: Image.Image) -> QPixmap:
        """Letterbox a page preview onto a fixed tile so the grid stays tidy."""
        tile = Image.new("L", (THUMB.width(), THUMB.height()), 245)
        thumb = img.convert("L")
        thumb.thumbnail((THUMB.width() - 8, THUMB.height() - 8), Image.LANCZOS)
        card = Image.new("L", (thumb.width + 2, thumb.height + 2), 170)
        card.paste(thumb, (1, 1))
        tile.paste(card, ((tile.width - card.width) // 2,
                          (tile.height - card.height) // 2))
        return pil_to_pixmap(tile)

    def _make_thumbs(self) -> None:
        budget = 2
        while self._thumb_queue and budget > 0:
            i = self._thumb_queue.pop(0)
            if i >= self.list.count() or i >= len(self.project.pages):
                continue
            item = self.project.pages[i]
            if item.is_blank():
                continue
            try:
                img = self.project.library.preview(
                    item, 180, self.project.tone_for(item), self.project.profile)
            except Exception:
                img = None
            if img is None:
                continue
            img = img.copy()
            if self.app.simulate_print:
                from ..core.tone import simulate_print
                img = simulate_print(img, self.project.tone_for(item),
                                     self.project.profile)
            self.list.item(i).setIcon(QIcon(self._framed(img)))
            budget -= 1
        if not self._thumb_queue:
            self._timer.stop()

    def _update_status(self) -> None:
        try:
            s = self.project.summary()
        except Exception as exc:
            self.status.setText(f"Layout error: {exc}")
            return
        self.status.setText(
            f"{s['user_pages']} pages imported, {s['blanks']} blank added  ·  "
            f"{s['sheets']} sheets of {s['sheet_size']}  ·  "
            f"{len(s['signature_sheets'])} signature(s)  ·  "
            f"finished page {s['page_size']}")

    def _selection_changed(self) -> None:
        rows = self._selected_rows()
        self.inspector.setEnabled(bool(rows))
        if not rows:
            self.info.setText("Select a page to adjust it")
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("")
            return
        self._updating = True
        item = self.project.pages[rows[0]]
        src = self.project.library.get(item.source_id)
        size = src.page_size(item.source_page) if src else None
        bits = [f"<b>Page {rows[0] + 1}</b>" +
                (f" (+{len(rows) - 1} more selected)" if len(rows) > 1 else "")]
        if src:
            bits.append(f"{src.title}")
            bits.append(f"{src.kind}, page {item.source_page + 1} of {src.page_count}")
        if size:
            bits.append(size.describe())
        if item.locked_blank:
            bits.append("<i>added automatically to fill the signature</i>")
        self.info.setText("<br>".join(bits))
        self.fit.setCurrentText(item.fit)
        self.rotation.setCurrentText(str(item.rotation))
        self.scale.set_value(item.scale)
        self.off_x.set_value(item.offset_x)
        self.off_y.set_value(item.offset_y)
        self.tone_on.setChecked(item.tone_enabled)
        self.preset.setCurrentIndex(
            0 if not item.tone_preset
            else max(0, self.preset.findText(item.tone_preset)))
        self._updating = False
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        item = self.project.pages[rows[0]]
        if item.is_blank():
            self.preview_label.setText("blank page")
            return
        img = self.project.library.preview(item, 420,
                                           self.project.tone_for(item),
                                           self.project.profile)
        if img is None:
            self.preview_label.setText("no preview")
            return
        if self.app.simulate_print:
            from ..core.tone import simulate_print
            img = simulate_print(img, self.project.tone_for(item),
                                 self.project.profile)
        pm = pil_to_pixmap(img)
        self.preview_label.setPixmap(pm.scaled(
            self.preview_label.width() - 8, self.preview_label.height() - 8,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
