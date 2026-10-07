"""The main window: menus, the tab bar and the document lifecycle."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QTabWidget, QTextBrowser, QToolBar, QVBoxLayout,
                               QWidget)

from .. import APP_NAME, __version__
from ..core.project import PROJECT_EXT, Project
from ..core.render import Renderer
from ..core.sources import ALL_EXT
from .export_tab import ExportTab
from .new_document import NewDocumentDialog
from .print_dialog import print_project
from .layout_tab import LayoutTab
from .pages_tab import PagesTab
from .printer_tab import PrinterTab
from .tone_tab import ToneTab

QUICKSTART = """
<h2>Signature Zine</h2>
<p>Lay out, tone-correct and print zines and book signatures on a desktop
laser printer.</p>

<h3>The short version</h3>
<ol>
<li><b>Pages</b> - import PDFs, photographs, scans or text. Drag them into
order. Blank pages are added for you so the signatures come out even.</li>
<li><b>Layout</b> - pick a binding. The imposition, the folding instructions
and the duplex flip setting all follow from it. Watch the preview.</li>
<li><b>Tone</b> - choose a preset, then pull the curve around. The right-hand
proof shows what the paper will look like, not the pale data that is sent
to the printer.</li>
<li><b>Printer</b> - print the linearisation target, scan it back in, and the
application works out exactly how much your printer darkens every tone.
Everything you export afterwards is corrected for it.</li>
<li><b>Export</b> - imposed sheets, a digital copy, instructions, a punching
template and a folding dummy.</li>
</ol>

