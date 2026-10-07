"""Regression tests for the imposition, tone and calibration engines.

Run with:  venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from unittest import mock
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz

from sigzine.core import binding as bindings
from sigzine.core import calibration as cal
from sigzine.core import imposition as imp
from sigzine.core import linearise as lin
from sigzine.core import printing
from sigzine.core import scan as scanmod
from sigzine.core import templates, testsheet
from sigzine.core import tone as T
from sigzine.core.project import DOCUMENT_PRESETS, Project
from sigzine.core.render import Renderer, place_geometry
from sigzine.core.typeset import TextStyle, typeset
from sigzine.core.units import MM, Size, paper


def make_photo(path: Path, w: int = 400, h: int = 300) -> Path:
    testsheet.synthetic_photo(w, h).convert("RGB").save(path, quality=88)
    return path


def colour_photo(w: int = 200, h: int = 150) -> Image.Image:
    """The synthetic photo, tinted differently in each quarter.

    synthetic_photo is a single channel by design, so a colour test needs
    something with actual chroma in it. Tinting keeps the smooth tonality
    that the tone pipeline cares about while giving every channel its own
    content to carry.
    """
    base = np.asarray(testsheet.synthetic_photo(w, h).convert("L"),
                      dtype=np.float32) / 255.0
    tint = np.zeros((h, w, 3), dtype=np.float32)
    tint[:, :w // 2] = (1.0, 0.35, 0.2)          # warm left
    tint[:, w // 2:] = (0.2, 0.45, 1.0)          # cool right
    tint[: h // 3, :] = (0.3, 1.0, 0.4)          # green band across the top
    arr = np.clip(base[..., None] * tint * 255.0, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def make_colour_photo(path: Path, w: int = 200, h: int = 150) -> Path:
    colour_photo(w, h).save(path, quality=92)
    return path


class FoldingTests(unittest.TestCase):
    def test_folio_matches_the_classic_forme(self):
        layout = imp.simulate_folding(1)
        self.assertEqual(len(layout), 4)
        front = {(c, r): pg for pg, (s, c, r, rot) in enumerate(layout, 1)
                 if s == "F"}
        self.assertEqual(front[(1, 0)], 1)
        self.assertEqual(front[(0, 0)], 4)

    def test_quarto_matches_the_classic_forme(self):
        layout = imp.simulate_folding(2)
        front = {(c, r): (pg, rot) for pg, (s, c, r, rot)
                 in enumerate(layout, 1) if s == "F"}
        self.assertEqual(front[(1, 1)], (1, 0))
        self.assertEqual(front[(0, 1)], (8, 0))
        self.assertEqual(front[(1, 0)], (4, 180))
        self.assertEqual(front[(0, 0)], (5, 180))

    def test_octavo_outer_forme(self):
        layout = imp.simulate_folding(3)
        outer = sorted(pg for pg, (s, *_rest) in enumerate(layout, 1)
                       if s == "F")
        self.assertEqual(outer, [1, 4, 5, 8, 9, 12, 13, 16])

    def test_consecutive_pages_share_a_leaf(self):
        for folds in (1, 2, 3, 4):
            layout = imp.simulate_folding(folds)
            for i in range(0, len(layout), 2):
                a, b = layout[i], layout[i + 1]
                self.assertEqual((a[1], a[2]), (b[1], b[2]),
                                 f"{folds} folds: pages {i+1}/{i+2} not on one leaf")
                self.assertNotEqual(a[0], b[0])


class ImpositionTests(unittest.TestCase):
    def _settings(self, key: str) -> imp.ImpositionSettings:
        s = imp.ImpositionSettings(binding_key=key)
        b = bindings.get(key)
        s.single_sided = not b.duplex
        s.creep_enabled = b.creep
        return s

    def test_every_page_placed_exactly_once(self):
        for key in bindings.keys():
            for count in (1, 3, 8, 17, 33):
                s = self._settings(key)
                total = imp.padded_page_count(count, s)
                plan = imp.build_plan(count, s)
                seen = [sl.page for sp in plan.sheets for sl in sp.slots
                        if sl.page is not None]
                self.assertEqual(sorted(seen), list(range(1, total + 1)),
                                 f"{key} with {count} pages")

    def test_padding_is_a_whole_number_of_sheets(self):
        for key in bindings.keys():
            b = bindings.get(key)
            for count in (1, 5, 9, 30):
                s = self._settings(key)
                total = imp.padded_page_count(count, s)
                self.assertGreaterEqual(total, count)
                if b.family == bindings.FOLDED:
                    per_sheet = 2 ** s.folds_per_sheet * 2
                    self.assertEqual(total % per_sheet, 0, key)

    def test_saddle_stitch_page_pairs_face_each_other(self):
        s = self._settings("saddle")
        plan = imp.build_plan(16, s)
        for sp in plan.sheets:
            pages = sorted(sl.page for sl in sp.slots if sl.page)
            self.assertEqual(len(pages), 2)
            self.assertEqual(sum(pages) % 2, 1,
                             "a folio side always holds one odd and one even page")

    def test_creep_moves_inner_sheets_toward_the_spine(self):
        s = self._settings("saddle")
        s.sheets_per_signature = 4
        s.creep_per_sheet = 1.0 * MM
        plan = imp.build_plan(16, s)
        outer = [sl for sp in plan.sheets if sp.sheet_in_signature == 1
                 for sl in sp.slots if sl.page == 1][0]
        inner = [sl for sp in plan.sheets if sp.sheet_in_signature == 4
                 for sl in sp.slots if sl.page == 7][0]
        self.assertAlmostEqual(outer.creep, 0.0)
        self.assertAlmostEqual(inner.creep, 3.0 * MM)
        # page 1 is a recto, so its spine is on the left: it shifts left
        self.assertLess(inner.trim_rect[0], outer.trim_rect[0])

    def test_duplex_axis_depends_on_orientation(self):
        s = imp.ImpositionSettings(back_flip="long")
        self.assertEqual(imp.duplex_axis(s, Size(612, 792)), "vertical")
        self.assertEqual(imp.duplex_axis(s, Size(792, 612)), "horizontal")
        s.back_flip = "auto"
        self.assertEqual(imp.duplex_axis(s, Size(792, 612)), "vertical")
        self.assertEqual(imp.duplex_driver_setting(s, Size(792, 612)),
                         "short edge")

    def test_backs_line_up_behind_fronts(self):
        s = self._settings("saddle")
        plan = imp.build_plan(8, s)
        front = {sl.page: sl for sl in plan.sheets[0].slots}
        back = {sl.page: sl for sl in plan.sheets[1].slots}
        # page 2 is the reverse of page 1, so it sits in the mirrored cell
        cols = 2
        self.assertEqual(back[2].cell[0], cols - 1 - front[1].cell[0])

    def test_mini_zine_is_one_sheet_of_eight(self):
        s = self._settings("mini8")
        plan = imp.build_plan(5, s)
        self.assertEqual(len(plan.sheets), 1)
        self.assertEqual(sorted(sl.page for sl in plan.sheets[0].slots),
                         list(range(1, 9)))

    def test_fukurotoji_is_single_sided_pairs(self):
        s = self._settings("stab")
        plan = imp.build_plan(6, s)
        self.assertTrue(all(sp.side == "front" for sp in plan.sheets))
        first = plan.sheets[0]
        self.assertEqual(sorted(sl.page for sl in first.slots), [1, 2])

    def test_flat_cut_and_stack_splits_the_book_in_half(self):
        s = self._settings("perfect")
        s.up = 2
        plan = imp.build_plan(16, s)
        left = [sl.page for sp in plan.sheets if sp.side == "front"
                for sl in sp.slots if sl.cell[0] == 0]
        self.assertTrue(all(p <= 8 for p in left), left)


class CutFoldWasteTests(unittest.TestCase):
    """Every fold but the spine is cut open, so it needs trim waste."""

    def _settings(self, folds: int, **kw) -> imp.ImpositionSettings:
        s = imp.ImpositionSettings(binding_key="sewn", folds_per_sheet=folds,
                                   sheet_margin=6 * MM)
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_only_the_spine_fold_is_left_folded(self):
        self.assertEqual(imp.cut_fold_boundaries(1), ([], []))
        self.assertEqual(imp.cut_fold_boundaries(2), ([], [1]))
        self.assertEqual(imp.cut_fold_boundaries(3), ([2], [1]))
        self.assertEqual(imp.cut_fold_boundaries(4), ([2], [1, 2, 3]))

    def test_quarto_leaves_waste_at_the_head_fold(self):
        s = self._settings(2, fold_trim=1.5 * MM)
        plan = imp.build_plan(8, s)
        front = plan.sheets[0]
        top = [sl for sl in front.slots if sl.cell[1] == 0]
        bottom = [sl for sl in front.slots if sl.cell[1] == 1]
        for a, b in zip(sorted(top, key=lambda s: s.cell[0]),
                        sorted(bottom, key=lambda s: s.cell[0])):
            self.assertAlmostEqual(b.trim_rect[1] - a.trim_rect[3], 3.0 * MM)

    def test_the_spine_fold_gets_no_waste(self):
        s = self._settings(2, fold_trim=1.5 * MM)
        front = imp.build_plan(8, s).sheets[0]
        left = [sl for sl in front.slots if sl.cell == (0, 0)][0]
        right = [sl for sl in front.slots if sl.cell == (1, 0)][0]
        self.assertAlmostEqual(left.trim_rect[2], right.trim_rect[0])

    def test_pages_stay_the_same_size_across_a_cut_fold(self):
        for folds in (1, 2, 3, 4):
            s = self._settings(folds, fold_trim=2 * MM)
            plan = imp.build_plan(2 ** folds * 2, s)
            sizes = {(round(sl.trim_rect[2] - sl.trim_rect[0], 6),
                      round(sl.trim_rect[3] - sl.trim_rect[1], 6))
                     for sp in plan.sheets for sl in sp.slots}
            self.assertEqual(len(sizes), 1, f"{folds} folds: {sizes}")
            self.assertEqual(sizes.pop(),
                             (round(plan.page_size.width, 6),
                              round(plan.page_size.height, 6)))

    def test_cut_open_folds_are_marked_cut_not_fold(self):
        s = self._settings(3, fold_trim=1.5 * MM)
        front = imp.build_plan(16, s).sheets[0]
        self.assertEqual([k for *_, k in front.cut_lines], ["cut", "cut"])
        self.assertEqual([k for *_, k in front.fold_lines], ["fold", "fold"])
        sheet = s.effective_sheet()
        # the cut runs down the middle of the waste, not along a page edge
        xs = [x0 for (x0, _, x1, _, _) in front.cut_lines if x0 == x1]
        self.assertEqual(len(xs), 1)
        self.assertAlmostEqual(xs[0], sheet.width / 2)
        for sl in front.slots:
            self.assertNotAlmostEqual(sl.trim_rect[0], sheet.width / 2)
            self.assertNotAlmostEqual(sl.trim_rect[2], sheet.width / 2)

    def test_no_allowance_asked_for_means_pages_abut_as_before(self):
        s = self._settings(2, fold_trim=0.0)
        front = imp.build_plan(8, s).sheets[0]
        top = [sl for sl in front.slots if sl.cell == (0, 0)][0]
        bottom = [sl for sl in front.slots if sl.cell == (0, 1)][0]
        self.assertAlmostEqual(top.trim_rect[3], bottom.trim_rect[1])

    def test_an_absurd_allowance_cannot_eat_the_page(self):
        s = self._settings(2, fold_trim=500.0)
        plan = imp.build_plan(8, s)
        for sp in plan.sheets:
            for sl in sp.slots:
                self.assertGreater(sl.trim_rect[2] - sl.trim_rect[0], 0)
                self.assertGreater(sl.trim_rect[3] - sl.trim_rect[1], 0)

    def test_the_document_page_size_knows_about_the_waste(self):
        p = Project()
        p.imposition = self._settings(2, fold_trim=1.5 * MM)
        p.imposition.trim_size = None
        slot = imp.build_plan(8, p.imposition).sheets[0].slots[0]
        self.assertAlmostEqual(p.page_size().height,
                               slot.trim_rect[3] - slot.trim_rect[1])


class PaperSizeTests(unittest.TestCase):
    """A template is a construction method: the paper decides the rest.

    The complaint these cover: a layout drawn for one paper size left part of
    the sheet unprinted when another size was selected.
    """

    PAPERS = ("Letter", "A4", "A3", "Legal", "A5")

    def _plan(self, key: str, sheet: str):
        p = Project()
        p.apply_document_preset(key, sheet)
        return p, imp.build_plan(24, p.imposition)

    def test_no_template_carries_a_paper_size(self):
        for pre in DOCUMENT_PRESETS:
            self.assertFalse(
                any(f in pre.__dict__ for f in ("sheet", "landscape")),
                f"{pre.key} still names a paper")

    def test_every_construction_fills_every_paper(self):
        for pre in DOCUMENT_PRESETS:
            for name in self.PAPERS:
                p, plan = self._plan(pre.key, name)
                waste = p.imposition.sheet_margin
                cells = [sl.cell_rect for sp in plan.sheets for sl in sp.slots]
                sheet = plan.sheet_size
                for got, want in (
                        (min(c[0] for c in cells), waste),
                        (min(c[1] for c in cells), waste),
                        (sheet.width - max(c[2] for c in cells), waste),
                        (sheet.height - max(c[3] for c in cells), waste)):
                    self.assertAlmostEqual(
                        got, want, places=4,
                        msg=f"{pre.key} on {name} leaves the sheet unprinted")

    def test_the_page_size_the_document_reports_is_the_one_imposed(self):
        for pre in DOCUMENT_PRESETS:
            for name in self.PAPERS:
                p, plan = self._plan(pre.key, name)
                self.assertAlmostEqual(p.page_size().width,
                                       plan.page_size.width, places=4,
                                       msg=f"{pre.key} on {name}")
                self.assertAlmostEqual(p.page_size().height,
                                       plan.page_size.height, places=4,
                                       msg=f"{pre.key} on {name}")

    def test_the_sheet_is_fed_the_way_the_construction_needs(self):
        # one fold wants the sheet long edge first, two folds short edge
        # first, whatever the paper
        for name in self.PAPERS:
            _, folio = self._plan("saddle-folio", name)
            _, quarto = self._plan("saddle-quarto", name)
            self.assertGreater(folio.sheet_size.width, folio.sheet_size.height,
                               name)
            self.assertGreater(quarto.sheet_size.height, quarto.sheet_size.width,
                               name)

    def test_a_single_leaf_keeps_the_paper_portrait(self):
        for name in self.PAPERS:
            _, plan = self._plan("side-stapled", name)
            self.assertGreater(plan.sheet_size.height, plan.sheet_size.width,
                               name)

    def test_the_rendered_sheet_is_the_paper_that_was_chosen(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            photo = make_photo(d / "p.jpg")
            for key in ("mini8", "stab", "saddle-folio"):
                for name in ("Letter", "A4"):
                    p = Project()
                    p.apply_document_preset(key, name)
                    p.add_file(photo)
                    plan = p.build_plan()
                    want = paper(name)
                    self.assertAlmostEqual(
                        min(plan.sheet_size.width, plan.sheet_size.height),
                        min(want.width, want.height), places=1,
                        msg=f"{key} on {name}")
                    out = Renderer(p).render_print(d / f"{key}-{name}.pdf", plan)
                    doc = fitz.open(out[0])
                    self.assertAlmostEqual(doc[0].rect.width,
                                           plan.sheet_size.width, places=1)
                    doc.close()

    def test_changing_the_paper_carries_the_margins_with_it(self):
        p = Project()
        p.apply_document_preset("sewn-folio", "Letter")
        before = (p.imposition.margin_inner / p.page_size().width,
                  p.imposition.margin_top / p.page_size().width)
        p.set_sheet_size("A3")
        after = (p.imposition.margin_inner / p.page_size().width,
                 p.imposition.margin_top / p.page_size().width)
        for a, b in zip(before, after):
            self.assertAlmostEqual(a, b, places=3)
        self.assertGreater(p.imposition.margin_inner, 14 * MM)

    def test_margins_set_by_hand_survive_a_change_of_paper(self):
        p = Project()
        p.apply_document_preset("sewn-folio", "Letter")
        p.imposition.margin_inner = 21 * MM
        p.set_sheet_size("A4")
        self.assertAlmostEqual(p.imposition.margin_inner, 21 * MM)

    def test_a_saved_document_keeps_the_feed_it_was_saved_with(self):
        with tempfile.TemporaryDirectory() as d:
            p = Project()
            p.apply_document_preset("saddle-folio", "Letter")
            p.imposition.orientation = "portrait"
            path = p.save(Path(d) / "o.sigzine")
            q = Project.load(path)
        self.assertEqual(q.imposition.orientation, "portrait")
        self.assertGreater(q.imposition.effective_sheet().height,
                           q.imposition.effective_sheet().width)

    def test_documents_saved_before_this_change_are_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sigzine"
            p = Project()
            p.apply_document_preset("saddle-quarto", "Letter")
            data = p.to_dict()
            data["preset_key"] = "quarter-letter"
            data["imposition"].pop("orientation")
            data["imposition"]["sheet_landscape"] = True
            path.write_text(json.dumps(data))
            q = Project.load(path)
        self.assertEqual(q.imposition.orientation, "landscape")
        self.assertEqual(q.preset_key, "saddle-quarto")


class DuplexRegistrationTests(unittest.TestCase):
    """The back of the sheet has to land on top of the front.

    Model: the printer displaces everything on the back page by (dx, dy) in
    that page's own coordinates. The sheet is held up to the light with the
    front toward the reader, so the back shows through at its physical
    position, and the reader reports where it falls on a scale centred on the
    sheet with right and down positive.
    """

    def _reading(self, dx: float, dy: float, axis: str):
        if axis == "vertical":      # turned left to right: x mirrors
            return (-dx, dy)
        return (dx, -dy)            # turned top to bottom: y mirrors

    def test_correction_cancels_the_displacement(self):
        for axis in ("vertical", "horizontal"):
            for dx, dy in ((0.8 * MM, -0.4 * MM), (-1.5 * MM, 2.0 * MM),
                           (0.0, 0.9 * MM)):
                rx, ry = self._reading(dx, dy, axis)
                cx, cy = imp.duplex_correction(rx, ry, axis)
                self.assertAlmostEqual(cx + dx, 0.0, places=9, msg=axis)
                self.assertAlmostEqual(cy + dy, 0.0, places=9, msg=axis)

    def test_a_perfect_printer_needs_no_correction(self):
        for axis in ("vertical", "horizontal"):
            self.assertEqual(imp.duplex_correction(0.0, 0.0, axis), (0.0, 0.0))

    def test_shift_moves_slots_marks_and_folds_together(self):
        s = imp.ImpositionSettings(binding_key="saddle")
        plan = imp.build_plan(8, s)
        back = next(sp for sp in plan.sheets if sp.side == "back")
        moved = imp.shift_sheet(back, 3.0, -2.0)
        for a, b in zip(back.slots, moved.slots):
            self.assertAlmostEqual(b.trim_rect[0] - a.trim_rect[0], 3.0)
            self.assertAlmostEqual(b.content_rect[1] - a.content_rect[1], -2.0)
            self.assertEqual(a.page, b.page)
        for a, b in zip(back.fold_lines, moved.fold_lines):
            self.assertAlmostEqual(b[0] - a[0], 3.0)

    def test_renderer_moves_only_the_backs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Project()
            for i in range(3):
                p.add_file(make_photo(Path(d) / f"r{i}.jpg"))
            prof = cal.PrinterProfile(name="offset rig")
            prof.duplex_offset_x_mm = 1.5
            prof.duplex_offset_y_mm = -0.5
            p.profile = prof
            r = Renderer(p)
            plan = p.build_plan()
            dx, dy = r.back_offset(plan)
            self.assertNotEqual((dx, dy), (0.0, 0.0))
            front = next(sp for sp in plan.sheets if sp.side == "front")
            back = next(sp for sp in plan.sheets if sp.side == "back")
            self.assertIs(r._registered(front, plan), front)
            shifted = r._registered(back, plan)
            self.assertAlmostEqual(
                shifted.slots[0].trim_rect[0] - back.slots[0].trim_rect[0], dx)

    def test_no_profile_means_no_shift(self):
        p = Project()
        r = Renderer(p)
        plan = imp.build_plan(4, p.imposition)
        self.assertEqual(r.back_offset(plan), (0.0, 0.0))


class RegistrationSolverTests(unittest.TestCase):
    """Three stations are enough to tell an offset from a skew."""

    def _sheet(self):
        from sigzine.core.units import paper as _paper
        return _paper("Letter")

    def test_stations_are_level_and_symmetric(self):
        sheet = self._sheet()
        st = testsheet.station_geometry(sheet)
        self.assertAlmostEqual(st["A"][1], st["C"][1], places=6)
        self.assertAlmostEqual((st["A"][0] + st["C"][0]) / 2, sheet.width / 2,
                               places=6)
        self.assertAlmostEqual(st["B"][0], sheet.width / 2, places=6)
        self.assertAlmostEqual(st["B"][1], sheet.height / 2, places=6)

    def test_centre_station_sees_only_the_offset(self):
        sheet = self._sheet()
        st = testsheet.station_geometry(sheet)
        r = cal.predict_reading(st["B"], sheet, dx=0.4 * MM, dy=-0.9 * MM,
                                theta=math.radians(0.5), sx=1.01, sy=0.99)
        self.assertAlmostEqual(r[0], -0.4 * MM, places=6)
        self.assertAlmostEqual(r[1], -0.9 * MM, places=6)

    def test_solver_recovers_offset_skew_and_scale(self):
        sheet = self._sheet()
        st = testsheet.station_geometry(sheet)
        for truth in (dict(dx=0.35 * MM, dy=-0.8 * MM,
                           theta=math.radians(0.12), sx=1.001, sy=0.999),
                      dict(dx=-1.2 * MM, dy=0.5 * MM,
                           theta=math.radians(-0.4), sx=0.998, sy=1.002),
                      dict(dx=0.0, dy=0.0, theta=0.0, sx=1.0, sy=1.0)):
            readings = {k: cal.predict_reading(v, sheet, **truth)
                        for k, v in st.items()}
            got = cal.solve_registration(readings, st, sheet)
            self.assertAlmostEqual(got["dx"], truth["dx"], places=6)
            self.assertAlmostEqual(got["dy"], truth["dy"], places=6)
            self.assertAlmostEqual(got["theta"], truth["theta"], places=9)
            self.assertAlmostEqual(got["scale_x"], truth["sx"], places=9)
            self.assertAlmostEqual(got["scale_y"], truth["sy"], places=9)

    def test_pure_skew_is_not_mistaken_for_an_offset(self):
        """One corner touching and the rest drifting is rotation, not shift."""
        sheet = self._sheet()
        st = testsheet.station_geometry(sheet)
        readings = {k: cal.predict_reading(v, sheet, theta=math.radians(0.3))
                    for k, v in st.items()}
        got = cal.solve_registration(readings, st, sheet)
        self.assertAlmostEqual(got["dx"], 0.0, places=6)
        self.assertAlmostEqual(got["dy"], 0.0, places=6)
        self.assertGreater(abs(got["theta_deg"]), 0.25)
        self.assertGreater(got["skew_at_corner"], 0.5 * MM)

    def test_description_calls_out_a_skew(self):
        sheet = self._sheet()
        st = testsheet.station_geometry(sheet)
        readings = {k: cal.predict_reading(v, sheet, dy=1.0 * MM,
                                           theta=math.radians(0.4))
                    for k, v in st.items()}
        text = " ".join(cal.describe_registration(
            cal.solve_registration(readings, st, sheet), MM))
        self.assertIn("Skew", text)
        self.assertIn("Offset", text)


class PrintOptionTests(unittest.TestCase):
    """What gets handed to lpr.

    A driverless queue forwards an option CUPS does not recognise to the
    printer as an IPP job attribute, and a printer that does not support
    the attribute rejects the whole job: accepted, sent, then dropped with
    nothing printed. So an option that is not universal has to be asked
    about before it is used.
    """

    # what `lpoptions -l` prints for a driverless queue, and for one with a
    # traditional PPD behind it
    DRIVERLESS = ("print-color-mode/Print Color Mode: *color monochrome\n"
                  "print-scaling/print scaling: auto auto-fit fill fit *none\n"
                  "sides/2-Sided Printing: *one-sided two-sided-long-edge\n")
    PPD = ("PageSize/Media Size: *Letter Legal A4\n"
           "Duplex/Two-Sided: *None DuplexNoTumble DuplexTumble\n"
           "Resolution/Output Resolution: 300dpi *600dpi\n")

    def setUp(self):
        printing._OPTION_CACHE.clear()
        self.addCleanup(printing._OPTION_CACHE.clear)

    def _with_lpoptions(self, text, code=0):
        def fake_run(cmd, timeout=6.0):
            if cmd and cmd[0] == "lpoptions":
                return (code, text, "")
            return (0, "", "")
        return mock.patch.object(printing, "_run", fake_run)

    def _opts(self, text, **kw):
        printing._OPTION_CACHE.clear()
        with self._with_lpoptions(text):
            return printing.build_options(printer="q", **kw)

    def test_standard_options_always_go(self):
        for text in (self.DRIVERLESS, self.PPD, ""):
            opts = self._opts(text, duplex="long edge", media="Letter")
            self.assertIn("sides=two-sided-long-edge", opts)
            self.assertIn("media=Letter", opts)

    def test_scaling_is_only_claimed_when_the_queue_knows_the_option(self):
        advertised = self._opts(self.DRIVERLESS)
        self.assertIn("print-scaling=none", advertised)
        silent = self._opts(self.PPD)
        self.assertFalse([o for o in silent if o.startswith("print-scaling")])
        self.assertFalse([o for o in silent if o.startswith("fit-to-page")])

    def test_an_unknown_queue_gets_nothing_exotic(self):
        """lpoptions failing, or missing entirely, must not mean guessing."""
        for code in (1, 127):
            printing._OPTION_CACHE.clear()
            with self._with_lpoptions("", code=code):
                opts = printing.build_options(printer="q")
            self.assertEqual(
                [o for o in opts if "scaling" in o or "fit" in o], [])

    def test_fit_to_page_is_used_where_that_is_the_spelling(self):
        opts = self._opts("fit-to-page/Fit to Page: *False True\n")
        self.assertIn("fit-to-page=false", opts)

    def test_only_one_scaling_option_is_sent(self):
        both = self._opts(self.DRIVERLESS + "fit-to-page/Fit: *False True\n")
        names = [o.split("=")[0] for o in both if "=" in o]
        self.assertEqual(names.count("print-scaling") + names.count("fit-to-page"), 1)

    def test_collate_is_left_off_a_single_copy(self):
        self.assertNotIn("collate=true", self._opts(self.PPD, copies=1))
        self.assertIn("collate=true", self._opts(self.PPD, copies=3))

    def test_options_are_read_once_per_queue(self):
        calls = []

        def fake_run(cmd, timeout=6.0):
            calls.append(list(cmd))
            return (0, self.DRIVERLESS, "")

        with mock.patch.object(printing, "_run", fake_run):
            printing.build_options(printer="q")
            printing.build_options(printer="q")
            printing.build_options(printer="other")
        self.assertEqual(len(calls), 2)

    def test_a_failure_says_what_was_actually_run(self):
        with mock.patch.object(printing, "_run",
                               lambda cmd, timeout=6.0: (1, "", "no such queue")), \
                mock.patch.object(printing, "available", lambda: True), \
                tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "x.pdf"
            pdf.write_bytes(b"%PDF-1.7\n")
            ok, msg = printing.print_pdf(pdf, printer="q")
        self.assertFalse(ok)
        self.assertIn("no such queue", msg)
        self.assertIn("lpr", msg)

    MONO_ONLY = ("print-color-mode/Print Color Mode: *monochrome\n"
                 "sides/2-Sided Printing: *one-sided\n")

    def test_colour_is_asked_for_with_the_ipp_spelling(self):
        self.assertIn("print-color-mode=color",
                      self._opts(self.DRIVERLESS, color=True))
        self.assertIn("print-color-mode=monochrome",
                      self._opts(self.DRIVERLESS, color=False))

    def test_colour_is_asked_for_with_the_ppd_spelling(self):
        ppd = self.PPD + "ColorModel/Color Model: *Gray RGB\n"
        self.assertIn("ColorModel=RGB", self._opts(ppd, color=True))
        self.assertIn("ColorModel=Gray", self._opts(ppd, color=False))

    def test_the_drivers_own_value_is_used_verbatim(self):
        """KGray is the driver's word for it, and the only one it will take."""
        ppd = self.PPD + "ColorModel/Color Model: *KGray CMYK\n"
        self.assertIn("ColorModel=CMYK", self._opts(ppd, color=True))
        self.assertIn("ColorModel=KGray", self._opts(ppd, color=False))

    def test_nothing_is_sent_when_the_mode_is_not_on_offer(self):
        """A mono-only queue has no colour value to accept."""
        opts = self._opts(self.MONO_ONLY, color=True)
        self.assertEqual([o for o in opts if "color" in o.lower()], [])
        self.assertIn("print-color-mode=monochrome",
                      self._opts(self.MONO_ONLY, color=False))

    def test_a_queue_with_no_colour_option_is_left_alone(self):
        opts = self._opts(self.PPD, color=True)
        self.assertEqual([o for o in opts if "olor" in o], [])

    def test_no_preference_sends_no_colour_option(self):
        for text in (self.DRIVERLESS, self.PPD, ""):
            opts = self._opts(text, color=None)
            self.assertEqual([o for o in opts
                              if "color" in o.lower() or "ColorModel" in o], [])

    def test_supports_color_reports_what_the_queue_says(self):
        for text, expected in ((self.DRIVERLESS, True),
                               (self.MONO_ONLY, False),
                               (self.PPD, None), ("", None)):
            printing._OPTION_CACHE.clear()
            with self._with_lpoptions(text):
                self.assertIs(printing.supports_color("q"), expected)

    def test_describe_quotes_a_path_with_spaces(self):
        line = printing.describe(["lpr", "-T", "my zine", "/tmp/a b.pdf"])
        self.assertIn("'my zine'", line)
        self.assertIn("'/tmp/a b.pdf'", line)


