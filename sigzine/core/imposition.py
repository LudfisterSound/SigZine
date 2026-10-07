"""Imposition: turning a list of pages into printable sheets.

The folded layouts are not looked up from a table - they are derived by
simulating the physical act of folding a sheet, which means any number of
folds is supported and the folding instructions always match the output.

Coordinates are PyMuPDF page coordinates: origin top-left, y grows downward.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import binding as bindings
from .units import MM, Size

Rect = Tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# Folding simulation
# ---------------------------------------------------------------------------

@dataclass
class Leaf:
    col: int
    row: int
    up: str      # 'F' or 'B' - which side of the sheet currently faces up
    rot: int     # 0 or 180 - rotation of that face as seen by the reader


def fold_sequence(folds: int) -> List[str]:
    """Alternating folds that finish with a vertical (spine) fold."""
    return ["v" if (folds - 1 - i) % 2 == 0 else "h" for i in range(folds)]


def fold_grid(folds: int) -> Tuple[int, int]:
    """Cells across and down on one side of the sheet for ``folds`` folds."""
    return (2 ** math.ceil(folds / 2), 2 ** (folds // 2))


def simulate_folding(folds: int) -> List[Tuple[str, int, int, int]]:
    """Return, for page 1..2*cols*rows, a tuple (side, col, row, rotation).

    ``side`` is 'F' or 'B'; col/row are cells on the front of the sheet.
    Folding convention: the top half always folds down and behind, the left
    half always folds right and behind, so the spine ends up on the left and
    page 1 finishes face up on top of the pile.
    """
    cols, rows = fold_grid(folds)
    grid: Dict[Tuple[int, int], List[Leaf]] = {
        (c, r): [Leaf(c, r, "F", 0)] for c in range(cols) for r in range(rows)
    }
    C, R = cols, rows
    for axis in fold_sequence(folds):
        new: Dict[Tuple[int, int], List[Leaf]] = {}
        if axis == "v":
            half = C // 2
            for c in range(half, C):
                for r in range(R):
                    staying = list(grid[(c, r)])
                    moving = grid[(C - 1 - c, r)]
                    flipped = [
                        Leaf(l.col, l.row, "B" if l.up == "F" else "F", l.rot)
                        for l in reversed(moving)
                    ]
                    new[(c - half, r)] = staying + flipped
            C -= half
        else:
            half = R // 2
            for c in range(C):
                for r in range(half, R):
                    staying = list(grid[(c, r)])
                    moving = grid[(c, R - 1 - r)]
                    flipped = [
                        Leaf(l.col, l.row, "B" if l.up == "F" else "F",
                             (l.rot + 180) % 360)
                        for l in reversed(moving)
                    ]
                    new[(c, r - half)] = staying + flipped
            R -= half
        grid = new

    pile = grid[(0, 0)]
    out: List[Tuple[str, int, int, int]] = []
    for leaf in pile:
        other = "B" if leaf.up == "F" else "F"
        out.append((leaf.up, leaf.col, leaf.row, leaf.rot))
        out.append((other, leaf.col, leaf.row, leaf.rot))
    return out


def fold_instructions(folds: int) -> List[str]:
    steps = []
    for axis in fold_sequence(folds):
        if axis == "v":
            steps.append("Fold the left half to the right, behind the sheet "
                         "(printed side stays facing you).")
        else:
            steps.append("Fold the top half down, behind the sheet.")
    steps.append("The last fold is the spine; the earlier folds are trimmed open.")
    return steps


def cut_fold_boundaries(folds: int) -> Tuple[List[int], List[int]]:
    """Internal grid boundaries that are folds trimmed open, not the spine.

    Returned as (column boundaries, row boundaries), numbered 1..n-1 so that
    boundary ``b`` sits between cell ``b - 1`` and cell ``b``.

    The last fold is always vertical and is the spine, so it is the finest
    vertical fold and creases at the odd column boundaries.  Every other
    crease - the coarser vertical folds and all of the horizontal ones - ends
    up as a folded edge that has to be cut open, which is where the trim
    allowance has to go.
    """
    cols, rows = fold_grid(folds)
    cut_cols = [c for c in range(1, cols) if c % 2 == 0]
    cut_rows = list(range(1, rows))
    return cut_cols, cut_rows


# ---------------------------------------------------------------------------
# Settings and plan objects
# ---------------------------------------------------------------------------

@dataclass
class ImpositionSettings:
    binding_key: str = "saddle"
    sheet_size: Size = Size(612.0, 792.0)
    orientation: str = "auto"           # 'auto' | 'landscape' | 'portrait'
    sheet_landscape: bool = True        # the manual choice, used when
                                        # ``orientation`` is not 'auto'
    trim_size: Optional[Size] = None    # None = the whole cell
    folds_per_sheet: int = 1
    sheets_per_signature: int = 4
    up: int = 2                         # flat layouts: leaves across the sheet
    panels_per_sheet: int = 4           # accordion
    margin_top: float = 6 * MM
    margin_bottom: float = 6 * MM
    margin_inner: float = 12 * MM
    margin_outer: float = 8 * MM
    gutter_extra: float = 0.0
    creep_per_sheet: float = 0.1 * MM
    creep_enabled: bool = True
    bleed: float = 0.0
    sheet_margin: float = 0.0           # unprintable / trim waste at the edges
    fold_trim: float = 1.5 * MM         # waste each page gives up where a fold
                                        # is cut open (see cut_fold_boundaries)
    back_flip: str = "auto"             # 'auto' | 'long' | 'short' (driver)
    balance_last_signature: bool = True
    crop_marks: bool = True
    fold_marks: bool = True
    registration_marks: bool = True
    collation_marks: bool = True
    sheet_slugs: bool = True            # sheet/side captions in the trim waste
    page_numbers_in_margin: bool = False
    single_sided: bool = False

    def effective_sheet(self) -> Size:
        """The sheet as it goes through the press, the right way round.

        With ``orientation`` on 'auto' the way the paper is fed is worked out
        from the construction and the paper actually selected, so a layout is
        never tied to one paper size.
        """
        s = self.sheet_size
        if self.orientation == "auto":
            return s.landscape() if auto_landscape(s, self) else s.portrait()
        return s.landscape() if self.landscape_feed() else s.portrait()

    def landscape_feed(self) -> bool:
        if self.orientation == "landscape":
            return True
        if self.orientation == "portrait":
            return False
        return bool(self.sheet_landscape)


@dataclass
class Slot:
    page: Optional[int]                 # 1-based page number, None = blank
    cell: Tuple[int, int]
    cell_rect: Rect
    trim_rect: Rect                     # where the trimmed page sits
    content_rect: Rect                  # trim inset by margins (safe area)
    rotation: int
    spine: str = "none"                 # 'left'|'right' in page-local terms
    creep: float = 0.0


@dataclass
class SheetPlan:
    number: int
    side: str                           # 'front' | 'back'
    signature: int
    sheet_in_signature: int
    sheets_in_signature: int
    slots: List[Slot] = field(default_factory=list)
    fold_lines: List[Tuple[float, float, float, float, str]] = field(default_factory=list)
    cut_lines: List[Tuple[float, float, float, float, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return (f"Sig {self.signature} / sheet {self.sheet_in_signature}"
                f" of {self.sheets_in_signature} - {self.side}")


@dataclass
class Plan:
    sheets: List[SheetPlan]
    sheet_size: Size
    page_size: Size
    source_pages: int
    total_pages: int
    signatures: List[int]               # sheets in each signature
    pages_per_signature: List[int]
    family: str
    folds: int
    notes: List[str] = field(default_factory=list)

    @property
    def sheet_count(self) -> int:
        sides = {}
        for s in self.sheets:
            sides.setdefault((s.signature, s.sheet_in_signature), 0)
        return len(sides)

    @property
    def blanks_added(self) -> int:
        return max(0, self.total_pages - self.source_pages)


# ---------------------------------------------------------------------------
# Signature planning
# ---------------------------------------------------------------------------

def plan_signatures(page_count: int, settings: ImpositionSettings) -> List[int]:
    """Return the number of sheets in each signature."""
    b = bindings.get(settings.binding_key)
    if b.family == bindings.MINI8:
        return [max(1, math.ceil(page_count / 8))]
    if b.family == bindings.ACCORDION:
        return [1]
    if b.family == bindings.FUKUROTOJI:
        return [max(1, math.ceil(page_count / 2))]
    if b.family == bindings.FLAT:
        per_sheet = settings.up * (1 if settings.single_sided else 2)
        return [max(1, math.ceil(page_count / per_sheet))]

    pages_per_sheet = 2 ** settings.folds_per_sheet * 2
    if not b.signatures:
        sheets = max(1, math.ceil(page_count / pages_per_sheet))
        return [sheets]

    per_sig = max(1, settings.sheets_per_signature)
    pages_per_sig = pages_per_sheet * per_sig
    count = max(1, math.ceil(page_count / pages_per_sig))
    sigs = [per_sig] * count
    if settings.balance_last_signature:
        used = (count - 1) * pages_per_sig
        remaining = page_count - used
        if remaining > 0:
            need = max(1, math.ceil(remaining / pages_per_sheet))
            sigs[-1] = need
        # even out a very thin last signature by borrowing from the previous one
        if len(sigs) > 1 and sigs[-1] == 1 and sigs[-2] > 2:
            sigs[-2] -= 1
            sigs[-1] += 1
    return sigs


def padded_page_count(page_count: int, settings: ImpositionSettings) -> int:
    b = bindings.get(settings.binding_key)
    if b.family == bindings.MINI8:
        return max(8, int(math.ceil(page_count / 8) * 8))
    if b.family == bindings.ACCORDION:
        per = settings.panels_per_sheet * (1 if settings.single_sided else 2)
        return max(per, int(math.ceil(page_count / per) * per))
    if b.family == bindings.FUKUROTOJI:
        return int(math.ceil(page_count / 2) * 2)
    if b.family == bindings.FLAT:
        per_sheet = settings.up * (1 if settings.single_sided else 2)
        return int(math.ceil(page_count / per_sheet) * per_sheet)
    sigs = plan_signatures(page_count, settings)
    pages_per_sheet = 2 ** settings.folds_per_sheet * 2
    return sum(s * pages_per_sheet for s in sigs)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

# The page shape every construction aims at: the ISO proportion, which is
# what a folded sheet of any paper tends toward anyway.
TARGET_PAGE_ASPECT = 1.0 / math.sqrt(2.0)


def grid_shape(settings: "ImpositionSettings",
               folds: Optional[int] = None) -> Tuple[int, int]:
    """Cells across and down on one side of the sheet.

    This depends only on how the thing is built - the binding, the number of
    folds, how many leaves sit across a flat sheet - never on which paper is
    loaded.
    """
    family = bindings.get(settings.binding_key).family
    if family == bindings.FOLDED:
        return fold_grid(max(1, settings.folds_per_sheet
                            if folds is None else folds))
    if family == bindings.MINI8:
        return (4, 2)
    if family == bindings.FUKUROTOJI:
        return (2, 1)
    if family == bindings.ACCORDION:
        return (max(2, settings.panels_per_sheet), 1)
    up = max(1, settings.up)
    return (up, 1) if up <= 2 else (2, up // 2)


def auto_landscape(sheet: Size, settings: "ImpositionSettings") -> bool:
    """Whether to feed ``sheet`` long edge first, for this construction.

    The grid of cells is fixed by the construction, so the only thing the
    paper decides is which way round it has to go in to make those cells a
    sensible page shape.  Whichever way gets closest to the target proportion
    wins, which is why one fold wants a landscape sheet and two folds want a
    portrait one, whatever the paper.
    """
    cols, rows = grid_shape(settings)
    best, best_landscape = None, True
    for landscape in (True, False):
        s = sheet.landscape() if landscape else sheet.portrait()
        w, h = s.width / cols, s.height / rows
        if w <= 0 or h <= 0:
            continue
        # distance in proportion, measured so that twice as wide and twice as
        # tall are equally wrong
        d = abs(math.log((w / h) / TARGET_PAGE_ASPECT))
        if best is None or d < best - 1e-9:
            best, best_landscape = d, landscape
    return best_landscape


def imposition_area(sheet: Size, settings: "ImpositionSettings") -> Rect:
    m = max(0.0, settings.sheet_margin)
    m = min(m, min(sheet.width, sheet.height) / 4.0)
    return (m, m, sheet.width - m, sheet.height - m)


def _cell_rect(area: Rect, cols: int, rows: int, c: int, r: int) -> Rect:
    w = (area[2] - area[0]) / cols
    h = (area[3] - area[1]) / rows
    return (area[0] + c * w, area[1] + r * h,
            area[0] + (c + 1) * w, area[1] + (r + 1) * h)


def _tracks(lo: float, hi: float, n: int, cuts: Sequence[int],
            gap: float) -> Tuple[List[float], float]:
    """Cell starts and the cell size, leaving ``gap`` of waste at each cut."""
    cell = (hi - lo - gap * len(cuts)) / n
    starts: List[float] = []
    x = lo
    for i in range(n):
        if i in cuts:
            x += gap
        starts.append(x)
        x += cell
    return starts, cell


@dataclass(frozen=True)
class CellGrid:
    """Where each page cell sits on the sheet, waste allowances included.

    Pages are all the same size: the waste strips at the cut-open folds come
    out of the imposition area before it is divided, not out of individual
    cells, so neighbouring pages do not end up different widths.
    """
    cols: int
    rows: int
    xs: Tuple[float, ...]
    ys: Tuple[float, ...]
    cell_width: float
    cell_height: float
    gap: float = 0.0
    cut_cols: Tuple[int, ...] = ()
    cut_rows: Tuple[int, ...] = ()

    def rect(self, c: int, r: int) -> Rect:
        return (self.xs[c], self.ys[r],
                self.xs[c] + self.cell_width, self.ys[r] + self.cell_height)

    def cell_size(self) -> Size:
        return Size(self.cell_width, self.cell_height)

    def boundary_x(self, b: int) -> float:
        """The crease or cut line at column boundary ``b``."""
        return self.xs[b] - (self.gap / 2.0 if b in self.cut_cols else 0.0)

    def boundary_y(self, b: int) -> float:
        return self.ys[b] - (self.gap / 2.0 if b in self.cut_rows else 0.0)


def page_grid(sheet: Size, settings: "ImpositionSettings",
              folds: Optional[int] = None) -> CellGrid:
    """Where the pages sit on ``sheet``, for any construction.

    Every layout divides the whole imposition area, so the pages fill the
    paper that is actually loaded rather than the paper the layout was first
    drawn for.  Only folded work takes trim waste out first, at the folds
    that get cut open.
    """
    if bindings.get(settings.binding_key).family == bindings.FOLDED:
        return folded_grid(sheet, settings, folds)
    cols, rows = grid_shape(settings, folds)
    area = imposition_area(sheet, settings)
    xs, cw = _tracks(area[0], area[2], cols, (), 0.0)
    ys, ch = _tracks(area[1], area[3], rows, (), 0.0)
    return CellGrid(cols, rows, tuple(xs), tuple(ys), cw, ch, 0.0, (), ())


def folded_grid(sheet: Size, settings: "ImpositionSettings",
                folds: Optional[int] = None) -> CellGrid:
    """The cell grid for a folded signature.

    Every fold other than the spine is cut open after folding, and the blade
    takes paper with it, so each of the two pages that meet at such a fold
    gives up ``fold_trim`` of waste.  Without that allowance the cut lands on
    the page edges themselves: there is nothing to trim off, and anything that
    was meant to bleed is lost.
    """
    folds = max(1, settings.folds_per_sheet if folds is None else folds)
    cols, rows = fold_grid(folds)
    area = imposition_area(sheet, settings)
    cut_cols, cut_rows = cut_fold_boundaries(folds)
    gap = max(0.0, settings.fold_trim) * 2.0
    # never eat more than a third of a cell, however the trim is set
    if cut_cols:
        gap = min(gap, (area[2] - area[0]) / (cols * 3.0))
    if cut_rows:
        gap = min(gap, (area[3] - area[1]) / (rows * 3.0))
    xs, cw = _tracks(area[0], area[2], cols, cut_cols, gap)
    ys, ch = _tracks(area[1], area[3], rows, cut_rows, gap)
    return CellGrid(cols, rows, tuple(xs), tuple(ys), cw, ch, gap,
                    tuple(cut_cols), tuple(cut_rows))


def _centered(cell: Rect, size: Size) -> Rect:
    cx = (cell[0] + cell[2]) / 2.0
    cy = (cell[1] + cell[3]) / 2.0
    return (cx - size.width / 2.0, cy - size.height / 2.0,
            cx + size.width / 2.0, cy + size.height / 2.0)


def _page_size_for(cell: Rect, settings: ImpositionSettings, rotation: int) -> Size:
    cw, ch = cell[2] - cell[0], cell[3] - cell[1]
    if settings.trim_size is None:
        return Size(cw, ch)
    t = settings.trim_size
    if rotation in (90, 270):
        t = t.rotated()
    return Size(min(t.width, cw), min(t.height, ch))


def _content_rect(trim: Rect, settings: ImpositionSettings, rotation: int,
                  spine: str) -> Rect:
    """Inset the trim box by the margins, expressed in page-local terms."""
    inner = settings.margin_inner + settings.gutter_extra
    outer = settings.margin_outer
    top, bottom = settings.margin_top, settings.margin_bottom
    left, right = (inner, outer) if spine == "left" else (outer, inner)
    if spine == "none":
        left = right = (inner + outer) / 2.0
    # rotate the margin quad into sheet space
    for _ in range((rotation // 90) % 4):
        left, top, right, bottom = bottom, left, top, right
    return (trim[0] + left, trim[1] + top, trim[2] - right, trim[3] - bottom)


def _shift(rect: Rect, dx: float, dy: float) -> Rect:
    return (rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy)


def _creep_delta(rotation: int, spine: str, amount: float) -> Tuple[float, float]:
    """Move page content toward its spine by ``amount`` (page-local -x/+x)."""
    if amount == 0 or spine == "none":
        return (0.0, 0.0)
    local = -amount if spine == "left" else amount
    rad = math.radians(rotation % 360)
    dx = local * math.cos(rad)
    dy = local * math.sin(rad)
    return (dx, dy)


def _mirror_col(cols: int, c: int) -> int:
    return cols - 1 - c


# ---------------------------------------------------------------------------
# The imposer
# ---------------------------------------------------------------------------

def build_plan(page_count: int, settings: ImpositionSettings) -> Plan:
    b = bindings.get(settings.binding_key)
    total = padded_page_count(page_count, settings)
    sheet = settings.effective_sheet()
    notes: List[str] = []

    if b.family == bindings.FOLDED:
        plan = _impose_folded(total, settings, notes)
    elif b.family == bindings.FLAT:
        plan = _impose_flat(total, settings, notes)
    elif b.family == bindings.MINI8:
        plan = _impose_mini8(total, settings, notes)
    elif b.family == bindings.FUKUROTOJI:
        plan = _impose_fukurotoji(total, settings, notes)
    elif b.family == bindings.ACCORDION:
        plan = _impose_accordion(total, settings, notes)
    else:
        raise ValueError(f"unknown imposition family {b.family}")

    if settings.trim_size is not None:
        cell = page_grid(sheet, settings).cell_size()
        slack_w = cell.width - settings.trim_size.width
        slack_h = cell.height - settings.trim_size.height
        if max(slack_w, slack_h) > 0.5:
            notes.append(
                f"The finished page is fixed at "
                f"{settings.trim_size.describe()}, which is smaller than the "
                f"{cell.describe()} this paper gives: "
                f"{max(0.0, slack_w) / MM:.0f} x {max(0.0, slack_h) / MM:.0f} "
                f"mm of each cell is left unprinted. Set the finished page "
                f"back to automatic to fill the sheet.")
        if min(slack_w, slack_h) < -0.5:
            notes.append(
                "The finished page is larger than this paper can hold, so it "
                "has been cut down to fit.")

    plan.source_pages = page_count
    if plan.blanks_added:
        notes.insert(0, f"{plan.blanks_added} blank page(s) added to complete "
                        f"the {b.name.lower()} layout.")
    plan.notes = notes
    plan.sheet_size = sheet
    return plan


def duplex_axis(settings: ImpositionSettings, sheet: Size) -> str:
    """Which way the sheet physically turns over: 'vertical' or 'horizontal'.

    'Flip on the long edge' means the sheet rotates about the axis parallel
    to its long edge, so what that does depends on the sheet orientation.
    'auto' always asks for the left-right turn a folded booklet needs.
    """
    if settings.back_flip == "auto":
        return "vertical"
    portrait = sheet.height >= sheet.width
    if settings.back_flip == "long":
        return "vertical" if portrait else "horizontal"
    return "horizontal" if portrait else "vertical"


def duplex_driver_setting(settings: ImpositionSettings, sheet: Size) -> str:
    """What the user should choose in the print dialog."""
    axis = duplex_axis(settings, sheet)
    portrait = sheet.height >= sheet.width
    if axis == "vertical":
        return "long edge" if portrait else "short edge"
    return "short edge" if portrait else "long edge"


def _apply_back_flip(cols: int, rows: int, c: int, r: int, rot: int,
                     settings: ImpositionSettings, sheet: Size) -> Tuple[int, int, int]:
    """Position on the back side, honouring the duplex flip axis.

    The simulation gives back-side cells as seen when the sheet is turned
    over about its vertical axis; turning it about the horizontal axis is
    the same layout rotated 180 degrees.
    """
    c = _mirror_col(cols, c)
    if duplex_axis(settings, sheet) == "horizontal":
        c = cols - 1 - c
        r = rows - 1 - r
        rot = (rot + 180) % 360
    return c, r, rot


def _make_slot(page: Optional[int], cols: int, rows: int, c: int, r: int,
               rot: int, settings: ImpositionSettings, sheet: Size,
               creep: float = 0.0, spine: str = "none",
               grid: Optional[CellGrid] = None) -> Slot:
    cell = (grid.rect(c, r) if grid is not None
            else _cell_rect(imposition_area(sheet, settings), cols, rows, c, r))
    size = _page_size_for(cell, settings, rot)
    trim = _centered(cell, size)
    dx, dy = _creep_delta(rot, spine, creep)
    trim = _shift(trim, dx, dy)
    content = _content_rect(trim, settings, rot, spine)
    return Slot(page=page, cell=(c, r), cell_rect=cell, trim_rect=trim,
                content_rect=content, rotation=rot, spine=spine, creep=creep)


def _spine_for(page: Optional[int]) -> str:
    if page is None:
        return "none"
    return "left" if page % 2 == 1 else "right"


def _grid_fold_lines(grid: CellGrid, area: Rect
                     ) -> List[Tuple[float, float, float, float, str]]:
    """Every internal grid boundary, as a crease to fold on."""
    lines = [(grid.boundary_x(c), area[1], grid.boundary_x(c), area[3], "fold")
             for c in range(1, grid.cols)]
    lines += [(area[0], grid.boundary_y(r), area[2], grid.boundary_y(r),
               "fold") for r in range(1, grid.rows)]
    return lines


def _folded_marks(grid: CellGrid, area: Rect
                  ) -> Tuple[List[Tuple[float, float, float, float, str]],
                             List[Tuple[float, float, float, float, str]]]:
    """Creases to fold on, and the cut-open folds to trim, kept apart."""
    folds: List[Tuple[float, float, float, float, str]] = []
    cuts: List[Tuple[float, float, float, float, str]] = []
    for c in range(1, grid.cols):
        x = grid.boundary_x(c)
        (cuts if c in grid.cut_cols else folds).append(
            (x, area[1], x, area[3], "cut" if c in grid.cut_cols else "fold"))
    for r in range(1, grid.rows):
        y = grid.boundary_y(r)
        (cuts if r in grid.cut_rows else folds).append(
            (area[0], y, area[2], y, "cut" if r in grid.cut_rows else "fold"))
    return folds, cuts


def _impose_folded(total: int, settings: ImpositionSettings,
                   notes: List[str]) -> Plan:
    folds = max(1, settings.folds_per_sheet)
    sheet = settings.effective_sheet()
    grid = page_grid(sheet, settings, folds)
    cols, rows = grid.cols, grid.rows
    layout = simulate_folding(folds)          # page (1-based) -> (side,c,r,rot)
    pages_per_sheet = len(layout)
    half = pages_per_sheet // 2

    sig_sheets = plan_signatures(total, settings)
    # re-plan against the padded count so the maths is exact
    sig_sheets = plan_signatures(total, settings)
    sheets: List[SheetPlan] = []
    page_cursor = 0
    sheet_no = 0
    pages_per_sig: List[int] = []

    for sig_index, k in enumerate(sig_sheets, start=1):
        sig_pages = pages_per_sheet * k
        pages_per_sig.append(sig_pages)
        base = page_cursor
        for j in range(1, k + 1):
            sheet_no += 1
            creep = settings.creep_per_sheet * (j - 1) if settings.creep_enabled else 0.0
            front_slots: List[Slot] = []
            back_slots: List[Slot] = []
            for i, (side, c, r, rot) in enumerate(layout, start=1):
                # map the single-sheet page number onto the nested quire
                if i <= half:
                    local = (j - 1) * half + i
                else:
                    local = sig_pages - (pages_per_sheet - i) - (j - 1) * half
                page = base + local
                page_num: Optional[int] = page if page <= total else None
                spine = _spine_for(page_num)
                if side == "F":
                    front_slots.append(_make_slot(page_num, cols, rows, c, r, rot,
                                                  settings, sheet, creep, spine,
                                                  grid))
                else:
                    bc, br, brot = _apply_back_flip(cols, rows, c, r, rot, settings, sheet)
                    back_slots.append(_make_slot(page_num, cols, rows, bc, br, brot,
                                                 settings, sheet, creep, spine,
                                                 grid))
            fl, cl = _folded_marks(grid, imposition_area(sheet, settings))
            if not settings.fold_marks:
                fl = []
            sheets.append(SheetPlan(sheet_no, "front", sig_index, j, k,
                                    front_slots, list(fl), list(cl)))
            if not settings.single_sided:
                sheets.append(SheetPlan(sheet_no, "back", sig_index, j, k,
                                        back_slots, list(fl), list(cl)))
        page_cursor += sig_pages

    page_size = grid.cell_size()
    if settings.trim_size is not None:
        page_size = settings.trim_size
    if grid.gap > 0:
        notes.append(f"Cut-open folds carry {grid.gap / 2 / MM:.1f} mm of trim "
                     f"waste on each side; cut along the dashed lines.")
    if settings.creep_enabled and max(sig_sheets) > 1:
        worst = settings.creep_per_sheet * (max(sig_sheets) - 1)
        notes.append(f"Creep compensation: innermost sheet shifted "
                     f"{worst / MM:.1f} mm toward the spine.")
    return Plan(sheets=sheets, sheet_size=sheet, page_size=page_size,
                source_pages=total, total_pages=page_cursor,
                signatures=sig_sheets, pages_per_signature=pages_per_sig,
                family=bindings.FOLDED, folds=folds)


def _impose_flat(total: int, settings: ImpositionSettings,
                 notes: List[str]) -> Plan:
    up = max(1, settings.up)
    sheet = settings.effective_sheet()
    grid = page_grid(sheet, settings)
    cols, rows = grid.cols, grid.rows
    single = settings.single_sided
    per_sheet = up * (1 if single else 2)
    n_sheets = int(math.ceil(total / per_sheet))
    sheets: List[SheetPlan] = []

    if up == 1:
        for k in range(1, n_sheets + 1):
            f = (k - 1) * (1 if single else 2) + 1
            fs = [_make_slot(f if f <= total else None, 1, 1, 0, 0, 0, settings,
                             sheet, 0.0, _spine_for(f), grid)]
            sheets.append(SheetPlan(k, "front", 1, k, n_sheets, fs))
            if not single:
                bp = f + 1
                bs = [_make_slot(bp if bp <= total else None, 1, 1, 0, 0, 0,
                                 settings, sheet, 0.0, _spine_for(bp), grid)]
                sheets.append(SheetPlan(k, "back", 1, k, n_sheets, bs))
        notes.append("1-up: no cutting, each sheet is one leaf.")
    else:
        # cut-and-stack: the left column is the first half of the book
        half = total // 2
        for k in range(1, n_sheets + 1):
            a = 2 * k - 1                 # left column, front
            b = half + a                  # right column, front
            front = []
            back = []
            pairs_front = [(0, a), (1, b)]
            for c, p in pairs_front:
                pp = p if p <= total else None
                front.append(_make_slot(pp, cols, rows, c, 0, 0, settings, sheet,
                                        0.0, _spine_for(pp), grid))
            pairs_back = [(1, a + 1), (0, b + 1)]
            for c, p in pairs_back:
                cc, rr, rot = _apply_back_flip(cols, rows, cols - 1 - c, 0, 0,
                                               settings, sheet)
                pp = p if p <= total else None
                back.append(_make_slot(pp, cols, rows, cc, rr, rot, settings,
                                       sheet, 0.0, _spine_for(pp), grid))
            x_cut = grid.boundary_x(1)
            cut = [(x_cut, 0, x_cut, sheet.height, "cut")]
            sheets.append(SheetPlan(k, "front", 1, k, n_sheets, front, [], cut))
            if not single:
                sheets.append(SheetPlan(k, "back", 1, k, n_sheets, back, [], list(cut)))
        notes.append("Cut-and-stack 2-up: guillotine down the centre, then put "
                     "the right-hand pile underneath the left-hand pile.")

    page_size = settings.trim_size or grid.cell_size()
    return Plan(sheets=sheets, sheet_size=sheet, page_size=page_size,
                source_pages=total, total_pages=total, signatures=[n_sheets],
                pages_per_signature=[total], family=bindings.FLAT, folds=0)


MINI8_LAYOUT = [
    # (page, col, row, rotation) on a 4x2 landscape sheet, printed one side
    (5, 0, 0, 180), (4, 1, 0, 180), (3, 2, 0, 180), (2, 3, 0, 180),
    (6, 0, 1, 0), (7, 1, 1, 0), (8, 2, 1, 0), (1, 3, 1, 0),
]


def _impose_mini8(total: int, settings: ImpositionSettings,
                  notes: List[str]) -> Plan:
    sheet = settings.effective_sheet()
    grid = page_grid(sheet, settings)
    cols, rows = grid.cols, grid.rows
    a = imposition_area(sheet, settings)
    n_sheets = max(1, int(math.ceil(total / 8)))
    sheets: List[SheetPlan] = []
    for k in range(1, n_sheets + 1):
        base = (k - 1) * 8
        slots = []
        for (p, c, r, rot) in MINI8_LAYOUT:
            page = base + p
            pg = page if page <= total else None
            slots.append(_make_slot(pg, cols, rows, c, r, rot, settings, sheet,
                                    0.0, _spine_for(pg), grid))
        y_cut = grid.boundary_y(1)
        cut = [(grid.boundary_x(1), y_cut, grid.boundary_x(3), y_cut, "cut")]
        fl = _grid_fold_lines(grid, a) if settings.fold_marks else []
        sheets.append(SheetPlan(k, "front", 1, k, n_sheets, slots, fl, cut))
    notes.append("Cut only the marked centre slit; every other line is a fold.")
    if n_sheets > 1:
        notes.append(f"{n_sheets} sheets: each one folds into its own "
                     f"eight-page booklet.")
    return Plan(sheets=sheets, sheet_size=sheet,
                page_size=settings.trim_size or grid.cell_size(),
                source_pages=total, total_pages=total, signatures=[n_sheets],
                pages_per_signature=[8] * n_sheets, family=bindings.MINI8,
                folds=3)


def _impose_fukurotoji(total: int, settings: ImpositionSettings,
                       notes: List[str]) -> Plan:
    sheet = settings.effective_sheet()
    grid = page_grid(sheet, settings)
    cols, rows = grid.cols, grid.rows
    n_sheets = int(math.ceil(total / 2))
    sheets = []
    for k in range(1, n_sheets + 1):
        left, right = 2 * k - 1, 2 * k
        slots = [
            _make_slot(left if left <= total else None, cols, rows, 0, 0, 0,
                       settings, sheet, 0.0, "left", grid),
            _make_slot(right if right <= total else None, cols, rows, 1, 0, 0,
                       settings, sheet, 0.0, "right", grid),
        ]
        a = imposition_area(sheet, settings)
        x_fold = grid.boundary_x(1)
        fold = [(x_fold, a[1], x_fold, a[3], "fold")]
        sheets.append(SheetPlan(k, "front", 1, k, n_sheets, slots, fold))
    notes.append("Single sided. Fold each sheet print-side-out, fold at the "
                 "fore edge, and bind through the open edges.")
    return Plan(sheets=sheets, sheet_size=sheet,
                page_size=settings.trim_size or grid.cell_size(),
                source_pages=total, total_pages=total, signatures=[n_sheets],
                pages_per_signature=[total], family=bindings.FUKUROTOJI, folds=1)


def _impose_accordion(total: int, settings: ImpositionSettings,
                      notes: List[str]) -> Plan:
    sheet = settings.effective_sheet()
    grid = page_grid(sheet, settings)
    cols, rows = grid.cols, grid.rows
    per_sheet = cols * (1 if settings.single_sided else 2)
    n_sheets = int(math.ceil(total / per_sheet))
    sheets = []
    for k in range(1, n_sheets + 1):
        base = (k - 1) * per_sheet
        front = []
        for c in range(cols):
            p = base + c + 1
            front.append(_make_slot(p if p <= total else None, cols, rows, c, 0,
                                    0, settings, sheet, 0.0, "none", grid))
        a = imposition_area(sheet, settings)
        fold = [(grid.boundary_x(c), a[1], grid.boundary_x(c), a[3], "fold")
                for c in range(1, cols)]
        sheets.append(SheetPlan(k, "front", 1, k, n_sheets, front, fold))
        if not settings.single_sided:
            back = []
            for c in range(cols):
                p = base + cols + c + 1
                bc, br, rot = _apply_back_flip(cols, rows, cols - 1 - c, 0, 0,
                                               settings, sheet)
                back.append(_make_slot(p if p <= total else None, cols, rows,
                                       bc, br, rot, settings, sheet, 0.0,
                                       "none", grid))
            sheets.append(SheetPlan(k, "back", 1, k, n_sheets, back, list(fold)))
    notes.append("Fold alternately mountain and valley; glue the strips end to "
                 "end with a 5 mm tab before folding.")
    return Plan(sheets=sheets, sheet_size=sheet,
                page_size=settings.trim_size or grid.cell_size(),
                source_pages=total, total_pages=total, signatures=[n_sheets],
                pages_per_signature=[total], family=bindings.ACCORDION, folds=0)


def shift_sheet(sp: SheetPlan, dx: float, dy: float) -> SheetPlan:
    """A copy of one sheet side with everything moved by (dx, dy)."""
    if dx == 0 and dy == 0:
        return sp
    slots = [Slot(page=s.page, cell=s.cell,
                  cell_rect=_shift(s.cell_rect, dx, dy),
                  trim_rect=_shift(s.trim_rect, dx, dy),
                  content_rect=_shift(s.content_rect, dx, dy),
                  rotation=s.rotation, spine=s.spine, creep=s.creep)
             for s in sp.slots]

    def move(lines):
        return [(a + dx, b + dy, c + dx, d + dy, kind)
                for (a, b, c, d, kind) in lines]

    return SheetPlan(number=sp.number, side=sp.side, signature=sp.signature,
                     sheet_in_signature=sp.sheet_in_signature,
                     sheets_in_signature=sp.sheets_in_signature,
                     slots=slots, fold_lines=move(sp.fold_lines),
                     cut_lines=move(sp.cut_lines))


def duplex_correction(reading_x: float, reading_y: float, axis: str
                      ) -> Tuple[float, float]:
    """Turn a reading off the registration sheet into a back-side shift.

    The reading is where the back's pointer lands on the front's scale, seen
    from the front with the light behind: right and down are positive. Which
    way that has to be undone on the back page depends on which edge the
    sheet was turned about.
    """
    if axis == "vertical":          # turned left to right
        return (reading_x, -reading_y)
    return (-reading_x, reading_y)  # turned top to bottom


# ---------------------------------------------------------------------------
# Print ordering
# ---------------------------------------------------------------------------

def order_sheets(plan: Plan, mode: str = "duplex") -> List[SheetPlan]:
    """Order the sheet sides for printing.

    duplex      - front, back, front, back (automatic duplexer)
    fronts      - every front, in order (manual duplex pass 1)
    backs       - every back, in order (manual duplex pass 2)
    backs_rev   - every back, reversed (for printers that re-stack face up)
    """
    if mode == "duplex":
        return list(plan.sheets)
    fronts = [s for s in plan.sheets if s.side == "front"]
    backs = [s for s in plan.sheets if s.side == "back"]
    if mode == "fronts":
        return fronts
    if mode == "backs":
        return backs
    if mode == "backs_rev":
        return list(reversed(backs))
    raise ValueError(mode)
