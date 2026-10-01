"""The Printer tab: test sheets, measurement, linearisation and profiles."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..core import printing
from ..core import testsheet as TS
from ..core.calibration import (MEASURE_KINDS, PrinterProfile, blend_profiles,
                                from_visual_reading, list_profiles)
from ..core.imposition import duplex_driver_setting
from ..core.linearise import FitReport, build_profile, refine, verify
from ..core.paths import printers_dir, test_sheets_dir
from ..core.scan import ScanResult, load_scan, measure_sheet, overlay
from ..core.units import PAPER_SIZES, PRINTABLE_SHEETS, paper
from .print_dialog import media_name
from .widgets import ImageView, MiniPlot, SliderSpin, section


# title, key, tooltip. The key is what _generate dispatches on.
TEST_SHEETS = (
    ("Linearisation target", "linearisation",
     "The patch sheet you measure to build a profile"),
    ("Linearisation check", "verification",
     "The same greys printed through the correction, to see what error is "
     "left. Needs a profile first."),
    ("Screening comparison", "screening",
     "Same photo through every halftone, to pick one"),
    ("Detail and resolution", "detail",
     "Hairlines, small type and a resolution wedge"),
    ("Duplex registration", "duplex",
     "Check front-to-back alignment and printer scaling"),
    ("Photo proof", "proof",
     "One photograph through six different treatments"),
)

# Default filenames, which double as the job name in the print queue.
SHEET_FILES = {
    "linearisation": "linearisation.pdf",
    "verification": "linearisation-check.pdf",
    "screening": "screening.pdf",
    "detail": "detail.pdf",
    "duplex": "duplex-registration.pdf",
    "proof": "photo-proof.pdf",
}


def _same_readings(nominal, measured, was_nominal, was_measured) -> bool:
    """Whether two sets of readings are the same to the table's precision."""
    if len(nominal) != len(was_nominal) or len(measured) != len(was_measured):
        return False
    if not nominal:
        return True
    try:
        return bool(np.allclose(nominal, was_nominal, rtol=1e-4, atol=1e-7)
                    and np.allclose(measured, was_measured, rtol=1e-4,
                                    atol=1e-7))
    except ValueError:
        return False