class ScaleBarTests(unittest.TestCase):
    def test_bars_are_whole_centimetres_and_ticked_at_both_ends(self):
        """A bar of 176 mm cannot carry a tick every 10 mm at both ends."""
        from sigzine.core.units import paper as _paper
        for name in ("Letter", "A4", "Legal"):
            sheet = _paper(name)
            W, m = sheet.width, 14 * MM
            bar_w = int((W - 2 * m - 30 * MM) / (10 * MM)) * 10 * MM
            self.assertAlmostEqual(bar_w % (10 * MM), 0.0, places=6, msg=name)
            self.assertGreater(bar_w, 80 * MM, name)
            self.assertLess(bar_w, W - 2 * m, name)


class ToneTests(unittest.TestCase):
    def test_every_preset_is_monotone_and_reaches_both_ends(self):
        for name in T.PRESETS:
            s = T.apply_preset(T.ToneSettings(), name)
            lut = T.combined_lut(s)
            self.assertTrue(np.all(np.diff(lut) >= -1e-6), name)
            self.assertAlmostEqual(float(lut[-1]), 1.0, places=2, msg=name)

    def test_ink_limits_are_respected(self):
        s = T.ToneSettings(min_dot=0.05, max_ink=0.8, dot_gain=0.0,
                           use_profile=False)
        lut = T.combined_lut(s)
        self.assertAlmostEqual(1.0 - float(lut[0]), 0.8, places=2)
        self.assertGreater(1.0 - float(lut[250]), 0.01)

    def test_dot_gain_compensation_cancels_the_gain(self):
        s = T.ToneSettings(dot_gain=0.2, min_dot=0.0, max_ink=1.0,
                           use_profile=False)
        lut = T.combined_lut(s)
        sent_ink = 1.0 - lut
        printed = np.clip(sent_ink + 0.2 * np.sin(np.pi * sent_ink), 0, 1)
        wanted = 1.0 - np.linspace(0, 1, len(lut))
        self.assertLess(float(np.max(np.abs(printed - wanted))), 0.02)

    def test_pchip_is_monotone_through_awkward_points(self):
        pts = [(0, 0), (0.2, 0.05), (0.21, 0.9), (1, 1)]
        y = T.pchip(pts, np.linspace(0, 1, 512))
        self.assertTrue(np.all(np.diff(y) >= -1e-9))

    def test_every_screen_produces_a_bitmap(self):
        img = testsheet.synthetic_photo(200, 150).convert("RGB")
        for screen in T.SCREENS:
            s = T.ToneSettings(screen=screen)
            out = T.apply_tone(img, s)
            self.assertEqual(out.size, (200, 150), screen)
            if screen != "none":
                self.assertEqual(out.mode, "1", screen)

    def test_simulation_undoes_the_correction(self):
        img = testsheet.synthetic_photo(300, 220).convert("RGB")
        s = T.ToneSettings(dot_gain=0.18, contrast=0.0, gamma=1.0,
                           sharpen=0.0, clarity=0.0)
        sent = T.apply_tone(img, s)
        shown = T.simulate_print(sent, s)
        grey = np.asarray(T.to_gray(img), dtype=float)
        got = np.asarray(shown, dtype=float)
        self.assertLess(float(np.mean(np.abs(grey - got))), 12.0)


