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

from tests.test_core import press, read_patch, target_levels


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


if __name__ == "__main__":
    unittest.main()
