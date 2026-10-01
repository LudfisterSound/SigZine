"""The Export tab: everything the project can produce."""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressDialog, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ..core import templates
from ..core.render import Cancelled
from ..core.tone import PRESETS
from .printer_tab import open_externally
from .widgets import section


class ExportTab(QWidget):
    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._build()

    @property
    def project(self):
        return self.app.project

    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        left = QWidget()
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 8, 0)

        lay.addWidget(section("Where"))
        row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home() / "Desktop"))
        b = QPushButton("Choose…")
        b.clicked.connect(self._choose_folder)
        row.addWidget(self.folder, 1)
        row.addWidget(b)
        lay.addLayout(row)
        form = QFormLayout()
        self.basename = QLineEdit("")
        self._basename_touched = False
        self.basename.textEdited.connect(
            lambda _t: setattr(self, "_basename_touched", True))
        form.addRow("Base name", self.basename)
        lay.addLayout(form)

        lay.addWidget(section("Print run"))
        self.want_print = QCheckBox("Imposed sheets, ready to print")
        self.want_print.setChecked(True)
        lay.addWidget(self.want_print)
        form = QFormLayout()
        self.duplex_mode = QComboBox()
        self.duplex_mode.addItem("One file, front and back interleaved", "duplex")
        self.duplex_mode.addItem("Two files: fronts then backs", "separate")
        self.duplex_mode.addItem("Fronts only", "fronts")
        self.duplex_mode.addItem("Backs only", "backs")
        form.addRow("Pages in the file", self.duplex_mode)
        self.print_dpi = QComboBox()
        self.print_dpi.addItems(["300", "600", "1200"])
        self.print_dpi.setCurrentText("600")
        form.addRow("Bitmap resolution", self.print_dpi)
        self.contone_dpi = QComboBox()
        self.contone_dpi.addItems(["150", "200", "300", "400", "600"])
        self.contone_dpi.setCurrentText("300")
        form.addRow("Greyscale resolution", self.contone_dpi)
        self.raster_pdfs = QCheckBox("Also tone-correct imported PDF pages "
                                     "(rasterises them)")
        form.addRow(self.raster_pdfs)
        lay.addLayout(form)

        lay.addWidget(section("Digital edition"))
        self.want_digital = QCheckBox("Screen-readable copy")
        self.want_digital.setChecked(True)
        lay.addWidget(self.want_digital)
        form = QFormLayout()
        self.digital_layout = QComboBox()
        self.digital_layout.addItem("Single pages", "single")
        self.digital_layout.addItem("Spreads", "spreads")
        self.digital_layout.addItem("Both, single pages first", "both")
        form.addRow("Layout", self.digital_layout)
        self.digital_tone = QComboBox()
        self.digital_tone.addItems(list(PRESETS.keys()))
        self.digital_tone.setCurrentText("Screen / digital copy")
        form.addRow("Tone", self.digital_tone)
        self.digital_dpi = QComboBox()
        self.digital_dpi.addItems(["96", "150", "200", "300"])
        self.digital_dpi.setCurrentText("150")
        form.addRow("Resolution", self.digital_dpi)
        self.digital_blanks = QCheckBox("Include the blank pages")
        self.digital_cover = QCheckBox("Cover on its own in spread view")
        self.digital_cover.setChecked(True)
        form.addRow(self.digital_blanks)
        form.addRow(self.digital_cover)
        lay.addLayout(form)

        lay.addWidget(section("Paperwork"))
        self.want_instructions = QCheckBox("Printing and binding instructions")
        self.want_instructions.setChecked(True)
        self.want_template = QCheckBox("Punching template for the binding")
        self.want_template.setChecked(True)
        self.want_dummy = QCheckBox("Folding dummy (page numbers only)")
        for w in (self.want_instructions, self.want_template, self.want_dummy):
            lay.addWidget(w)

        lay.addStretch(1)
        printb = QPushButton("Print now…")
        printb.setMinimumHeight(36)
        printb.setToolTip("Impose the sheets and send them straight to a "
                          "printer")
        printb.clicked.connect(lambda: self.app.print_now())
        lay.addWidget(printb)
        go = QPushButton("Export files")
        go.setMinimumHeight(36)
        go.clicked.connect(self.export)
        lay.addWidget(go)
        openf = QPushButton("Open the output folder")
        openf.clicked.connect(lambda: open_externally(Path(self.folder.text())))
        lay.addWidget(openf)
        outer.addWidget(left, 0)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.addWidget(section("What will be made"))
        self.plan_label = QLabel("")
        self.plan_label.setWordWrap(True)
        rl.addWidget(self.plan_label)
        rl.addWidget(section("Log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        rl.addWidget(self.log, 1)
        outer.addWidget(right, 1)

    def _choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export to",
                                             self.folder.text())
        if d:
            self.folder.setText(d)

    def refresh(self) -> None:
        p = self.project
        if not self._basename_touched:
            self.basename.setText(p.name or "Untitled")
        self.want_digital.setChecked(p.digital.enabled)
        self.raster_pdfs.setChecked(p.output.rasterise_pdf_sources)
        try:
            s = p.summary()
        except Exception as exc:
            self.plan_label.setText(f"Layout error: {exc}")
            return
        base = self.basename.text() or "Untitled"
        bits = [
            f"<b>{s['binding']}</b>",
            f"{s['pages']} pages ({s['blanks']} blank) on {s['sheets']} sheets "
            f"of {s['sheet_size']}, {s['sides']} sides to print",
            f"Finished page {s['page_size']}",
            "",
            "<b>Files</b>",
        ]
        if self.want_print.isChecked():
            if self.duplex_mode.currentData() == "separate":
                bits.append(f"{base} - print - fronts.pdf")
                bits.append(f"{base} - print - backs.pdf")
            else:
                bits.append(f"{base} - print.pdf")
        if self.want_digital.isChecked():
            bits.append(f"{base} - digital.pdf")
        if self.want_instructions.isChecked():
            bits.append(f"{base} - instructions.pdf")
        if self.want_template.isChecked():
            bits.append(f"{base} - punching template.pdf")
        if self.want_dummy.isChecked():
            bits.append(f"{base} - folding dummy.pdf")
        self.plan_label.setText("<br>".join(bits))

    # -- the work ---------------------------------------------------------
    def _line(self, text: str) -> None:
        self.log.appendPlainText(text)
        QApplication.processEvents()

    def export(self) -> None:
        p = self.project
        if not p.pages:
            QMessageBox.information(self, "Nothing to export",
                                    "Import some pages first.")
            return
        folder = Path(self.folder.text()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot write there", str(exc))
            return
        base = (self.basename.text() or p.name or "Untitled").strip()

        p.output.duplex_mode = self.duplex_mode.currentData()
        p.output.print_dpi = int(self.print_dpi.currentText())
        p.output.contone_dpi = int(self.contone_dpi.currentText())
        p.output.rasterise_pdf_sources = self.raster_pdfs.isChecked()
        p.digital.enabled = self.want_digital.isChecked()
        p.digital.layout = self.digital_layout.currentData()
        p.digital.tone_preset = self.digital_tone.currentText()
        p.digital.dpi = int(self.digital_dpi.currentText())
        p.digital.include_blanks = self.digital_blanks.isChecked()
        p.digital.cover_alone = self.digital_cover.isChecked()

        self.log.clear()
        started = time.time()
        made: List[Path] = []
        dlg = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setValue(0)
        QApplication.processEvents()

        r = self.app.renderer
        r.cancel = False

        def progress(i: int, n: int, msg: str) -> None:
            dlg.setLabelText(msg)
            dlg.setMaximum(max(1, n))
            dlg.setValue(i)
            QApplication.processEvents()
            if dlg.wasCanceled():
                r.cancel = True

        try:
            plan = p.build_plan()
            self._line(f"{plan.total_pages} pages, {plan.sheet_count} sheets, "
                       f"{len(plan.sheets)} sides")
            for n in plan.notes:
                self._line("  note: " + n)

            if self.want_print.isChecked():
                dlg.setLabelText("Imposing sheets…")
                out = r.render_print(folder / f"{base} - print.pdf", plan,
                                     p.output.duplex_mode, progress)
                for o in out:
                    made.append(o)
                    self._line(f"wrote {o.name}")
            if self.want_digital.isChecked():
                dlg.setLabelText("Building the digital copy…")
                d = r.render_digital(folder / f"{base} - digital.pdf", progress)
                made.append(d)
                self._line(f"wrote {d.name}")
            if self.want_instructions.isChecked():
                i = templates.instruction_sheet(
                    folder / f"{base} - instructions.pdf", p, plan)
                made.append(i)
                self._line(f"wrote {i.name}")
            if self.want_template.isChecked():
                b = p.binding
                if b.sewing_template:
                    t = templates.sewing_template(
                        folder / f"{base} - punching template.pdf",
                        p.imposition.binding_key, plan.page_size,
                        signatures=len(plan.signatures))
                    made.append(t)
                    self._line(f"wrote {t.name}")
                else:
                    self._line(f"no punching template for {b.name}")
            if self.want_dummy.isChecked():
                d = r.render_dummy(folder / f"{base} - folding dummy.pdf", plan)
                made.append(d)
                self._line(f"wrote {d.name}")
        except Cancelled:
            self._line("cancelled")
        except Exception as exc:
            self._line("ERROR: " + str(exc))
            self._line(traceback.format_exc())
            QMessageBox.critical(self, "Export failed", str(exc))
        finally:
            dlg.close()
            r.cancel = False

        self._line(f"done in {time.time() - started:.1f}s")
        if made:
            open_externally(made[0])