class ColourToneTests(unittest.TestCase):
    """Colour has to survive the pipeline, and grey has to stay grey."""

    def test_the_default_is_still_one_channel(self):
        out = T.apply_tone(colour_photo(), T.ToneSettings())
        self.assertEqual(out.mode, "L")

    def test_colour_keeps_three_channels_and_the_hues(self):
        img = colour_photo()
        out = T.apply_tone(img, T.ToneSettings(color=True))
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.size, img.size)
        arr = np.asarray(out, dtype=float)
        spread = arr.max(axis=2) - arr.min(axis=2)
        self.assertGreater(float(spread.mean()), 20.0)
        # the warm left stays warmer than the cool right
        left, right = arr[-20:, :40], arr[-20:, -40:]
        self.assertGreater(float(left[..., 0].mean() - left[..., 2].mean()), 10)
        self.assertGreater(float(right[..., 2].mean() - right[..., 0].mean()), 10)

    def test_a_grey_original_stays_neutral_in_colour_mode(self):
        img = testsheet.synthetic_photo(120, 90).convert("RGB")
        arr = np.asarray(T.apply_tone(img, T.ToneSettings(color=True)),
                         dtype=float)
        self.assertLess(float(np.max(arr.max(axis=2) - arr.min(axis=2))), 1.5)

    def test_a_neutral_image_gets_the_same_tone_as_in_mono(self):
        img = testsheet.synthetic_photo(120, 90).convert("RGB")
        s = T.ToneSettings(contrast=0.3, gamma=1.2, dot_gain=0.18,
                           min_dot=0.03, max_ink=0.9)
        mono = np.asarray(T.apply_tone(img, s), dtype=float)
        col = np.asarray(T.apply_tone(img, T.ToneSettings(**{
            **{f: getattr(s, f) for f in ("contrast", "gamma", "dot_gain",
                                          "min_dot", "max_ink")},
            "color": True})), dtype=float)[..., 0]
        self.assertLess(float(np.mean(np.abs(mono - col))), 1.0)

    def test_tone_switched_off_still_hands_back_colour(self):
        out = T.apply_tone(colour_photo(), T.ToneSettings(color=True,
                                                         enabled=False))
        self.assertEqual(out.mode, "RGB")

    def test_transparency_lands_on_paper_white(self):
        img = Image.new("RGBA", (8, 8), (255, 0, 0, 0))
        arr = np.asarray(T.apply_tone(img, T.ToneSettings(
            color=True, enabled=False)), dtype=int)
        self.assertTrue(np.all(arr == 255))

    def test_every_screen_separates_each_channel(self):
        img = colour_photo(160, 120)
        for screen in T.SCREENS:
            s = T.ToneSettings(color=True, screen=screen)
            out = T.apply_tone(img, s)
            self.assertEqual(out.mode, "RGB", screen)
            self.assertEqual(out.size, img.size, screen)
            if screen != "none":
                values = set(np.unique(np.asarray(out)).tolist())
                self.assertTrue(values <= {0, 255}, f"{screen}: {values}")

    def test_the_channel_screens_are_not_laid_on_top_of_each_other(self):
        """One angle for all three separations is how you get a moire."""
        flat = Image.new("RGB", (96, 96), (128, 128, 128))
        s = T.ToneSettings(color=True, screen="halftone dot", screen_lpi=40,
                           enabled=False)
        arr = np.asarray(T.screen_image(flat, s), dtype=int)
        self.assertFalse(np.array_equal(arr[..., 0], arr[..., 1]))
        self.assertFalse(np.array_equal(arr[..., 1], arr[..., 2]))

    def test_auto_levels_does_not_neutralise_a_cast(self):
        """Per channel stretching would; reading the luminance once does not."""
        base = np.asarray(testsheet.synthetic_photo(120, 90).convert("L"),
                          dtype=np.float32) / 255.0
        blue = np.stack([base * 0.45, base * 0.6, base], axis=2) * 255
        img = Image.fromarray(blue.astype(np.uint8), "RGB")
        s = T.ToneSettings(color=True, auto_levels=True, dot_gain=0.0,
                           min_dot=0.0, max_ink=1.0, use_profile=False)
        arr = np.asarray(T.apply_tone(img, s), dtype=float)
        self.assertGreater(float(arr[..., 2].mean() - arr[..., 0].mean()), 20.0)

    def test_simulation_keeps_the_colour_it_was_given(self):
        s = T.ToneSettings(color=True, dot_gain=0.18, sharpen=0.0,
                           clarity=0.0, contrast=0.0, gamma=1.0)
        sent = T.apply_tone(colour_photo(), s)
        shown = T.simulate_print(sent, s)
        self.assertEqual(shown.mode, "RGB")
        arr = np.asarray(shown, dtype=float)
        self.assertGreater(float((arr.max(axis=2) - arr.min(axis=2)).mean()), 15)

    def test_colour_survives_a_save_and_reload(self):
        p = Project()
        p.tone.color = True
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.sigzine"
            p.save(path)
            self.assertTrue(Project.load(path).tone.color)


