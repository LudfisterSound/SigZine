"""Binding methods and the layout constraints each one implies."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Imposition families
FOLDED = "folded"          # nested sheets folded together into signatures
FLAT = "flat"              # single leaves, 2-up cut-and-stack (or 1-up)
MINI8 = "mini8"            # one sheet, eight pages, single cut, no sewing
FUKUROTOJI = "fukurotoji"  # single-sided pairs, folded at the fore edge
ACCORDION = "accordion"    # continuous concertina strip


@dataclass(frozen=True)
class Binding:
    key: str
    name: str
    family: str
    blurb: str
    page_multiple: int = 4
    signatures: bool = True                 # can the book be split into sections
    default_sheets_per_signature: int = 1
    max_sheets_per_signature: int = 16
    folds_per_sheet: int = 1                # 1 = folio, 2 = quarto, 3 = octavo
    duplex: bool = True
    creep: bool = True
    spine_allowance_mm: float = 0.0         # extra gutter this binding wants
    sewing_template: Optional[str] = None   # key used by templates.py
    default_holes: int = 0
    steps: List[str] = field(default_factory=list)
    tips: List[str] = field(default_factory=list)

    @property
    def pages_per_sheet(self) -> int:
        if self.family == MINI8:
            return 8
        if self.family == FUKUROTOJI:
            return 2
        if self.family == ACCORDION:
            return 0  # variable, panel driven
        return 2 ** self.folds_per_sheet * 2

    def pages_per_signature(self, sheets: int) -> int:
        if self.family == MINI8:
            return 8
        if self.family == FUKUROTOJI:
            return 2 * sheets
        return self.pages_per_sheet * max(1, sheets)


BINDINGS: Dict[str, Binding] = {}


def _add(b: Binding) -> Binding:
    BINDINGS[b.key] = b
    return b


_add(Binding(
    key="saddle",
    name="Saddle stitch (stapled zine)",
    family=FOLDED,
    blurb="Nested folded sheets stapled through the spine fold. The classic "
          "photocopied zine.",
    page_multiple=4,
    signatures=False,
    default_sheets_per_signature=4,
    max_sheets_per_signature=16,
    folds_per_sheet=1,
    spine_allowance_mm=0.0,
    sewing_template="saddle",
    default_holes=2,
    steps=[
        "Print duplex using the flip setting on the instruction sheet.",
        "Check the sheet numbers printed in the trim area before folding.",
        "Nest the sheets in order, lowest sheet number on the outside.",
        "Score the spine with a bone folder, then fold the whole nest at once.",
        "Open flat over a saddle or a stack of foam and staple through the fold.",
        "Trim the fore edge after folding so the creep is cleaned up.",
    ],
    tips=[
        "Above about 20 sheets of 80 gsm the fore-edge creep gets ugly; split it "
        "into sections and switch to a sewn binding.",
        "Long-reach or saddle staplers are cheap; a folded scrap of card and a "
        "normal stapler will also do if you open the stapler flat.",
    ],
))

_add(Binding(
    key="pamphlet",
    name="Pamphlet stitch (3-hole)",
    family=FOLDED,
    blurb="One folded section sewn with a single thread through three holes.",
    page_multiple=4,
    signatures=False,
    default_sheets_per_signature=5,
    max_sheets_per_signature=10,
    folds_per_sheet=1,
    sewing_template="pamphlet3",
    default_holes=3,
    steps=[
        "Fold and nest the section, then punch the holes from the inside of the fold.",
        "Start on the inside at the centre hole, leaving a 10 cm tail.",
        "Out through the head hole, along the outside, in through the tail hole.",
        "Back out through the centre hole, on the other side of the long stitch.",
        "Tie a square knot around the long stitch, trapping it in the middle.",
    ],
    tips=["Linen thread waxed with beeswax will not saw through the fold."],
))

_add(Binding(
    key="pamphlet5",
    name="Pamphlet stitch (5-hole)",
    family=FOLDED,
    blurb="A sturdier pamphlet stitch for taller sections.",
    page_multiple=4,
    signatures=False,
    default_sheets_per_signature=6,
    max_sheets_per_signature=12,
    folds_per_sheet=1,
    sewing_template="pamphlet5",
    default_holes=5,
    steps=[
        "Punch five evenly spaced holes, the centre one at the middle of the spine.",
        "Sew out from the centre, working to the head, back down to the tail, "
        "and home to the centre.",
        "Knot around the long centre stitch.",
    ],
))

_add(Binding(
    key="sewn",
    name="Section sewn (Smyth)",
    family=FOLDED,
    blurb="Several folded signatures sewn to each other; the standard book block.",
    page_multiple=4,
    signatures=True,
    default_sheets_per_signature=4,
    max_sheets_per_signature=8,
    folds_per_sheet=1,
    spine_allowance_mm=3.0,
    sewing_template="sewn",
    default_holes=6,
    steps=[
        "Fold each signature and press it under a weight overnight.",
        "Punch all signatures against one pierced template so the stations line up.",
        "Sew signature to signature with a kettle stitch at the head and tail.",
        "Glue the spine with PVA, round it if you want, then case in.",
    ],
    tips=[
        "16 pages per signature (4 sheets) is the sweet spot for 80-100 gsm paper.",
        "Collation marks print on the spine fold: a staircase means the order is right.",
    ],
))

_add(Binding(
    key="coptic",
    name="Coptic stitch",
    family=FOLDED,
    blurb="Exposed chain stitch between signatures and boards. Opens dead flat.",
    page_multiple=4,
    signatures=True,
    default_sheets_per_signature=4,
    max_sheets_per_signature=8,
    folds_per_sheet=1,
    spine_allowance_mm=4.0,
    sewing_template="coptic",
    default_holes=6,
    steps=[
        "Punch the boards to match the signature stations.",
        "Sew the first signature to the back board with a simple loop at each station.",
        "Add each signature with a chain stitch that links into the loop below.",
        "Finish into the front board and tie off inside the last signature.",
    ],
    tips=["Because it opens flat, keep the gutter margin generous anyway - the "
          "sewing itself eats a few millimetres."],
))

_add(Binding(
    key="longstitch",
    name="Long stitch",
    family=FOLDED,
    blurb="Signatures sewn directly through a wrap-around cover.",
    page_multiple=4,
    signatures=True,
    default_sheets_per_signature=4,
    max_sheets_per_signature=8,
    folds_per_sheet=1,
    spine_allowance_mm=3.0,
    sewing_template="longstitch",
    default_holes=6,
    steps=[
        "Score the cover for the spine, one panel per signature plus turn-ins.",
        "Cut or punch the slits in the cover spine panels.",
        "Sew each signature to its own spine panel, running a long stitch outside.",
    ],
))

_add(Binding(
    key="sidestaple",
    name="Side stapled (flat zine)",
    family=FLAT,
    blurb="Single leaves stapled down the left edge or through one corner. "
          "No folding at all.",
    page_multiple=2,
    signatures=False,
    folds_per_sheet=0,
    creep=False,
    spine_allowance_mm=12.0,
    sewing_template="sidestaple",
    default_holes=2,
    steps=[
        "Print, then guillotine if you imposed more than one leaf per sheet.",
        "Jog the stack square against the spine edge and clamp it.",
        "Staple 8-12 mm in from the spine, using the printed template.",
        "Two or three staples for a thick zine, one through the corner at 45 "
        "degrees for a thin one.",
    ],
    tips=[
        "A long-reach stapler is not needed here - an ordinary desk stapler "
        "reaches the spine margin easily.",
        "Side stapling eats into the gutter: keep at least 15 mm of spine "
        "margin or the text disappears into the fold.",
        "The pages will not lie flat. That is the trade for not folding "
        "anything.",
    ],
))

_add(Binding(
    key="perfect",
    name="Perfect bound (glued)",
    family=FLAT,
    blurb="Single leaves, spine roughened and glued into a wrapper.",
    page_multiple=2,
    signatures=False,
    default_sheets_per_signature=1,
    folds_per_sheet=0,
    creep=False,
    spine_allowance_mm=6.0,
    steps=[
        "Print 2-up cut-and-stack, guillotine down the centre, keep both piles in order.",
        "Jog the block square and clamp it with 3-5 mm of spine proud of the jig.",
        "Rough the spine with coarse sandpaper, then notch it every centimetre.",
        "Two thin coats of flexible PVA, fanning the block for the second coat.",
        "Wrap the cover while the glue is tacky and clamp until dry.",
    ],
    tips=["Laser toner and PVA are not friends: sand or notch the spine or the "
          "pages will pull out."],
))

_add(Binding(
    key="screwpost",
    name="Screw post / Chicago screws",
    family=FLAT,
    blurb="Single leaves drilled and bolted. Rebindable, good for portfolios.",
    page_multiple=2,
    signatures=False,
    folds_per_sheet=0,
    creep=False,
    spine_allowance_mm=15.0,
    sewing_template="screwpost",
    default_holes=3,
    steps=[
        "Print 2-up cut-and-stack or 1-up, then guillotine.",
        "Drill through the whole block at once using the printed template.",
        "Bolt with posts sized to the block thickness plus the covers.",
    ],
))

_add(Binding(
    key="stab",
    name="Japanese stab (fukurotoji)",
    family=FUKUROTOJI,
    blurb="Single-sided sheets folded at the fore edge, sewn through the open edge.",
    page_multiple=2,
    signatures=False,
    folds_per_sheet=0,
    duplex=False,
    creep=False,
    spine_allowance_mm=18.0,
    sewing_template="stab4",
    default_holes=4,
    steps=[
        "Print single sided only - the reverse of each sheet is hidden inside the fold.",
        "Fold every sheet in half with the print facing out, fold at the fore edge.",
        "Jog the folded edges flush, clamp, and punch through the stab margin.",
        "Sew the four-hole (yotsume toji) pattern, doubling back to fill every gap.",
    ],
    tips=[
        "Because it is single sided you can print on one side of anything, "
        "including paper that would curl or show through when duplexed.",
        "The stab margin is dead space: keep 18-25 mm at the spine.",
    ],
))

_add(Binding(
    key="mini8",
    name="8-page mini zine",
    family=MINI8,
    blurb="One sheet, one cut, eight pages. No staples, no thread.",
    page_multiple=8,
    signatures=False,
    folds_per_sheet=0,
    duplex=False,
    creep=False,
    steps=[
        "Print one side only, landscape.",
        "Fold in half the long way, then in half, then in half again; unfold.",
        "Fold in half the short way and cut the centre slit between the two middle panels.",
        "Open, fold the long way again, push the ends together so the slit opens out.",
        "Wrap the pages round and crease the spine.",
    ],
    tips=["Duplex the sheet with a second 8-page layout to get a 16-page double mini."],
))

_add(Binding(
    key="accordion",
    name="Accordion / concertina",
    family=ACCORDION,
    blurb="A continuous folded strip; can be read as panels or opened as one image.",
    page_multiple=2,
    signatures=False,
    folds_per_sheet=0,
    duplex=True,
    creep=False,
    steps=[
        "Print the strips, trim, and glue them end to end with 5 mm tabs.",
        "Fold alternately mountain-valley, working from one end.",
        "Glue the first and last panels to boards if you want a hard cover.",
    ],
    tips=["Full-bleed images crossing the folds work beautifully here; "
          "keep text away from the creases."],
))

_add(Binding(
    key="frenchfold",
    name="French fold",
    family=FUKUROTOJI,
    blurb="Single-sided sheets folded at the head or fore edge, then bound at the spine.",
    page_multiple=2,
    signatures=False,
    duplex=False,
    creep=False,
    spine_allowance_mm=8.0,
    steps=[
        "Print single sided, fold each sheet with the print out.",
        "Bind the folded stack at the spine with glue, staples or a stab stitch.",
    ],
    tips=["Hides show-through completely: ideal for thin or translucent stock."],
))


def get(key: str) -> Binding:
    return BINDINGS[key]


def keys() -> List[str]:
    return list(BINDINGS.keys())


def names() -> List[str]:
    return [b.name for b in BINDINGS.values()]


def by_name(name: str) -> Binding:
    for b in BINDINGS.values():
        if b.name == name:
            return b
    raise KeyError(name)
