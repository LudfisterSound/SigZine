"""Tests for the bits of UI logic that can go wrong silently.

Only the data handling is tested here, not how anything looks: the table
that holds the measurements is the profile's only route in from the user,
and when it disagrees with the profile the readings quietly rot.

Qt needs somewhere to draw. On a machine with no display it is pointed at
the offscreen platform before it loads, because Qt aborts the process
rather than raising when it cannot find a platform plugin - an exception
could be caught and skipped over, an abort takes the whole test run with
it. If PySide6 is not installed at all, the module skips.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if (not os.environ.get("QT_QPA_PLATFORM") and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
        and sys.platform not in ("darwin", "win32")):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtWidgets import QApplication, QTableWidgetItem
    _APP = QApplication.instance() or QApplication([])
    _QT_ERROR = ""
except Exception as exc:                                  # pragma: no cover
    _APP = None
    _QT_ERROR = str(exc)

from unittest import mock

from sigzine.core.units import MM
from tests.test_core import press, read_patch, target_levels


class QtAvailabilityTests(unittest.TestCase):
    """On a build machine, a skipped UI test is a silently untested UI.

    Qt is allowed to be missing on a developer's box, where the rest of the
    suite still runs.  Under CI the libraries are installed on purpose, so a
    Qt that will not start is a failure rather than seventeen quiet skips.
    """

    def test_qt_starts_wherever_ci_says_it_should(self):
        if not os.environ.get("CI"):
            self.skipTest("not a CI run")
        self.assertIsNotNone(_APP, f"Qt would not start under CI: {_QT_ERROR}")


class _FakeRenderer:
    def invalidate(self) -> None:
        pass


class _FakeApp:
    """Just enough of MainWindow for PrinterTab to run against."""

    def __init__(self) -> None:
        from sigzine.core.project import Project
        self.project = Project()
        self.renderer = _FakeRenderer()

    def project_changed(self) -> None:
        pass

    def layout_changed(self) -> None:
        pass


@unittest.skipIf(_APP is None, f"Qt will not start here: {_QT_ERROR}")
class PrinterTabTests(unittest.TestCase):
    def setUp(self) -> None:
        from sigzine.ui.printer_tab import PrinterTab
        self.tab = PrinterTab(_FakeApp())
        self.tab.kind.setCurrentText("scan grey 0-255")

    def _readings(self, noise: float = 4.0, seed: int = 1):
        levels = target_levels()
        vals = read_patch(press(levels, 0.18), noise=noise,
                          rng=np.random.default_rng(seed))
        return levels, [float(v) for v in vals]

    def _build(self):
        levels, vals = self._readings()
        self.tab._fill_table(levels, vals)
        self.tab.build()
        return self.tab.profile

    def test_shrinking_the_table_does_not_leave_a_stale_reading(self):
        """Qt keeps the items in rows that survive a row-count change, and
        those get harvested back as if the user had typed them."""
        levels, vals = self._readings()
        self.tab._fill_table(levels, vals)
        self.tab._fill_table(levels[:10], vals[:10])
        self.tab._harvest()
        self.assertEqual(len(self.tab.profile.nominal), 10)
        self.assertEqual(len(self.tab.profile.measured), 10)

    def test_building_leaves_a_fitted_profile(self):
        p = self._build()
        self.assertTrue(p.is_fitted())
        self.assertEqual(p.passes, 1)
        self.assertEqual(len(p.nominal), len(target_levels()))

    def test_loading_a_profile_keeps_its_fitted_curve(self):
        """Refilling the form must not look like the user editing the
        readings, or the fit is thrown away the moment it is loaded."""
        p = self._build()
        before = p.response_lut(256)
        self.tab._load_into_form(p)
        self.assertTrue(self.tab.profile.is_fitted())
        self.assertTrue(np.allclose(self.tab.profile.response_lut(256), before))

    def test_a_saved_profile_comes_back_fitted(self):
        from sigzine.core.calibration import PrinterProfile
        p = self._build()
        with tempfile.TemporaryDirectory() as d:
            again = PrinterProfile.load(p.save(Path(d)))
        self.tab.profile = again
        self.tab._load_into_form(again)
        self.assertTrue(self.tab.profile.is_fitted())
        self.assertEqual(self.tab.profile.passes, 1)

    def test_refining_through_the_tab_advances_the_pass_count(self):
        from sigzine.core import linearise as lin
        p = self._build()
        wanted = np.linspace(0, 1, 21).tolist()
        grid = np.linspace(0, 1, 1024)
        sent = np.interp(wanted, grid, p.linearisation_lut(1024))
        check = [float(v) for v in read_patch(press(sent, 0.18))]
        better, fit = lin.refine(p, wanted, check, "scan grey 0-255")
        self.tab.profile = better
        self.tab._load_into_form(better)
        self.assertTrue(self.tab.profile.is_fitted())
        self.assertEqual(self.tab.profile.passes, 2)
        self.assertEqual(len(self.tab.profile.nominal), 21)

    def test_editing_a_reading_drops_the_fit(self):
        """A fitted curve describes a particular set of readings. Change
        them and it is stale, so it has to go until it is rebuilt."""
        self._build()
        self.tab.table.setItem(5, 1, QTableWidgetItem("99"))
        self.tab._harvest()
        self.assertFalse(self.tab.profile.is_fitted())
        self.assertEqual(self.tab.profile.passes, 0)

    def test_retyping_the_same_reading_keeps_the_fit(self):
        self._build()
        item = self.tab.table.item(5, 1)
        self.tab.table.setItem(5, 1, QTableWidgetItem(item.text()))
        self.tab._harvest()
        self.assertTrue(self.tab.profile.is_fitted())


@unittest.skipIf(_APP is None, f"Qt will not start here: {_QT_ERROR}")
class TestSheetPrintingTests(unittest.TestCase):
    """The Print buttons beside each test sheet.

    A calibration target is the one thing that has to come out at exactly
    100%, so these check that the job really is submitted with scaling off
    rather than handed to a viewer, and that the patch map stays with the
    PDF so the sheet can still be measured later.
    """

    def setUp(self) -> None:
        from sigzine.core import printing
        self.submitted = []
        self.result = (True, "Sent it")
        self.queues = [printing.Printer("Brother_HL_3170CDW", "idle", True),
                       printing.Printer("Other_Printer")]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        def fake_print_pdf(path, printer=None, copies=1, duplex="none",
                           media=None, title=None, extra=None):
            self.submitted.append(dict(path=Path(path), printer=printer,
                                       duplex=duplex, media=media,
                                       title=title))
            return self.result

        patches = [
            mock.patch.object(printing, "list_printers", lambda: self.queues),
            mock.patch.object(printing, "available", lambda: True),
            mock.patch.object(printing, "print_pdf", fake_print_pdf),
            mock.patch("sigzine.ui.printer_tab.test_sheets_dir",
                       lambda: Path(self.tmp.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        from sigzine.ui.printer_tab import PrinterTab
        self.tab = PrinterTab(_FakeApp())
        self.tab.kind.setCurrentText("scan grey 0-255")

    def _build_profile(self):
        levels = target_levels()
        vals = [float(v) for v in read_patch(press(levels, 0.18))]
        self.tab._fill_table(levels, vals)
        self.tab.build()

    def test_printing_submits_instead_of_opening_a_viewer(self):
        with mock.patch("sigzine.ui.printer_tab.open_externally") as opened:
            self.tab.print_sheet("linearisation")
        opened.assert_not_called()
        self.assertEqual(len(self.submitted), 1)
        self.assertTrue(self.submitted[0]["path"].exists())

    def test_the_job_goes_to_the_chosen_queue(self):
        self.tab.queue.setCurrentIndex(1)
        self.tab.print_sheet("detail")
        self.assertEqual(self.submitted[0]["printer"], "Other_Printer")

    def test_only_the_registration_sheet_is_printed_two_sided(self):
        for key in ("linearisation", "detail", "screening", "proof"):
            self.tab.print_sheet(key)
        self.assertTrue(all(j["duplex"] == "none" for j in self.submitted))
        self.submitted.clear()
        self.tab.print_sheet("duplex")
        self.assertIn(self.submitted[0]["duplex"], ("long edge", "short edge"))

    def test_the_patch_map_is_kept_beside_the_printed_sheet(self):
        """Printed sheets get measured later, sometimes days later, and the
        scan reader needs the map that was written with them."""
        from sigzine.core import testsheet as TS
        self.tab.print_sheet("linearisation")
        pdf = self.submitted[0]["path"]
        self.assertTrue(TS.map_path_for(pdf).exists())
        self.assertEqual(Path(self.tab._last_map), TS.map_path_for(pdf))
        self.assertEqual(TS.SheetMap.load(TS.map_path_for(pdf)).kind,
                         "linearisation")

    def test_the_check_sheet_needs_a_profile_first(self):
        with mock.patch("sigzine.ui.printer_tab.QMessageBox.information"):
            self.tab.print_sheet("verification")
        self.assertEqual(self.submitted, [])

    def test_the_check_sheet_prints_once_there_is_a_profile(self):
        from sigzine.core import testsheet as TS
        self._build_profile()
        self.tab.print_sheet("verification")
        self.assertEqual(len(self.submitted), 1)
        smap = TS.SheetMap.load(TS.map_path_for(self.submitted[0]["path"]))
        self.assertEqual(smap.kind, "verification")

    def test_a_failed_job_still_leaves_the_pdf_behind(self):
        """If the queue refuses it, the sheet is still on disk to print by
        hand - the work of generating it is not thrown away."""
        self.result = (False, "printer is on fire")
        with mock.patch("sigzine.ui.printer_tab.QMessageBox.warning") as warned:
            self.tab.print_sheet("detail")
        self.assertTrue(self.submitted[0]["path"].exists())
        warned.assert_called_once()
        self.assertIn("Not printed", self.tab.print_state.text())

    def test_the_queue_is_remembered_on_the_profile(self):
        """A profile describes one printer, so it should know which one."""
        self.tab.queue.setCurrentIndex(1)
        self.tab._harvest()
        self.assertEqual(self.tab.profile.queue, "Other_Printer")
        from sigzine.core.calibration import PrinterProfile
        with tempfile.TemporaryDirectory() as d:
            again = PrinterProfile.load(self.tab.profile.save(Path(d)))
        self.tab._load_into_form(again)
        self.assertEqual(self.tab._queue_name(), "Other_Printer")

    def test_saving_still_writes_and_opens_a_file(self):
        target = Path(self.tmp.name) / "chosen.pdf"
        with mock.patch.object(type(self.tab), "_ask", lambda s, d: target), \
                mock.patch("sigzine.ui.printer_tab.open_externally") as opened:
            self.tab.save_sheet("detail")
        self.assertTrue(target.exists())
        opened.assert_called_once()
        self.assertEqual(self.submitted, [])


@unittest.skipIf(_APP is None, f"Qt will not start here: {_QT_ERROR}")
class LayoutTabPaperTests(unittest.TestCase):
    """Choosing a different paper has to carry the layout with it."""

    def setUp(self) -> None:
        from sigzine.ui.layout_tab import LayoutTab
        self.app = _FakeApp()
        self.app.project.apply_document_preset("saddle-folio", "Letter")
        self.tab = LayoutTab(self.app)
        self.tab.refresh()

    def test_the_feed_starts_on_automatic(self):
        self.assertEqual(self.tab.orientation.currentText(), "Automatic")
        self.assertEqual(self.app.project.imposition.orientation, "auto")

    def test_choosing_another_paper_moves_the_page_and_the_margins(self):
        from sigzine.core.units import paper
        p = self.app.project
        before = p.imposition.margin_inner
        self.tab.sheet.setCurrentText("A3")
        self.assertAlmostEqual(p.imposition.sheet_size.width,
                               paper("A3").width, places=1)
        # the page grew, so the margins came with it, and the controls show it
        self.assertGreater(p.imposition.margin_inner, before)
        self.assertAlmostEqual(self.tab.m_inner.value(),
                               p.imposition.margin_inner / MM, places=1)
        self.assertAlmostEqual(self.tab.sheet_margin.value(),
                               p.imposition.sheet_margin / MM, places=1)

    def test_the_layout_still_fills_the_sheet_after_a_change_of_paper(self):
        from sigzine.core.imposition import build_plan
        for name in ("A4", "A3", "Legal", "Letter"):
            self.tab.sheet.setCurrentText(name)
            s = self.app.project.imposition
            plan = build_plan(8, s)
            cells = [sl.cell_rect for sl in plan.sheets[0].slots]
            self.assertAlmostEqual(max(c[2] for c in cells),
                                   plan.sheet_size.width - s.sheet_margin,
                                   places=4, msg=name)
            self.assertAlmostEqual(max(c[3] for c in cells),
                                   plan.sheet_size.height - s.sheet_margin,
                                   places=4, msg=name)

    def test_the_feed_can_still_be_overruled_by_hand(self):
        self.tab.orientation.setCurrentText("Portrait")
        s = self.app.project.imposition
        self.assertEqual(s.orientation, "portrait")
        self.assertGreater(s.effective_sheet().height, s.effective_sheet().width)


@unittest.skipIf(_APP is None, f"Qt will not start here: {_QT_ERROR}")
class NewDocumentPaperTests(unittest.TestCase):
    """The dialog asks for the construction and the paper separately."""

    def test_the_paper_choice_changes_what_the_templates_promise(self):
        from sigzine.ui.new_document import NewDocumentDialog
        dlg = NewDocumentDialog(allow_open=False)
        dlg.paper.setCurrentText("Letter")
        letter = dlg.lists["zine"].item(0).text()
        dlg.paper.setCurrentText("A3")
        a3 = dlg.lists["zine"].item(0).text()
        self.assertEqual(dlg.sheet_name, "A3")
        self.assertNotEqual(letter, a3)
        self.assertIn("A3", a3)
        # the construction itself is the same template either way
        self.assertEqual(letter.split("\n")[0], a3.split("\n")[0])

    def test_the_chosen_paper_comes_back_with_the_template(self):
        from sigzine.core.project import Project
        from sigzine.core.units import paper
        from sigzine.ui.new_document import NewDocumentDialog
        dlg = NewDocumentDialog(allow_open=False)
        dlg.paper.setCurrentText("A4")
        dlg.accept()
        p = Project()
        p.apply_document_preset(dlg.preset_key, dlg.sheet_name)
        self.assertAlmostEqual(p.imposition.sheet_size.width,
                               paper("A4").width, places=1)


@unittest.skipIf(_APP is None, f"Qt will not start here: {_QT_ERROR}")
class NoPrinterTests(unittest.TestCase):
    def test_print_buttons_are_disabled_with_no_queue(self):
        from sigzine.core import printing
        with mock.patch.object(printing, "list_printers", lambda: []), \
                mock.patch.object(printing, "available", lambda: False):
            from sigzine.ui.printer_tab import PrinterTab
            tab = PrinterTab(_FakeApp())
        self.assertTrue(tab._print_buttons)
        self.assertFalse(any(b.isEnabled() for b in tab._print_buttons))
        self.assertIsNone(tab._queue_name())
        tab._harvest()
        self.assertEqual(tab.profile.queue, "")


if __name__ == "__main__":
    unittest.main()
