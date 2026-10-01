"""The first question: is this a zine or a book, and what shape?"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QStyle, QStyledItemDelegate, QTabWidget,
                               QVBoxLayout, QWidget)

from ..core.project import DocumentPreset, presets_for

MODE_BLURB = {
    "zine": ("Short, self-contained, printed in one go. Sheets are folded or "
             "stapled straight into the finished thing, and you get a digital "
             "edition alongside the print run."),
    "book": ("Longer work in signatures: several folded sections gathered and "
             "sewn or glued. Imports of any kind, blank pages filled in "
             "automatically, creep compensation and a punching template."),
}


class PresetDelegate(QStyledItemDelegate):
    """Title in bold, then the description, then the numbers."""

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        try:
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            if selected:
                painter.fillRect(option.rect, option.palette.highlight())
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            title, blurb, numbers = (text.split("\n") + ["", ""])[:3]
            base = (option.palette.highlightedText().color() if selected
                    else option.palette.text().color())
            dim = (option.palette.highlightedText().color() if selected
                   else option.palette.mid().color())
            r = QRectF(option.rect).adjusted(10, 6, -10, -4)
            f = painter.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() + 0.5)
            painter.setFont(f)
            painter.setPen(QPen(base))
            painter.drawText(QRectF(r.left(), r.top(), r.width(), 18),
                             int(Qt.AlignmentFlag.AlignLeft), title)
            f.setBold(False)
            f.setPointSizeF(f.pointSizeF() - 0.5)
            painter.setFont(f)
            painter.setPen(QPen(base))
            painter.drawText(QRectF(r.left(), r.top() + 20, r.width(), 36),
                             int(Qt.TextFlag.TextWordWrap), blurb)
            painter.setPen(QPen(dim))
            painter.drawText(QRectF(r.left(), r.bottom() - 16, r.width(), 16),
                             int(Qt.AlignmentFlag.AlignLeft), numbers)
        finally:
            painter.restore()


class PresetList(QListWidget):
    def __init__(self, mode: str, parent=None) -> None:
        super().__init__(parent)
        self.setSpacing(2)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self.setItemDelegate(PresetDelegate(self))
        for pre in presets_for(mode):
            item = QListWidgetItem(f"{pre.title}\n{pre.blurb}\n{pre.describe()}")
            item.setData(Qt.ItemDataRole.UserRole, pre.key)
            item.setSizeHint(QSize(360, 94))
            self.addItem(item)
        if self.count():
            self.setCurrentRow(0)

    def chosen(self) -> Optional[str]:
        item = self.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class NewDocumentDialog(QDialog):
    """Returns ``preset_key`` when accepted, or sets ``open_existing``."""

    def __init__(self, parent=None, allow_open: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("New document")
        self.resize(660, 520)
        self.preset_key: Optional[str] = None
        self.open_existing = False
        self.show_guide = False

        lay = QVBoxLayout(self)
        title = QLabel("<h2>What are you making?</h2>")
        lay.addWidget(title)
        lay.addWidget(QLabel(
            "This sets the binding, the sheet, the folding and the page size. "
            "All of it can be changed later on the Layout tab."))

        self.tabs = QTabWidget()
        self.lists = {}
        for mode, label in (("zine", "Zine"), ("book", "Book signatures")):
            page = QWidget()
            pl = QVBoxLayout(page)
            blurb = QLabel(MODE_BLURB[mode])
            blurb.setWordWrap(True)
            blurb.setStyleSheet("color: palette(mid);")
            pl.addWidget(blurb)
            lst = PresetList(mode)
            lst.itemDoubleClicked.connect(self.accept)
            pl.addWidget(lst, 1)
            self.lists[mode] = lst
            self.tabs.addTab(page, label)
        lay.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        guide = QPushButton("Quick start guide")
        guide.clicked.connect(self._guide)
        row.addWidget(guide)
        row.addStretch(1)
        if allow_open:
            openb = QPushButton("Open an existing document…")
            openb.clicked.connect(self._open)
            row.addWidget(openb)
        start = QPushButton("Start")
        start.setDefault(True)
        start.clicked.connect(self.accept)
        row.addWidget(start)
        lay.addLayout(row)

    def _current_list(self) -> PresetList:
        mode = "zine" if self.tabs.currentIndex() == 0 else "book"
        return self.lists[mode]

    def _open(self) -> None:
        self.open_existing = True
        self.reject()

    def _guide(self) -> None:
        self.show_guide = True

    def accept(self) -> None:
        self.preset_key = self._current_list().chosen()
        super().accept()
