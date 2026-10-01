"""Print straight from the application, without a trip through Preview."""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QMessageBox, QProgressDialog,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..core import printing
from ..core.imposition import duplex_driver_setting
from ..core.paths import cache_dir
from ..core.render import Cancelled
from ..core.units import PAPER_SIZES

WHOLE = "whole"
FRONTS = "fronts"
BACKS = "backs"
MANUAL = "manual"


def media_name(sheet_size) -> Optional[str]:
    """The CUPS media name for a sheet, if it is a size CUPS knows."""
    for name, size in PAPER_SIZES.items():
        if (abs(size.width - min(sheet_size.width, sheet_size.height)) < 1.2
                and abs(size.height - max(sheet_size.width,
                                          sheet_size.height)) < 1.2):
            return name.replace(" ", "")
    return None


class PrintDialog(QDialog):
    def __init__(self, project, plan, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.plan = plan
        self.setWindowTitle("Print")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        s = project.imposition
        single = s.single_sided
        self.driver_flip = duplex_driver_setting(s, plan.sheet_size)

        head = QLabel(
            f"<b>{project.name}</b><br>"
            f"{plan.total_pages} pages, {plan.sheet_count} sheets of "
            f"{plan.sheet_size.describe()}, {len(plan.sheets)} sides")
        head.setWordWrap(True)
        lay.addWidget(head)

        form = QFormLayout()
        self.printer = QComboBox()
        self.printers: List[printing.Printer] = printing.list_printers()
        for p in self.printers:
            self.printer.addItem(p.label, p.name)
        if not self.printers:
            self.printer.addItem("No printer found", None)
        form.addRow("Printer", self.printer)

        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        form.addRow("Copies", self.copies)

        self.what = QComboBox()
        if single:
            self.what.addItem("All sheets (one side)", WHOLE)
        else:
            self.what.addItem("Whole run, printer does the duplexing", WHOLE)
            self.what.addItem("Manual duplex: fronts, then backs", MANUAL)
            self.what.addItem("Fronts only", FRONTS)
            self.what.addItem("Backs only", BACKS)
        self.what.currentIndexChanged.connect(self._sync)
        form.addRow("Print", self.what)

        self.duplex = QComboBox()
        self.duplex.addItem(f"Two-sided, flip on the {self.driver_flip}",
                            self.driver_flip)
        other = "short edge" if self.driver_flip == "long edge" else "long edge"
        self.duplex.addItem(f"Two-sided, flip on the {other}", other)
        self.duplex.addItem("One-sided", "none")
        if single:
            self.duplex.setCurrentIndex(2)
        form.addRow("Sides", self.duplex)

        m = media_name(plan.sheet_size)
        self.media = QComboBox()
        self.media.addItem(f"{m or 'Printer default'}"
                           f"  ({plan.sheet_size.describe()})", m)
        self.media.addItem("Let the printer decide", None)
        form.addRow("Paper", self.media)
        lay.addLayout(form)

        note = QLabel(
            "Scaling is switched off in the job so the imposition stays true "
            "to size. If your printer has a 'fit to page' default of its own, "
            "turn it off once in the queue settings.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        lay.addWidget(note)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        lay.addWidget(self.warning)

        bb = QDialogButtonBox()
        self.print_btn = bb.addButton("Print", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._sync()

    def _sync(self) -> None:
        mode = self.what.currentData()
        if mode in (FRONTS, BACKS, MANUAL):
            self.duplex.setCurrentIndex(2)
            self.duplex.setEnabled(False)
            self.warning.setText(
                "Manual duplex: the fronts print first. Put the stack back in "
                "the tray the way your printer wants it, then confirm."
                if mode == MANUAL else "")
        else:
            self.duplex.setEnabled(not self.project.imposition.single_sided)
            self.warning.setText("")

    # -- results ----------------------------------------------------------
    @property
    def printer_name(self) -> Optional[str]:
        return self.printer.currentData()

    @property
    def mode(self) -> str:
        return self.what.currentData()

    @property
    def sides(self) -> str:
        return self.duplex.currentData()

    @property
    def media_option(self) -> Optional[str]:
        return self.media.currentData()


def print_project(window) -> None:
    """Render the current document and send it to a printer."""
    project = window.project
    if not project.pages:
        QMessageBox.information(window, "Nothing to print",
                                "Import some pages first.")
        return
    if not printing.available():
        QMessageBox.warning(window, "No printing system",
                            "This machine has no lpr command, so the "
                            "application cannot submit a job. Export the PDF "
                            "and print it from a viewer instead.")
        return
    try:
        plan = project.build_plan()
    except Exception as exc:
        QMessageBox.critical(window, "Cannot build the layout", str(exc))
        return

    dlg = PrintDialog(project, plan, window)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    if dlg.printer_name is None and dlg.printers:
        return

    renderer = window.renderer
    renderer.cancel = False
    progress = QProgressDialog("Imposing sheets…", "Cancel", 0, 100, window)
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    QApplication.processEvents()

    def report(i: int, n: int, msg: str) -> None:
        progress.setMaximum(max(1, n))
        progress.setValue(i)
        progress.setLabelText(msg)
        QApplication.processEvents()
        if progress.wasCanceled():
            renderer.cancel = True

    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = cache_dir() / "print"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"{project.name or 'document'} {stamp}.pdf"

    try:
        mode = dlg.mode
        if mode == MANUAL:
            files = renderer.render_print(base, plan, "separate", report)
        elif mode in (FRONTS, BACKS):
            files = renderer.render_print(base, plan, mode, report)
        else:
            files = renderer.render_print(base, plan, "duplex", report)
    except Cancelled:
        progress.close()
        window.status.showMessage("Printing cancelled", 4000)
        return
    except Exception as exc:
        progress.close()
        QMessageBox.critical(window, "Could not build the print file", str(exc))
        return
    finally:
        renderer.cancel = False
    progress.close()

    if not files:
        QMessageBox.warning(window, "Nothing to send",
                            "That selection produced no sheets.")
        return

    def submit(path: Path, label: str) -> bool:
        ok, msg = printing.print_pdf(
            path, printer=dlg.printer_name, copies=dlg.copies.value(),
            duplex=dlg.sides, media=dlg.media_option,
            title=f"{project.name} {label}".strip())
        if not ok:
            QMessageBox.critical(window, "The printer refused the job", msg)
        else:
            window.status.showMessage(msg, 8000)
        return ok

    if dlg.mode == MANUAL and len(files) >= 2:
        if not submit(files[0], "fronts"):
            return
        go = QMessageBox.question(
            window, "Second pass",
            "The fronts are on their way.\n\n"
            "When they are all out, put the stack back in the tray, then "
            "print the backs.\n\nIf you are not sure which way up, run two "
            "sheets first and check the page numbers.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if go != QMessageBox.StandardButton.Ok:
            window.status.showMessage(
                f"Backs not sent. The file is at {files[1]}", 10000)
            return
        submit(files[1], "backs")
    else:
        for i, f in enumerate(files):
            if not submit(f, "" if len(files) == 1 else f.stem.split(" - ")[-1]):
                return