<h3>Worth knowing</h3>
<ul>
<li>Print at 100%. "Fit to page" will quietly ruin the imposition.</li>
<li>Turn off toner save and every "enhancement" in the driver - the
correction is already in the file.</li>
<li>Run the folding dummy first when you try a new binding. It costs one
sheet and saves a whole run.</li>
<li>Paper grain should run parallel to the spine, or the fold will fight
you.</li>
</ul>
"""


class QuickStart(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Quick start")
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        b = QTextBrowser()
        b.setHtml(QUICKSTART)
        lay.addWidget(b)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class MainWindow(QMainWindow):
    def __init__(self, start_dialog: bool = True) -> None:
        super().__init__()
        self._start_dialog = start_dialog
        self.project = Project()
        self.renderer = Renderer(self.project)
        self.simulate_print = True
        self.settings = QSettings("Signature Zine", "Signature Zine")

        self.setWindowTitle(APP_NAME)
        self.resize(1360, 900)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.pages_tab = PagesTab(self)
        self.layout_tab = LayoutTab(self)
        self.tone_tab = ToneTab(self)
        self.printer_tab = PrinterTab(self)
        self.export_tab = ExportTab(self)
        self.tabs.addTab(self.pages_tab, "Pages")
        self.tabs.addTab(self.layout_tab, "Layout")
        self.tabs.addTab(self.tone_tab, "Tone")
        self.tabs.addTab(self.printer_tab, "Printer")
        self.tabs.addTab(self.export_tab, "Export")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)

        self._build_menus()
        self.status = self.statusBar()
        self.project_changed()
        QTimer.singleShot(300, self._maybe_quickstart)

    # -- menus ------------------------------------------------------------
    def _act(self, menu, text, slot, shortcut=None, tip="") -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_menus(self) -> None:
        m = self.menuBar().addMenu("&File")
        self._act(m, "New…", self.new_document, "Ctrl+N")
        self._act(m, "New zine (saddle stitched)",
                  lambda: self.new_project("zine", "saddle-folio"))
        self._act(m, "New book (sewn signatures)",
                  lambda: self.new_project("book", "sewn-folio"))
        m.addSeparator()
        self._act(m, "Open…", self.open_project, QKeySequence.StandardKey.Open)
        self.recent_menu = m.addMenu("Open recent")
        self._rebuild_recent()
        m.addSeparator()
        self._act(m, "Save", self.save_project, QKeySequence.StandardKey.Save)
        self._act(m, "Save as…", self.save_project_as, "Ctrl+Shift+S")
        m.addSeparator()
        self._act(m, "Import files…", self.pages_tab.import_files, "Ctrl+I")
        self._act(m, "Import folder…", self.pages_tab.import_folder)
        m.addSeparator()
        self._act(m, "Print…", self.print_now, "Ctrl+P")
        self._act(m, "Export…", lambda: self.tabs.setCurrentWidget(self.export_tab),
                  "Ctrl+E")
        m.addSeparator()
        self._act(m, "Quit", self.close, QKeySequence.StandardKey.Quit)

        m = self.menuBar().addMenu("&Edit")
        self._act(m, "Add blank page", self.pages_tab.add_blank)
        self._act(m, "Duplicate page", self.pages_tab.duplicate)
        self._act(m, "Remove page", self.pages_tab.remove, "Backspace")
        m.addSeparator()
        self._act(m, "Rotate left", lambda: self.pages_tab.rotate(-90), "Ctrl+[")
        self._act(m, "Rotate right", lambda: self.pages_tab.rotate(90), "Ctrl+]")
        m.addSeparator()
        self._act(m, "Re-flow text pages", self._reflow)

        m = self.menuBar().addMenu("&Layout")
        for key, label in (("zine", "Zine defaults"), ("book", "Book defaults")):
            self._act(m, label, lambda k=key: self.set_mode(k))
        m.addSeparator()
        self._act(m, "Rebuild the imposition", self.layout_changed, "Ctrl+R")
        self._act(m, "Fill out the signatures now", self._pad_now)

        m = self.menuBar().addMenu("&Printer")
        self._act(m, "Test sheets and calibration",
                  lambda: self.tabs.setCurrentWidget(self.printer_tab))
        self._act(m, "Reload saved profiles", self.printer_tab.reload_profiles)

        m = self.menuBar().addMenu("&Help")
        self._act(m, "Quick start", lambda: QuickStart(self).exec())
        self._act(m, "About", self._about)

        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._act(tb, "New…", self.new_document)
        self._act(tb, "Open", self.open_project)
        self._act(tb, "Save", self.save_project)
        tb.addSeparator()
        self._act(tb, "Import…", self.pages_tab.import_files)
        tb.addSeparator()
        self._act(tb, "Print…", self.print_now)
        self._act(tb, "Export…",
                  lambda: self.tabs.setCurrentWidget(self.export_tab))

    # -- document ---------------------------------------------------------
    def new_project(self, mode: str = "zine",
                    preset_key: Optional[str] = None,
                    sheet: Optional[str] = None) -> None:
        if not self._confirm_discard():
            return
        self.project.library.close()
        self.project = Project()
        if preset_key:
            self.project.apply_document_preset(preset_key, sheet)
        else:
            self.project.apply_mode_defaults(mode)
            self.project.name = ("Untitled zine" if mode == "zine"
                                 else "Untitled book")
        self.renderer = Renderer(self.project)
        self.export_tab._basename_touched = False
        self.project_changed()
        self.tabs.setCurrentWidget(self.pages_tab)

    def new_document(self) -> None:
        """Ask what is being made, then set the document up for it."""
        dlg = NewDocumentDialog(self)
        result = dlg.exec()
        if dlg.show_guide:
            QuickStart(self).exec()
            return self.new_document()
        if dlg.open_existing:
            self.open_project()
            return
        if result == QDialog.DialogCode.Accepted and dlg.preset_key:
            self.new_project(preset_key=dlg.preset_key, sheet=dlg.sheet_name)

    def print_now(self) -> None:
        print_project(self)

    def set_mode(self, mode: str) -> None:
        self.project.apply_mode_defaults(mode)
        self.project_changed()

    def open_project(self, path: Optional[str] = None) -> None:
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open", "", f"Signature Zine (*{PROJECT_EXT})")
        if not path:
            return
        try:
            p = Project.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not open", str(exc))
            return
        self.project.library.close()
        self.project = p
        self.renderer = Renderer(p)
        missing = p.missing_files()
        if missing:
            QMessageBox.warning(
                self, "Missing files",
                "These files have moved or been deleted:\n\n" +
                "\n".join(missing[:10]))
        self._add_recent(path)
        self.export_tab.basename.setText(p.name)
        self.project_changed()

    def save_project(self) -> bool:
        if self.project.path is None:
            return self.save_project_as()
        try:
            self.project.save(self.project.path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return False
        self._add_recent(str(self.project.path))
        self._update_title()
        self.status.showMessage(f"Saved {self.project.path.name}", 4000)
        return True

    def save_project_as(self) -> bool:
        start = str(Path.home() / f"{self.project.name}{PROJECT_EXT}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as", start, f"Signature Zine (*{PROJECT_EXT})")
        if not path:
            return False
        self.project.path = Path(path)
        return self.save_project()

    def _confirm_discard(self) -> bool:
        if not self.project.dirty or not self.project.pages:
            return True
        r = QMessageBox.question(
            self, "Unsaved changes", "Save the current document first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return False
        if r == QMessageBox.StandardButton.Save:
            return self.save_project()
        return True

    def closeEvent(self, ev) -> None:
        if self._confirm_discard():
            self.project.library.close()
            ev.accept()
        else:
            ev.ignore()

    # -- recents ----------------------------------------------------------
    def _recent(self) -> List[str]:
        return [p for p in (self.settings.value("recent", []) or [])
                if Path(p).exists()]

    def _add_recent(self, path: str) -> None:
        items = [path] + [p for p in self._recent() if p != path]
        self.settings.setValue("recent", items[:10])
        self._rebuild_recent()

    def _rebuild_recent(self) -> None:
        self.recent_menu.clear()
        items = self._recent()
        if not items:
            a = self.recent_menu.addAction("Nothing yet")
            a.setEnabled(False)
            return
        for p in items:
            self.recent_menu.addAction(
                Path(p).name, lambda checked=False, q=p: self.open_project(q))

    # -- change plumbing ---------------------------------------------------
    def project_changed(self) -> None:
        self.renderer.project = self.project
        self.renderer.invalidate()
        self.pages_tab.refresh()
        self.layout_tab.refresh()
        self.tone_tab.refresh()
        self.export_tab.refresh()
        self._update_title()

    def layout_changed(self) -> None:
        self.layout_tab.rebuild()
        self.pages_tab._update_status()
        self.export_tab.refresh()
        self._update_title()

    def tone_changed(self) -> None:
        self.pages_tab._thumb_queue = list(range(len(self.project.pages)))
        self.pages_tab._timer.start()
        self._update_title()

    def _tab_changed(self, index: int) -> None:
        w = self.tabs.widget(index)
        if hasattr(w, "refresh"):
            w.refresh()

    def _update_title(self) -> None:
        mark = "• " if self.project.dirty else ""
        where = f" — {self.project.path}" if self.project.path else ""
        self.setWindowTitle(f"{mark}{self.project.name} — {APP_NAME}{where}")

    def _reflow(self) -> None:
        self.project.reflow_text()
        self.project_changed()

    def _pad_now(self) -> None:
        n = self.project.apply_padding()
        self.project_changed()
        self.status.showMessage(f"{n} blank page(s) added", 4000)

    def _about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Imposition, tone correction and printer calibration for "
            "people who print their own zines and books.</p>")

    def _maybe_quickstart(self) -> None:
        if not self._start_dialog:
            return
        if not self.settings.value("seen_quickstart", False, type=bool):
            QuickStart(self).exec()
            self.settings.setValue("seen_quickstart", True)
        if not self.project.pages and self.project.path is None:
            self.new_document()

    # -- drag and drop -----------------------------------------------------
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls()]
        projects = [p for p in paths if p.endswith(PROJECT_EXT)]
        if projects:
            self.open_project(projects[0])
            return
        added = 0
        for p in paths:
            path = Path(p)
            try:
                if path.is_dir():
                    added += len(self.project.add_folder(path))
                elif path.suffix.lower() in ALL_EXT:
                    added += len(self.project.add_file(path))
            except Exception:
                continue
        if added:
            self.project_changed()
            self.status.showMessage(f"Imported {added} page(s)", 4000)