class CalibrationTests(unittest.TestCase):
    def _simulated(self, gain: float = 0.18) -> cal.PrinterProfile:
        nom = np.linspace(0, 1, 21)
        cov = np.clip(nom + gain * np.sin(np.pi * nom), 0, 1)
        R = 1 - cov * 0.96
        srgb = np.where(R <= 0.0031308, R * 12.92,
                        1.055 * R ** (1 / 2.4) - 0.055)
        return cal.PrinterProfile(name="sim", measure_kind="scan grey 0-255",
                                  nominal=nom.tolist(),
                                  measured=(srgb * 255).tolist())

    def test_recovers_the_dot_gain(self):
        p = self._simulated(0.18)
        self.assertAlmostEqual(p.analyse()["gain_50"], 0.18, places=2)

    def test_linearisation_closes_the_loop(self):
        p = self._simulated(0.22)
        lin = p.linearisation_lut(256)
        printed = np.clip(lin + 0.22 * np.sin(np.pi * lin), 0, 1)
        wanted = np.linspace(0, 1, 256)
        self.assertLess(float(np.max(np.abs(printed - wanted))), 0.02)

    def test_noisy_measurements_still_give_a_monotone_curve(self):
        p = self._simulated(0.15)
        rng = np.random.default_rng(3)
        p.measured = [float(np.clip(m + rng.normal(0, 6), 0, 255))
                      for m in p.measured]
        resp = p.response_lut(256)
        self.assertTrue(np.all(np.diff(resp) >= -1e-9))

    def test_visual_calibration_is_usable(self):
        p = cal.from_visual_reading("eye", 0.04, 0.92, 0.36)
        self.assertTrue(p.has_linearisation())
        self.assertGreater(p.analyse()["gain_50"], 0.05)

    def test_profiles_round_trip(self):
        p = self._simulated()
        with tempfile.TemporaryDirectory() as d:
            path = p.save(Path(d))
            q = cal.PrinterProfile.load(path)
        self.assertEqual(q.nominal, p.nominal)
        self.assertEqual(q.name, p.name)