def open_externally(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            import os
            os.startfile(str(path))       # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


class VisualDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Quick visual calibration")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Read three numbers off the printed linearisation sheet.\n"
            "No instrument needed - this gets you most of the way there."))
        form = QFormLayout()
        self.light = QDoubleSpinBox()
        self.light.setRange(0, 30)
        self.light.setValue(4)
        self.light.setSuffix(" %")
        self.dark = QDoubleSpinBox()
        self.dark.setRange(50, 100)
        self.dark.setValue(92)
        self.dark.setSuffix(" %")
        self.mid = QDoubleSpinBox()
        self.mid.setRange(10, 90)
        self.mid.setValue(38)
        self.mid.setSuffix(" %")
        form.addRow("Lightest patch you can see at all", self.light)
        form.addRow("Darkest patch still separate from solid", self.dark)
        form.addRow("Patch that looks like a 50% grey", self.mid)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ScanCheckDialog(QDialog):
    def __init__(self, img: Image.Image, result: ScanResult, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Check the sampling")
        self.resize(760, 860)
        lay = QVBoxLayout(self)
        msg = ("Blue circles are the corner marks, red boxes are where each "
               "patch was read. If they line up, accept.")
        if result.warnings:
            msg += "\n\n" + "\n".join("! " + w for w in result.warnings)
        lay.addWidget(QLabel(msg))
        view = ImageView()
        small = overlay(img, result)
        small.thumbnail((900, 1100), Image.LANCZOS)
        view.set_image(small)
        lay.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class VerifyDialog(QDialog):
    """What the linearisation check sheet says, and the offer to refine."""

    def __init__(self, report, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Linearisation check")
        self.resize(460, 520)
        self.refine_requested = False
        lay = QVBoxLayout(self)
        head = QLabel("<br>".join(report.summary()))
        head.setWordWrap(True)
        lay.addWidget(head)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Asked for", "Got", "Error"])
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        rows = report.table()
        table.setRowCount(len(rows))
        for r, (want, got, err) in enumerate(rows):
            for col, text in enumerate((f"{want * 100:.0f}%",
                                        f"{got * 100:.1f}%",
                                        f"{err * 100:+.1f}%")):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 2 and abs(err) > report.tolerance:
                    item.setForeground(QColor(190, 40, 40))
                table.setItem(r, col, item)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(table, 1)

        bb = QDialogButtonBox()
        btn = bb.addButton("Refine the profile from this",
                           QDialogButtonBox.ButtonRole.AcceptRole)
        btn.setToolTip("Fold these readings back into the profile. This is "
                       "measured through the correction, so it pins the "
                       "printer down better than the first pass did.")
        btn.clicked.connect(self._refine)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _refine(self) -> None:
        self.refine_requested = True
        self.accept()


class RegistrationDialog(QDialog):
    """Turn six readings off the registration sheet into real numbers."""

    def __init__(self, sheet, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Front to back registration")
        self.sheet = sheet
        self.result_offset = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Hold the printed sheet up to a window, front toward you, and "
            "read each pointer against the scale it sits on. Right and down "
            "are positive. Leave a station blank if you only measured the "
            "centre."))
        grid = QFormLayout()
        self.fields = {}
        for tag, where in (("A", "top left"), ("B", "centre"),
                           ("C", "top right")):
            row = QHBoxLayout()
            ax = QDoubleSpinBox()
            ay = QDoubleSpinBox()
            for w in (ax, ay):
                w.setRange(-25, 25)
                w.setDecimals(2)
                w.setSingleStep(0.1)
                w.setSuffix(" mm")
            row.addWidget(QLabel("across"))
            row.addWidget(ax)
            row.addWidget(QLabel("down"))
            row.addWidget(ay)
            holder = QWidget()
            holder.setLayout(row)
            grid.addRow(f"{tag}  ({where})", holder)
            self.fields[tag] = (ax, ay)
            ax.valueChanged.connect(self._solve)
            ay.valueChanged.connect(self._solve)
        lay.addLayout(grid)
        self.report = QLabel("")
        self.report.setWordWrap(True)
        self.report.setMinimumHeight(110)
        self.report.setStyleSheet("background: palette(base); padding: 8px;")
        lay.addWidget(self.report)
        bb = QDialogButtonBox()
        self.use = bb.addButton("Use this offset",
                                QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._solve()

    def _readings(self):
        from ..core.units import MM as _MM
        return {t: (ax.value() * _MM, ay.value() * _MM)
                for t, (ax, ay) in self.fields.items()}

    def _solve(self) -> None:
        from ..core.calibration import describe_registration, solve_registration
        from ..core.testsheet import station_geometry
        from ..core.units import MM as _MM
        r = self._readings()
        try:
            solved = solve_registration(r, station_geometry(self.sheet),
                                        self.sheet)
        except Exception as exc:
            self.report.setText(str(exc))
            return
        self.result_offset = (-solved["dx"] / _MM, solved["dy"] / _MM)
        lines = describe_registration(solved, _MM)
        worst = solved["skew_at_corner"] / _MM
        if worst < 0.3 and abs(solved["dx"]) < 0.15 * _MM \
                and abs(solved["dy"]) < 0.15 * _MM:
            lines.append("<b>Nothing here is worth chasing.</b>")
        elif worst >= 0.3:
            lines.append("<b>The skew cannot be corrected by an offset.</b> "
                         "It is usually the paper guides in the tray. Print "
                         "a second sheet: if the skew changes, it is not "
                         "repeatable and there is nothing to fix.")
        self.report.setText("<br>".join(lines))

    @property
    def offset(self):
        """The reading to store, which is the centre station as measured."""
        ax, ay = self.fields["B"]
        return (ax.value(), ay.value())


class PrinterTab(QWidget):
    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.profile = PrinterProfile()
        self._last_map: Optional[Path] = None
        self._last_check_map: Optional[Path] = None
        self._fit: Optional[FitReport] = None
        self._updating = False
        self._build()
        self.reload_profiles()

    @property
    def project(self):
        return self.app.project

    # -- ui ---------------------------------------------------------------
    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Orientation.Horizontal)

        # --- profiles ---
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)
        ll.addWidget(section("Profiles"))
        self.plist = QListWidget()
        self.plist.currentRowChanged.connect(self._select_profile)
        ll.addWidget(self.plist, 1)
        row = QHBoxLayout()
        for label, slot in (("New", self.new_profile),
                            ("Save", self.save_profile),
                            ("Delete", self.delete_profile)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        ll.addLayout(row)
        use = QPushButton("Use in this document")
        use.clicked.connect(self.use_profile)
        ll.addWidget(use)
        form = QFormLayout()
        self.name = QLineEdit()
        self.model = QLineEdit()
        self.paper = QLineEdit()
        self.dpi = QComboBox()
        self.dpi.addItems(["300", "600", "1200"])
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(60)
        for label, w in (("Name", self.name), ("Printer", self.model),
                         ("Paper", self.paper), ("Resolution", self.dpi)):
            form.addRow(label, w)
        form.addRow("Notes", self.notes)
        ll.addLayout(form)
        split.addWidget(left)

        # --- measuring ---
        mid = QScrollArea()
        mid.setWidgetResizable(True)
        mid.setFrameShape(QScrollArea.Shape.NoFrame)
        midw = QWidget()
        mid.setWidget(midw)
        ml = QVBoxLayout(midw)
        ml.setContentsMargins(4, 0, 8, 0)

        ml.addWidget(section("1. Print a test sheet"))
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Paper"))
        self.test_sheet_size = QComboBox()
        self.test_sheet_size.addItems(PRINTABLE_SHEETS)
        srow.addWidget(self.test_sheet_size, 1)
        ml.addLayout(srow)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("Send to"))
        self.queue = QComboBox()
        self.queues = printing.list_printers()
        for p in self.queues:
            self.queue.addItem(p.label, p.name)
        if not self.queues:
            self.queue.addItem("No printer found", None)
        self.queue.currentIndexChanged.connect(self._queue_changed)
        prow.addWidget(self.queue, 1)
        ml.addLayout(prow)

        can_print = printing.available() and bool(self.queues)
        self._print_buttons = []
        for title, key, tip in TEST_SHEETS:
            row = QHBoxLayout()
            label = QLabel(title)
            label.setToolTip(tip)
            row.addWidget(label, 1)
            pb = QPushButton("Print")
            pb.setToolTip(
                "Print it straight away at exactly 100%" if can_print else
                "No printer queue was found, so only saving is available")
            pb.setEnabled(can_print)
            pb.clicked.connect(lambda _=False, k=key: self.print_sheet(k))
            self._print_buttons.append(pb)
            row.addWidget(pb)
            sb = QPushButton("Save…")
            sb.setToolTip("Write the PDF somewhere and open it")
            sb.clicked.connect(lambda _=False, k=key: self.save_sheet(k))
            row.addWidget(sb)
            ml.addLayout(row)

        scale_note = QLabel(
            "Print sends the sheet to the queue with scaling switched off. "
            "Saving opens it in a viewer instead, where a stray \"scale to "
            "fit\" will quietly ruin a calibration target.")
        scale_note.setWordWrap(True)
        scale_note.setStyleSheet("color: palette(mid);")
        ml.addWidget(scale_note)
        self.print_state = QLabel("")
        self.print_state.setWordWrap(True)
        ml.addWidget(self.print_state)

        ml.addWidget(section("2. Read it back"))
        krow = QHBoxLayout()
        krow.addWidget(QLabel("Measurements are"))
        self.kind = QComboBox()
        self.kind.addItems(MEASURE_KINDS)
        self.kind.setCurrentText("scan grey 0-255")
        self.kind.currentTextChanged.connect(self._recalc)
        krow.addWidget(self.kind, 1)
        ml.addLayout(krow)
        brow = QHBoxLayout()
        for label, slot in (("Load a scan…", self.load_scan),
                            ("Quick visual…", self.visual),
                            ("Paste values…", self.paste_values),
                            ("Clear", self.clear_measurements)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            brow.addWidget(b)
        ml.addLayout(brow)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Requested %", "Measured"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(240)
        self.table.itemChanged.connect(self._table_edited)
        ml.addWidget(self.table, 1)
        ml.addWidget(QLabel(
            "Tip: scan at 300 dpi, greyscale, with every scanner correction "
            "switched off."))

        ml.addWidget(section("3. Build the profile"))
        buildb = QPushButton("Build the correction from these readings")
        buildb.setToolTip("Fit a smooth curve to the patches and invert it. "
                          "Repeated patches are averaged and patches that "
                          "disagree with their neighbours are left out.")
        buildb.clicked.connect(self.build)
        ml.addWidget(buildb)
        self.fit_state = QLabel("")
        self.fit_state.setWordWrap(True)
        self.fit_state.setStyleSheet("color: palette(mid);")
        ml.addWidget(self.fit_state)

        ml.addWidget(section("4. Check it on paper"))
        checknote = QLabel(
            "Print the <b>linearisation check</b> above, measure it, and the "
            "error you read is the error that is left.")
        checknote.setWordWrap(True)
        checknote.setStyleSheet("color: palette(mid);")
        ml.addWidget(checknote)
        loadcheckb = QPushButton("Read a check sheet back…")
        loadcheckb.clicked.connect(self.load_verification)
        ml.addWidget(loadcheckb)
        split.addWidget(mid)

        # --- results ---
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        rl.addWidget(section("5. What your printer does"))
        self.plot = MiniPlot()
        self.plot.xlabel = "requested ink  ->"
        rl.addWidget(self.plot, 1)
        self.analysis = QLabel("No measurements yet.")
        self.analysis.setWordWrap(True)
        rl.addWidget(self.analysis)
        rl.addWidget(section("Front to back registration"))
        reg_note = QLabel(
            "Print the duplex registration sheet, hold it to a window with "
            "the front toward you, and read where the back's pointer falls "
            "on the front's scale. Type both numbers in exactly as you read "
            "them. The backs are then moved to match the fronts.")
        reg_note.setWordWrap(True)
        reg_note.setStyleSheet("color: palette(mid);")
        rl.addWidget(reg_note)
        self.duplex_form = QFormLayout()
        self.off_x = SliderSpin(-10, 10, 0, 0.1, 1, " mm")
        self.off_y = SliderSpin(-10, 10, 0, 0.1, 1, " mm")
        self.off_x.valueChanged.connect(self._offset_changed)
        self.off_y.valueChanged.connect(self._offset_changed)
        self.duplex_form.addRow("Reading across", self.off_x)
        self.duplex_form.addRow("Reading down", self.off_y)
        rl.addLayout(self.duplex_form)
        measure = QPushButton("Work it out from A, B and C…")
        measure.setToolTip("Enter all three stations and see whether what is "
                           "left is an offset, a skew or a scale difference")
        measure.clicked.connect(self.measure_registration)
        rl.addWidget(measure)
        self.reg_state = QLabel("")
        self.reg_state.setWordWrap(True)
        rl.addWidget(self.reg_state)
        applyb = QPushButton("Apply the suggested ink limits to this document")
        applyb.clicked.connect(self.apply_limits)
        rl.addWidget(applyb)
        rl.addStretch(0)
        split.addWidget(right)
        split.setSizes([260, 430, 430])
        outer.addWidget(split)

    # -- profile list -----------------------------------------------------
    def reload_profiles(self) -> None:
        self._updating = True
        self.plist.clear()
        self.profiles = list_profiles()
        for p in self.profiles:
            self.plist.addItem(QListWidgetItem(p.name))
        self._updating = False
        if self.profiles:
            self.plist.setCurrentRow(0)
        else:
            self._load_into_form(self.profile)

    def _select_profile(self, row: int) -> None:
        if self._updating or row < 0 or row >= len(self.profiles):
            return
        self.profile = self.profiles[row]
        self._load_into_form(self.profile)

    def _load_into_form(self, p: PrinterProfile) -> None:
        self._updating = True
        self.name.setText(p.name)
        self.model.setText(p.model)
        self.paper.setText(p.paper)
        self.dpi.setCurrentText(str(p.dpi))
        self.notes.setPlainText(p.notes)
        self.kind.setCurrentText(p.measure_kind)
        self._select_queue(p.queue)
        self.off_x.set_value(p.duplex_offset_x_mm)
        self.off_y.set_value(p.duplex_offset_y_mm)
        # The table has to be refilled before anything is allowed to harvest:
        # harvesting a stale table against a new profile looks like the user
        # editing the readings, and throws the profile's fitted curve away.
        self._fill_table(p.nominal, p.measured)
        self._updating = False
        self._offset_changed()
        self._recalc()

    def _harvest(self) -> None:
        p = self.profile
        p.name = self.name.text() or "Untitled printer"
        p.model = self.model.text()
        p.paper = self.paper.text()
        p.dpi = int(self.dpi.currentText())
        p.notes = self.notes.toPlainText()
        p.measure_kind = self.kind.currentText()
        p.queue = self._queue_name() or ""
        p.duplex_offset_x_mm = self.off_x.value()
        p.duplex_offset_y_mm = self.off_y.value()
        nom, mea = [], []
        for r in range(self.table.rowCount()):
            a = self.table.item(r, 0)
            b = self.table.item(r, 1)
            if not a or not b or not a.text().strip() or not b.text().strip():
                continue
            try:
                nom.append(float(a.text()) / 100.0)
                mea.append(float(b.text()))
            except ValueError:
                continue
        if p.is_fitted() and not _same_readings(nom, mea, p.nominal, p.measured):
            # The readings have been changed, so the fitted curve no longer
            # describes them. Fall back to the raw points until it is rebuilt.
            p.response_points = []
            p.passes = 0
            self._fit = None
        p.nominal, p.measured = nom, mea

    def new_profile(self) -> None:
        self.profile = PrinterProfile(name="New printer")
        self._load_into_form(self.profile)

    def save_profile(self) -> None:
        self._harvest()
        path = self.profile.save()
        QMessageBox.information(self, "Saved", f"Profile saved to\n{path}")
        keep = self.profile.name
        self.reload_profiles()
        for i in range(self.plist.count()):
            if self.plist.item(i).text() == keep:
                self.plist.setCurrentRow(i)
                break

    def delete_profile(self) -> None:
        row = self.plist.currentRow()
        if row < 0 or row >= len(self.profiles):
            return
        p = self.profiles[row]
        if QMessageBox.question(self, "Delete profile",
                                f"Delete '{p.name}'?") != \
                QMessageBox.StandardButton.Yes:
            return
        try:
            (printers_dir() / p.filename()).unlink()
        except Exception as exc:
            QMessageBox.warning(self, "Could not delete", str(exc))
        self.reload_profiles()

    def use_profile(self) -> None:
        self._harvest()
        self.project.set_profile(self.profile)
        self.app.renderer.invalidate()
        self.app.project_changed()
        QMessageBox.information(
            self, "Profile in use",
            f"'{self.profile.name}' is now applied to every image in this "
            f"document.")

    # -- test sheets ------------------------------------------------------
    def _sheet(self):
        return paper(self.test_sheet_size.currentText())

    def _queue_name(self) -> Optional[str]:
        return self.queue.currentData()

    def _select_queue(self, name: str) -> None:
        """Point the combo at ``name`` if that queue is still there."""
        if not name:
            return
        for i in range(self.queue.count()):
            if self.queue.itemData(i) == name:
                self.queue.setCurrentIndex(i)
                return

    def _queue_changed(self, *args) -> None:
        if self._updating:
            return
        self._harvest()

    def _ask(self, default: str) -> Optional[Path]:
        path, _ = QFileDialog.getSaveFileName(self, "Save test sheet",
                                              default, "PDF (*.pdf)")
        return Path(path) if path else None

    def _duplex_correction(self):
        """Whether to print the registration sheet with the correction in."""
        p = self.profile
        if not (p.duplex_offset_x_mm or p.duplex_offset_y_mm):
            return (0.0, 0.0)
        from ..core.imposition import duplex_axis, duplex_correction
        from ..core.units import MM as _MM
        axis = duplex_axis(self.project.imposition, self._sheet())
        answer = QMessageBox.question(
            self, "Check the correction?",
            f"This profile already carries a measured offset of "
            f"{p.duplex_offset_x_mm:+.1f} mm across and "
            f"{p.duplex_offset_y_mm:+.1f} mm down.\n\n"
            f"Print the sheet with that correction applied, so you can "
            f"check it lands on zero?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return (0.0, 0.0)
        return duplex_correction(p.duplex_offset_x_mm * _MM,
                                 p.duplex_offset_y_mm * _MM, axis)

    def _proof_image(self):
        for item in self.project.pages:
            src = self.project.library.get(item.source_id)
            if src and src.kind == "image":
                return src.pil_page(item.source_page, max_px=2400)
        return None

    def _check_ready(self) -> bool:
        """The check sheet is printed through a correction, so there has
        to be one."""
        if not self.profile.has_linearisation():
            QMessageBox.information(
                self, "Nothing to check",
                "Build a correction first: there is nothing to check until "
                "the profile knows what your printer does.")
            return False
        if not self.profile.is_fitted():
            return QMessageBox.question(
                self, "Build first?",
                "This profile is still using the raw readings rather than a "
                "fitted curve.\n\nCheck it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == \
                QMessageBox.StandardButton.Yes
        return True

    def _generate(self, key: str, path: Path) -> bool:
        """Write test sheet ``key`` to ``path``. False if it was not made."""
        self._harvest()
        sheet = self._sheet()
        p = self.profile
        try:
            if key == "linearisation":
                TS.linearisation_sheet(path, sheet, title=p.name)
                self._last_map = TS.map_path_for(path)
            elif key == "verification":
                if not self._check_ready():
                    return False
                TS.verification_sheet(path, p, sheet, title=p.name)
                self._last_check_map = TS.map_path_for(path)
            elif key == "screening":
                TS.screening_sheet(path, sheet, dpi=p.dpi)
            elif key == "detail":
                TS.detail_sheet(path, sheet, dpi=p.dpi)
            elif key == "duplex":
                TS.duplex_sheet(path, sheet, self._duplex_correction())
            elif key == "proof":
                TS.proof_sheet(path, self._proof_image(), sheet, p,
                               dpi=p.dpi, base=self.project.tone)
            else:
                raise ValueError(f"unknown test sheet {key!r}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not make the sheet", str(exc))
            return False
        return True

    def save_sheet(self, key: str) -> None:
        path = self._ask(SHEET_FILES[key])
        if path and self._generate(key, path):
            open_externally(path)

    def print_sheet(self, key: str) -> None:
        """Generate and submit in one go, with scaling switched off.

        The PDF is kept rather than thrown away: the linearisation and
        check sheets write a patch map beside it, and the scan reader needs
        that map whenever the sheet eventually gets measured.
        """
        path = test_sheets_dir() / SHEET_FILES[key]
        if not self._generate(key, path):
            return
        title = next((t for t, k, _ in TEST_SHEETS if k == key), key)
        # Only the registration sheet has a back; everything else is one
        # side, and asking for duplex on a one-page job wastes a sheet.
        duplex = "none"
        if key == "duplex":
            duplex = duplex_driver_setting(self.project.imposition,
                                           self._sheet())
        ok, message = printing.print_pdf(
            path, printer=self._queue_name(), duplex=duplex,
            media=media_name(self._sheet()),
            title=f"Signature Zine - {title}")
        if ok:
            extra = ""
            if key == "duplex":
                extra = f" Two-sided, flipping on the {duplex}."
            self.print_state.setText(
                f"<b>{message}.</b>{extra} The PDF is kept in "
                f"<i>{path.parent}</i>.")
        else:
            self.print_state.setText(f"<b>Not printed:</b> {message}")
            QMessageBox.warning(
                self, "Could not print", message
                + f"\n\nThe sheet itself was written to\n{path}\n\n"
                  "so you can print it by hand.")

    # -- measurements -----------------------------------------------------
    def _fill_table(self, nominal, measured) -> None:
        self.table.blockSignals(True)
        # Shrinking the table leaves the old items in the rows that survive,
        # and those get harvested back as if they were readings. Clear first.
        self.table.clearContents()
        self.table.setRowCount(max(len(nominal), 0) + 1)
        for r, (n, m) in enumerate(zip(nominal, measured)):
            self.table.setItem(r, 0, QTableWidgetItem(f"{n * 100:g}"))
            self.table.setItem(r, 1, QTableWidgetItem(f"{m:g}"))
        self.table.blockSignals(False)

    def _table_edited(self, *args) -> None:
        if self._updating:
            return
        self._recalc()

    def clear_measurements(self) -> None:
        self.profile.nominal = []
        self.profile.measured = []
        self._fill_table([], [])
        self._recalc()

    def paste_values(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Paste measurements")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "One patch per line: 'requested measured', or just the measured "
            "values in order from 0 to 100%."))
        edit = QPlainTextEdit()
        lay.addWidget(edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        nom, mea = [], []
        rows = [r for r in edit.toPlainText().splitlines() if r.strip()]
        for i, line in enumerate(rows):
            parts = line.replace(",", " ").split()
            try:
                if len(parts) >= 2:
                    nom.append(float(parts[0]) / 100.0)
                    mea.append(float(parts[1]))
                elif parts:
                    nom.append(i / max(1, len(rows) - 1))
                    mea.append(float(parts[0]))
            except ValueError:
                continue
        if len(nom) < 4:
            QMessageBox.warning(self, "Not enough data",
                                "At least four patches are needed.")
            return
        self._fill_table(nom, mea)
        self._recalc()

    def visual(self) -> None:
        dlg = VisualDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._harvest()
        p = from_visual_reading(self.profile.name or "Visual",
                                dlg.light.value() / 100.0,
                                dlg.dark.value() / 100.0,
                                dlg.mid.value() / 100.0)
        p.model, p.paper, p.dpi = self.profile.model, self.profile.paper, \
            self.profile.dpi
        self.profile = p
        self._load_into_form(p)

    def load_scan(self) -> None:
        img_path, _ = QFileDialog.getOpenFileName(
            self, "Open the scanned test sheet", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)")
        if not img_path:
            return
        map_path = self._last_map
        if not map_path or not Path(map_path).exists():
            guess = Path(img_path).with_suffix(".patches.json")
            if guess.exists():
                map_path = guess
        if not map_path or not Path(map_path).exists():
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Where is the patch map for this sheet?", "",
                "Patch map (*.patches.json);;JSON (*.json)")
            if not chosen:
                QMessageBox.information(
                    self, "Need the patch map",
                    "The .patches.json file is written next to the test sheet "
                    "PDF when you generate it.")
                return
            map_path = Path(chosen)
        try:
            smap = TS.SheetMap.load(Path(map_path))
            img = load_scan(img_path)
            result = measure_sheet(img, smap)
        except Exception as exc:
            QMessageBox.warning(self, "Could not read the scan", str(exc))
            return
        if ScanCheckDialog(img, result, self).exec() != \
                QDialog.DialogCode.Accepted:
            return
        self.kind.setCurrentText("scan grey 0-255")
        self._fill_table(result.nominal, result.values)
        self._recalc()

    # -- building and checking --------------------------------------------
    def build(self) -> None:
        self._harvest()
        p = self.profile
        if len(p.nominal) < 4:
            QMessageBox.information(
                self, "Not enough patches",
                "Measure the linearisation target first - at least four "
                "patches, and a full sheet is much better.")
            return
        try:
            built, report = build_profile(p.nominal, p.measured,
                                         p.measure_kind, name=p.name,
                                         template=p)
        except Exception as exc:
            QMessageBox.warning(self, "Could not build the profile", str(exc))
            return
        self.profile = built
        self._load_into_form(built)
        self._fit = report
        self._show_fit()
        if not report.trustworthy():
            QMessageBox.warning(
                self, "Built, but check it",
                "The readings do not describe a smooth curve well:\n\n"
                + "\n".join(report.summary())
                + "\n\nIt is still usable, but print the check sheet before "
                  "trusting it.")

    def _show_fit(self) -> None:
        if self._fit is None:
            if self.profile.is_fitted():
                self.fit_state.setText(
                    "Carrying a fitted correction"
                    + (f", refined over {self.profile.passes} passes."
                       if self.profile.passes > 1 else "."))
            else:
                self.fit_state.setText(
                    "No fitted correction yet. Without one the raw readings "
                    "are used directly, noise and all.")
            return
        self.fit_state.setText(" ".join(self._fit.summary()))

    def load_verification(self) -> None:
        self._harvest()
        if not self.profile.has_linearisation():
            QMessageBox.information(self, "Nothing to check against",
                                    "Build a profile first.")
            return
        smap = self._pick_map(self._last_check_map, "verification")
        if smap is None:
            return
        img_path, _ = QFileDialog.getOpenFileName(
            self, "Open the scanned check sheet", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)")
        if not img_path:
            return
        try:
            img = load_scan(img_path)
            result = measure_sheet(img, smap)
        except Exception as exc:
            QMessageBox.warning(self, "Could not read the scan", str(exc))
            return
        if ScanCheckDialog(img, result, self).exec() != \
                QDialog.DialogCode.Accepted:
            return
        report = verify(self.profile, result.nominal, result.values,
                        "scan grey 0-255")
        dlg = VerifyDialog(report, self)
        dlg.exec()
        if not dlg.refine_requested:
            return
        try:
            better, fit = refine(self.profile, result.nominal, result.values,
                                 "scan grey 0-255")
        except Exception as exc:
            QMessageBox.warning(self, "Could not refine", str(exc))
            return
        self.profile = better
        self._load_into_form(better)
        self._fit = fit
        self._show_fit()
        QMessageBox.information(
            self, "Refined",
            f"'{better.name}' is now on pass {better.passes}.\n\n"
            f"Print the check sheet again to confirm it landed. Save the "
            f"profile to keep it.")

    def _pick_map(self, remembered: Optional[Path], kind: str):
        """Find the patch map for a sheet, asking only when it has to."""
        path = remembered if remembered and Path(remembered).exists() else None
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Where is the patch map for this sheet?", "",
                "Patch map (*.patches.json);;JSON (*.json)")
            if not chosen:
                QMessageBox.information(
                    self, "Need the patch map",
                    "The .patches.json file is written next to the sheet's "
                    "PDF when you generate it.")
                return None
            path = Path(chosen)
        try:
            smap = TS.SheetMap.load(Path(path))
        except Exception as exc:
            QMessageBox.warning(self, "Could not read the patch map", str(exc))
            return None
        if kind and smap.kind != kind:
            QMessageBox.warning(
                self, "Wrong sheet",
                f"That patch map is for a '{smap.kind}' sheet, not a "
                f"'{kind}' one.")
            return None
        return smap

    def measure_registration(self) -> None:
        dlg = RegistrationDialog(self._sheet(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ax, ay = dlg.offset
        self._updating = True
        self.off_x.set_value(ax)
        self.off_y.set_value(ay)
        self._updating = False
        self._offset_changed()

    def _offset_changed(self, *args) -> None:
        if self._updating:
            return
        self._harvest()
        x, y = self.profile.duplex_offset_x_mm, self.profile.duplex_offset_y_mm
        if x == 0 and y == 0:
            self.reg_state.setText("No correction: the two sides are printed "
                                   "where the layout puts them.")
        else:
            self.reg_state.setText(
                f"<b>Backs will be moved by {abs(x):.1f} mm "
                f"{'right' if x > 0 else 'left'} and {abs(y):.1f} mm "
                f"{'down' if y < 0 else 'up'}</b> relative to the layout, to "
                f"land under the fronts.")
        live = (self.project.profile is not None
                and self.project.profile.name == self.profile.name)
        if live:
            self.project.profile.duplex_offset_x_mm = x
            self.project.profile.duplex_offset_y_mm = y
            self.app.renderer.invalidate()
            self.app.layout_changed()

    # -- analysis ---------------------------------------------------------
    def _recalc(self) -> None:
        self._harvest()
        p = self.profile
        self.plot.clear()
        self._show_fit()
        if not p.has_linearisation():
            self.analysis.setText(
                "No measurements yet. Print the linearisation target, then "
                "scan it back in or read it by eye.")
            return
        resp = p.response_lut(256)
        corr = p.linearisation_lut(256)
        self.plot.add(resp, QColor(200, 60, 60),
                      "what it prints" if p.is_fitted()
                      else "what it prints (unfitted)")
        self.plot.add(corr, QColor(60, 120, 210), "the correction", dashed=True)
        try:
            self.plot.add_points(np.asarray(p.nominal), p.coverage(),
                                 QColor(120, 120, 120))
        except Exception:
            pass
        a = p.analyse()
        lo, hi = p.suggested_limits()
        built = ""
        if p.is_fitted():
            built = ("fitted curve"
                     + (f", refined {p.passes - 1}x" if p.passes > 1 else "")
                     + f", {p.fit_rms * 100:.2f}% residual<br>")
        self.analysis.setText(
            f"<b>{p.name}</b><br>{built}"
            f"Dot gain: {a['gain_25'] * 100:+.1f}% at 25%, "
            f"{a['gain_50'] * 100:+.1f}% at 50%, "
            f"{a['gain_75'] * 100:+.1f}% at 75%<br>"
            f"Smallest dot that prints: {a['min_printable'] * 100:.1f}%<br>"
            f"Shadows stop separating above {a['shadow_merge'] * 100:.0f}%<br>"
            f"{a['patches']} patches read, {a['flat_steps']} of them "
            f"indistinguishable from their neighbour<br>"
            f"Suggested limits: minimum dot {lo * 100:.1f}%, "
            f"maximum ink {hi * 100:.0f}%")

    def apply_limits(self) -> None:
        self._harvest()
        if not self.profile.has_linearisation():
            QMessageBox.information(self, "Nothing to apply",
                                    "Measure the printer first.")
            return
        lo, hi = self.profile.suggested_limits()
        t = self.project.tone
        t.min_dot, t.max_ink = lo, hi
        t.use_profile = True
        self.project.set_profile(self.profile)
        self.app.renderer.invalidate()
        self.app.project_changed()
        QMessageBox.information(
            self, "Applied",
            f"Minimum dot {lo * 100:.1f}% and maximum ink {hi * 100:.0f}% are "
            f"now in force, using '{self.profile.name}'.")
