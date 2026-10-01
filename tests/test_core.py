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
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz

from sigzine.core import binding as bindings
from sigzine.core import calibration as cal
from sigzine.core import imposition as imp
from sigzine.core import scan as scanmod
from sigzine.core import templates, testsheet
from sigzine.core import tone as T
from sigzine.core.project import Project
from sigzine.core.render import Renderer, place_geometry
from sigzine.core.typeset import TextStyle, typeset
from sigzine.core.units import MM, Size, paper


def make_photo(path: Path, w: int = 400, h: int = 300) -> Path:
    testsheet.synthetic_photo(w, h).convert("RGB").save(path, quality=88)
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