def press(ink, gain: float = 0.18):
    """A pretend printer: requested ink in, coverage actually laid down out."""
    x = np.asarray(ink, dtype=float)
    return np.clip(x + gain * np.sin(np.pi * x), 0.0, 1.0)


def read_patch(coverage, dmax: float = 0.96, noise: float = 0.0, rng=None):
    """What a scanner reports for that coverage, as 0-255 grey."""
    R = 1.0 - np.asarray(coverage, dtype=float) * dmax
    srgb = np.where(R <= 0.0031308, R * 12.92, 1.055 * R ** (1 / 2.4) - 0.055)
    v = srgb * 255.0
    if noise:
        v = v + (rng or np.random.default_rng(0)).normal(0, noise, size=v.shape)
    return np.clip(v, 0, 255)


def target_levels():
    """The requested values the real linearisation sheet prints.

    A main ramp plus a highlight and a shadow wedge, which is why several
    levels appear twice.
    """
    coarse = [round(i * 0.02, 4) for i in range(51)]
    high = [round(i * 0.005, 4) for i in range(21)]
    low = [round(0.90 + i * 0.005, 4) for i in range(21)]
    return coarse + high + low


class LinearisationBuilderTests(unittest.TestCase):
    GAIN = 0.18

    def _measure(self, levels=None, gain=None, noise=0.0, seed=1):
        levels = target_levels() if levels is None else list(levels)
        gain = self.GAIN if gain is None else gain
        rng = np.random.default_rng(seed)
        vals = read_patch(press(levels, gain), noise=noise, rng=rng)
        return levels, [float(v) for v in vals]

    def _truth(self, gain=None, size=256):
        return press(np.linspace(0, 1, size), self.GAIN if gain is None else gain)

    # -- pooling ----------------------------------------------------------
    def test_repeated_levels_are_pooled_not_overwritten(self):
        """The old path threaded a curve through raw points and the pchip
        de-duplication kept whichever repeat came last, throwing the rest
        away. Every patch should count."""
        pooled = lin.pool_readings([0.1, 0.1, 0.1, 0.5],
                                   [100.0, 120.0, 140.0, 60.0])
        self.assertEqual(pooled.nominal.tolist(), [0.1, 0.5])
        self.assertAlmostEqual(float(pooled.value[0]), 120.0)
        self.assertEqual(pooled.count.tolist(), [3, 1])
        self.assertAlmostEqual(float(pooled.spread[0]), 20.0)
        self.assertEqual(pooled.raw_patches, 4)

    def test_every_patch_reaches_the_fit(self):
        levels, vals = self._measure()
        p, report = lin.build_profile(levels, vals, "scan grey 0-255")
        self.assertEqual(report.patches, len(levels))
        self.assertLess(report.levels, len(levels))   # repeats were pooled
        self.assertGreater(report.repeats, 0)

    def test_a_bad_repeat_is_outvoted_by_its_siblings(self):
        levels = [0.0, 0.25, 0.5, 0.5, 0.5, 0.75, 1.0]
        good = read_patch(press(levels, self.GAIN))
        vals = [float(v) for v in good]
        vals[4] = 20.0                                # one ruined patch
        pooled = lin.pool_readings(levels, vals)
        i = int(np.argmin(np.abs(pooled.nominal - 0.5)))
        self.assertAlmostEqual(float(pooled.value[i]),
                               float(good[2]), places=6)

    # -- the fit itself ---------------------------------------------------
    def test_fit_tracks_the_real_response(self):
        levels, vals = self._measure()
        p, report = lin.build_profile(levels, vals, "scan grey 0-255")
        err = np.abs(p.response_lut(256) - self._truth())
        self.assertLess(float(np.max(err)), 0.015)
        self.assertEqual(report.status, "ok")
        self.assertTrue(report.trustworthy())

    def test_fit_beats_raw_interpolation_on_noisy_readings(self):
        levels, vals = self._measure(noise=6.0, seed=5)
        fitted, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        raw = cal.PrinterProfile(name="raw", measure_kind="scan grey 0-255",
                                 nominal=levels, measured=vals)
        truth = self._truth()
        fit_err = float(np.max(np.abs(fitted.response_lut(256) - truth)))
        raw_err = float(np.max(np.abs(raw.response_lut(256) - truth)))
        self.assertLess(fit_err, raw_err)
        self.assertLess(fit_err, 0.03)

    def test_fitted_curve_is_smooth_where_the_raw_one_is_not(self):
        """A wobbly correction prints as banding, so the curve the profile
        hands to tone.py has to be smoother than the readings behind it."""
        levels, vals = self._measure(noise=6.0, seed=11)
        fitted, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        raw = cal.PrinterProfile(name="raw", measure_kind="scan grey 0-255",
                                 nominal=levels, measured=vals)
        wobble = lambda p: float(np.sum(np.abs(np.diff(p.response_lut(256), 2))))
        self.assertLess(wobble(fitted), wobble(raw) / 3.0)

    def test_fit_stays_monotone_through_heavy_noise(self):
        levels, vals = self._measure(noise=14.0, seed=7)
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        self.assertTrue(np.all(np.diff(p.response_lut(512)) >= -1e-9))
        self.assertTrue(np.all(np.diff(p.linearisation_lut(512)) >= -1e-9))

    def test_a_single_ruined_patch_is_dropped(self):
        levels, vals = self._measure()
        spoil = levels.index(0.5)
        vals[spoil] = 250.0                           # a crease, or dirt
        p, report = lin.build_profile(levels, vals, "scan grey 0-255")
        self.assertGreaterEqual(report.outliers, 1)
        at_half = float(np.interp(0.5, np.linspace(0, 1, 256),
                                 p.response_lut(256)))
        self.assertLess(abs(at_half - float(press(0.5, self.GAIN))), 0.02)

    def test_isotonic_is_the_nearest_rising_sequence(self):
        got = lin.isotonic([0.0, 0.3, 0.2, 0.9])
        self.assertTrue(np.all(np.diff(got) >= -1e-12))
        self.assertAlmostEqual(float(got[1]), 0.25)
        self.assertAlmostEqual(float(got[2]), 0.25)
        unchanged = lin.isotonic([0.1, 0.2, 0.3])
        self.assertTrue(np.allclose(unchanged, [0.1, 0.2, 0.3]))

    def test_sparse_readings_still_build_something_usable(self):
        levels = [0.0, 0.1, 0.4, 0.8, 1.0]
        _, vals = self._measure(levels=levels)
        p, report = lin.build_profile(levels, vals, "scan grey 0-255")
        resp = p.response_lut(256)
        self.assertTrue(np.all(np.diff(resp) >= -1e-9))
        self.assertAlmostEqual(float(resp[0]), 0.0, places=2)
        self.assertAlmostEqual(float(resp[-1]), 1.0, places=2)
        self.assertFalse(report.trustworthy())        # five patches is thin

    def test_the_correction_linearises_the_simulated_press(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        want = np.linspace(0, 1, 128)
        printed = press(p.linearisation_lut(128), self.GAIN)
        self.assertLess(float(np.max(np.abs(printed - want))), 0.02)

    def test_fitted_profile_round_trips(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255", name="fitted")
        with tempfile.TemporaryDirectory() as d:
            q = cal.PrinterProfile.load(p.save(Path(d)))
        self.assertTrue(q.is_fitted())
        self.assertEqual(q.passes, 1)
        self.assertTrue(np.allclose(q.response_lut(256), p.response_lut(256)))

    def test_older_profiles_without_a_fit_still_work(self):
        levels, vals = self._measure(levels=np.linspace(0, 1, 21).tolist())
        old = cal.PrinterProfile(name="old", measure_kind="scan grey 0-255",
                                 nominal=levels, measured=vals)
        self.assertTrue(old.has_linearisation())
        self.assertFalse(old.is_fitted())
        self.assertAlmostEqual(old.analyse()["gain_50"], self.GAIN, places=2)

    # -- verification -----------------------------------------------------
    def _verify_against(self, profile, gain, wanted=None, noise=0.0, seed=3):
        wanted = (np.linspace(0, 1, 21).tolist() if wanted is None
                  else list(wanted))
        grid = np.linspace(0, 1, 1024)
        sent = np.interp(wanted, grid, profile.linearisation_lut(1024))
        vals = read_patch(press(sent, gain), noise=noise,
                          rng=np.random.default_rng(seed))
        return lin.verify(profile, wanted, [float(v) for v in vals],
                          "scan grey 0-255")

    def test_a_good_profile_verifies_clean(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        report = self._verify_against(p, self.GAIN)
        self.assertTrue(report.passed)
        self.assertLess(report.error_max, 0.02)

    def test_verification_catches_a_printer_that_moved(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        report = self._verify_against(p, 0.32)        # new toner, say
        self.assertFalse(report.passed)
        self.assertGreater(report.error_max, 0.05)
        self.assertTrue(any("outside" in s for s in report.summary()))

    def test_verification_ignores_the_two_pinned_ends(self):
        """Paper and solid define the scale, so they cannot disagree with
        it and must not be counted as a success."""
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        report = self._verify_against(p, 0.32)
        self.assertGreater(report.worst_at, 0.0)
        self.assertLess(report.worst_at, 1.0)

    # -- refinement -------------------------------------------------------
    def test_refining_shrinks_the_error(self):
        """One pass is a guess made from a noisy coarse read; the second is
        measured through the correction and should converge."""
        coarse = np.linspace(0, 1, 9).tolist()
        levels, vals = self._measure(levels=coarse, noise=5.0, seed=2)
        first, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        before = self._verify_against(first, self.GAIN)

        wanted = np.linspace(0, 1, 21).tolist()
        grid = np.linspace(0, 1, 1024)
        sent = np.interp(wanted, grid, first.linearisation_lut(1024))
        check = read_patch(press(sent, self.GAIN))
        second, report = lin.refine(first, wanted,
                                    [float(v) for v in check],
                                    "scan grey 0-255")
        after = self._verify_against(second, self.GAIN)

        self.assertEqual(second.passes, 2)
        self.assertLess(after.error_max, before.error_max)
        self.assertLess(after.error_max, 0.02)
        self.assertEqual(report.status, "ok")

    def test_refining_a_good_profile_does_no_harm(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        wanted = np.linspace(0, 1, 21).tolist()
        grid = np.linspace(0, 1, 1024)
        sent = np.interp(wanted, grid, p.linearisation_lut(1024))
        check = read_patch(press(sent, self.GAIN))
        q, _ = lin.refine(p, wanted, [float(v) for v in check],
                          "scan grey 0-255")
        after = self._verify_against(q, self.GAIN)
        self.assertTrue(after.passed)
        self.assertLess(after.error_max, 0.02)

    # -- the sheet, end to end --------------------------------------------
    def test_verification_sheet_round_trips_through_a_scan(self):
        levels, vals = self._measure()
        p, _ = lin.build_profile(levels, vals, "scan grey 0-255")
        with tempfile.TemporaryDirectory() as d:
            pdf, smap = testsheet.verification_sheet(Path(d) / "check.pdf", p)
            self.assertTrue(testsheet.map_path_for(pdf).exists())
            reloaded = testsheet.SheetMap.load(testsheet.map_path_for(pdf))
            self.assertEqual(reloaded.kind, "verification")
            self.assertEqual(len(reloaded.patches), len(smap.patches))

            pm = fitz.open(pdf)[0].get_pixmap(dpi=150)
            img = Image.frombytes("RGB", (pm.width, pm.height),
                                  pm.samples).convert("L")
            sent = np.clip(1.0 - np.asarray(img, dtype=np.float32) / 255.0,
                           0, 1)
            shown = read_patch(press(sent, self.GAIN))
            sim = Image.fromarray(shown.astype(np.uint8))
            result = scanmod.measure_sheet(sim, reloaded)

        self.assertGreater(len(result.values), 20)
        report = lin.verify(p, result.nominal, result.values,
                            "scan grey 0-255")
        self.assertTrue(report.passed, "; ".join(report.summary()))


class ScanTests(unittest.TestCase):
    def test_reads_a_simulated_scan_back(self):
        with tempfile.TemporaryDirectory() as d:
            pdf, smap = testsheet.linearisation_sheet(Path(d) / "lin.pdf")
            pm = fitz.open(pdf)[0].get_pixmap(dpi=150)
            img = Image.frombytes("RGB", (pm.width, pm.height),
                                  pm.samples).convert("L")
            g = np.asarray(img, dtype=np.float32) / 255.0
            ink = np.clip(1 - g, 0, 1)
            ink = np.clip(ink + 0.18 * np.sin(np.pi * ink), 0, 1)
            R = 1 - ink * 0.96
            srgb = np.where(R <= 0.0031308, R * 12.92,
                            1.055 * R ** (1 / 2.4) - 0.055)
            arr = np.clip(srgb * 255, 0, 255).astype(np.uint8)
            sim = Image.fromarray(arr).rotate(-0.6, expand=True, fillcolor=255,
                                              resample=Image.BICUBIC)
            canvas = Image.new("L", (sim.width + 120, sim.height + 120), 50)
            canvas.paste(sim, (60, 60))
            result = scanmod.measure_sheet(canvas, smap)
            self.assertGreater(len(result.values), 80)
            p = cal.PrinterProfile(name="scanned",
                                   measure_kind="scan grey 0-255",
                                   nominal=result.nominal,
                                   measured=result.values)
            self.assertAlmostEqual(p.analyse()["gain_50"], 0.18, places=1)


class RenderTests(unittest.TestCase):
    def test_place_geometry_fit_fill_and_rotation(self):
        r, crop = place_geometry(Size(100, 200), (0, 0, 50, 50), "fit", 0)
        self.assertIsNone(crop)
        self.assertAlmostEqual(r[3] - r[1], 50)
        r, crop = place_geometry(Size(100, 200), (0, 0, 50, 50), "fill", 0)
        self.assertIsNotNone(crop)
        self.assertAlmostEqual(r[2] - r[0], 50)
        r, crop = place_geometry(Size(100, 200), (0, 0, 50, 50), "fit", 90)
        self.assertAlmostEqual(r[2] - r[0], 50)

    def test_full_export_for_each_binding(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            photos = [make_photo(d / f"p{i}.jpg") for i in range(3)]
            for key in ("saddle", "sewn", "perfect", "stab", "mini8",
                        "accordion", "coptic"):
                p = Project()
                p.name = key
                p.set_binding(key)
                for ph in photos:
                    p.add_file(ph)
                plan = p.build_plan()
                r = Renderer(p)
                out = r.render_print(d / f"{key}.pdf", plan)
                self.assertTrue(out and out[0].exists(), key)
                doc = fitz.open(out[0])
                self.assertEqual(doc.page_count, len(plan.sheets), key)
                self.assertAlmostEqual(doc[0].rect.width,
                                       plan.sheet_size.width, places=1)
                doc.close()
                inst = templates.instruction_sheet(d / f"{key}-i.pdf", p, plan)
                self.assertTrue(inst.exists())

    def test_digital_copy_page_counts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = Project()
            for i in range(5):
                p.add_file(make_photo(d / f"q{i}.jpg"))
            p.apply_padding()
            r = Renderer(p)
            p.digital.layout = "single"
            p.digital.include_blanks = False
            out = r.render_digital(d / "dig.pdf")
            doc = fitz.open(out)
            self.assertEqual(doc.page_count, 5)
            doc.close()
            p.digital.layout = "spreads"
            out = r.render_digital(d / "dig2.pdf")
            doc = fitz.open(out)
            self.assertEqual(doc.page_count, 3)   # cover + two spreads
            self.assertAlmostEqual(doc[0].rect.width,
                                   p.page_size().width * 2, places=1)
            doc.close()

    def test_colour_pages_reach_the_pdf_in_colour(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            photo = make_colour_photo(d / "c.jpg")

            def saturation(colour: bool) -> float:
                p = Project()
                p.tone.color = colour
                p.tone.screen = "none"
                for _ in range(2):
                    p.add_file(photo)
                plan = p.build_plan()
                out = Renderer(p).render_print(
                    d / f"{'colour' if colour else 'mono'}.pdf", plan)
                doc = fitz.open(out[0])
                pm = doc[0].get_pixmap(dpi=60, alpha=False)
                arr = np.asarray(Image.frombytes(
                    "RGB", (pm.width, pm.height), pm.samples), dtype=float)
                doc.close()
                return float((arr.max(axis=2) - arr.min(axis=2)).mean())

            self.assertLess(saturation(False), 1.0)
            self.assertGreater(saturation(True), 10.0)

    def test_vector_sources_stay_vector(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = typeset("# Hello\n\nSome words that stay as text.",
                          Size(300, 500), (20, 20, 20, 20), TextStyle())
            src.save(d / "t.pdf")
            p = Project()
            p.add_file(d / "t.pdf")
            r = Renderer(p)
            out = r.render_print(d / "out.pdf", p.build_plan())
            doc = fitz.open(out[0])
            self.assertIn("Hello", doc[0].get_text())
            doc.close()


class ProjectTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = Project()
            p.name = "Round trip"
            p.add_file(make_photo(d / "a.jpg"))
            p.add_text("# Title\n\nBody text.", "Words")
            p.set_binding("coptic")
            p.tone.contrast = 0.42
            p.imposition.margin_inner = 17 * MM
            path = p.save(d / "proj.sigzine")
            q = Project.load(path)
        self.assertEqual(q.name, "Round trip")
        self.assertEqual(q.imposition.binding_key, "coptic")
        self.assertAlmostEqual(q.tone.contrast, 0.42)
        self.assertAlmostEqual(q.imposition.margin_inner, 17 * MM)
        self.assertEqual(len(q.pages), len(p.pages))

    def test_blank_padding_appears_in_the_page_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = Project()
            for i in range(3):
                p.add_file(make_photo(Path(d) / f"b{i}.jpg"))
            added = p.apply_padding()
            self.assertEqual(added, 1)
            self.assertEqual(len(p.pages), 4)
            self.assertTrue(p.pages[-1].locked_blank)
            # re-padding does not accumulate blanks
            p.apply_padding()
            self.assertEqual(len(p.pages), 4)

    def test_empty_document_is_not_padded(self):
        p = Project()
        self.assertEqual(p.apply_padding(), 0)
        self.assertEqual(len(p.pages), 0)

    def test_signature_planning_balances_the_last_section(self):
        p = Project()
        p.set_binding("sewn")
        p.imposition.sheets_per_signature = 4
        sigs = imp.plan_signatures(33, p.imposition)
        self.assertEqual(sum(sigs) * 4, imp.padded_page_count(33, p.imposition))
        self.assertGreaterEqual(min(sigs), 2)


class TemplateTests(unittest.TestCase):
    def test_hole_positions_are_inside_the_spine(self):
        for key in bindings.keys():
            spine = 210 * MM
            holes = templates.hole_positions(key, spine)
            self.assertGreaterEqual(len(holes), 2, key)
            self.assertTrue(all(0 < h < spine for h in holes), key)
            self.assertEqual(holes, sorted(holes), key)

    def test_templates_render(self):
        with tempfile.TemporaryDirectory() as d:
            for key in ("pamphlet", "stab", "coptic", "screwpost"):
                out = templates.sewing_template(Path(d) / f"{key}.pdf", key,
                                                Size(140 * MM, 200 * MM))
                self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
