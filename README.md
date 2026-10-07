# Signature Zine

A desktop application for laying out, tone-correcting and printing zines and
book signatures on an ordinary laser printer.

It does three things properly:

- **Imposition.** Pick a binding and it works out which page goes where on
  which sheet, which way up, and what to tell the print dialog. The folded
  layouts are derived by simulating the physical folds, so the folding
  instructions always match the file.
- **Tone.** Photographs that look right on screen print muddy. The tone
  engine measures what your printer actually does to every grey and builds
  the inverse of it, then applies that to every image on the way out.
- **Paperwork.** Printing instructions, punching templates with the sewing
  stations measured out, and a folding dummy you can test a new binding with
  before committing a whole run.

## Install

Two ways. If you just want to use the program, build it once as a normal
application and forget the rest of this section:

```bash
packaging/build.sh --dmg       # macOS: dist/Signature Zine.dmg
packaging/build.sh             # Linux or Windows: dist/
```

That produces a self-contained application with Python and everything else
inside it. Open the disk image, drag **Signature Zine** to Applications, and
it runs like anything else - no terminal, no virtual environment, and
`.sigzine` files open by double-clicking.

The first launch needs one extra step, because the app is not signed with an
Apple developer certificate: right-click it and choose **Open**, then
**Open** again. Double-clicking it the first time gives "cannot be opened
because the developer cannot be verified" and no way past. After that it
opens normally. To check a build did not come out broken:

```bash
"/Applications/Signature Zine.app/Contents/MacOS/Signature Zine" --self-test
```

The bundle is around 300 MB, nearly all of it Qt and MuPDF.

### Running from source

For working on the program:

```bash
./setup.sh      # installs PySide6, PyMuPDF, Pillow, NumPy
./run.sh        # starts the application
```

Or install it into your own environment, which puts a `signature-zine`
command on the path:

```bash
pip install .
```

The virtual environment is created in *~/Library/Application Support/Signature
Zine/venv*, not in the project folder. This folder usually lives under
*~/Documents*, which on a Mac is normally synced to iCloud Drive: iCloud would
upload a gigabyte of Qt, and while it manages those files it sets the BSD
`hidden` flag on them, which stops Qt from finding its own plugins and the
application from starting. Set `SIGZINE_VENV` to put it somewhere else, or
create a `venv/` in the project folder and the scripts will use that instead.

On macOS you can also double-click **Signature Zine.command** in the Finder.

Requires Python 3.9 or newer. Nothing else needs to be installed system-wide.

## Starting a document

The first thing the application asks is what you are making. **Zine** is short
work printed in one go - folded or stapled straight into the finished thing,
with a digital edition alongside. **Book signatures** is longer work in
sections that get gathered and sewn or glued. Each offers a few ready-made
starting points, and each one is a way of building the thing rather than a
paper size: saddle stitched with one fold or two, the eight-page mini, side
stapled, sewn signatures with one fold or two, coptic, perfect bound,
Japanese stab.

The paper is chosen separately, in the same dialog, and everything that
depends on it follows from it: which way round the sheet is fed, how big a
page comes out of it, the margins and the waste left at the edges. So the
same construction works on Letter, A4, A3 or anything else in the list, and
the layout always fills the paper that is loaded instead of leaving part of
the sheet unprinted. The *Feed as* control on the Layout tab is on
*Automatic* for that reason; set it to landscape or portrait if you want to
overrule it. All of it is only a starting point - the Layout tab can change
anything afterwards.

## Printing

**Print now…** on the toolbar, in the File menu (Cmd-P) or on the Export tab
imposes the sheets and hands them to a printer queue directly. It picks up
your printers from CUPS, defaults the duplex flip to whatever the imposition
actually needs, sets the paper size, and switches scaling off in the job so
nothing gets shrunk to fit. For a printer with no duplexer there is a manual
two-pass mode: fronts first, then a prompt to reload the tray, then the backs.

## The five tabs

### Pages
Import PDFs, photographs, scans, or plain text and Markdown-ish files, which
are typeset into pages for you. Drag to reorder, rotate, duplicate, insert
blanks. Blank pages are added automatically so the signatures come out even -
they appear in the page list marked *blank (auto)* rather than happening
invisibly at export time.

Per page you can set the fit (fit, fill, actual size, stretch), rotation,
scale, a nudge in either direction, and a tone preset that overrides the
document default.

### Layout
Choose the binding; everything else follows from it.

| Binding | Imposition | Notes |
| --- | --- | --- |
| Saddle stitch (stapled zine) | nested folios | the classic photocopied zine, creep compensated |
| Pamphlet stitch, 3 and 5 hole | nested folios | sewing template included |
| Section sewn (Smyth) | nested folios, many signatures | collation marks on the spine |
| Coptic stitch | nested folios, many signatures | opens flat |
| Long stitch | nested folios, many signatures | sewn through the cover |
| Side stapled (flat zine) | flat leaves | no folding at all, stapled down the edge |
| Perfect bound | 2-up cut-and-stack | left pile then right pile |
| Screw post | flat leaves | drilled, rebindable |
| Japanese stab (fukurotoji) | single-sided pairs | folded at the fore edge |
| French fold | single-sided pairs | hides show-through |
| 8-page mini zine | one sheet, one cut | no staples, no thread |
| Accordion | panel strips | folds concertina |

Sheets can be folded once (folio), twice (quarto), three times (octavo) or
four times (sextodecimo); the app shows how many pages that puts on a sheet.
Set the sheets per signature and it plans the sections, evening out the last
one instead of leaving it nearly empty.

The preview shows the actual imposed sheet with page numbers over it, and
**Simulate print** shows what the paper will look like rather than the pale,
pre-compensated data that is sent to the printer.

Creep compensation shifts each nested sheet toward the spine so the fore edge
trims clean. The duplex setting defaults to *Automatic*, which works out
whether your driver's "long edge" or "short edge" gives the left-right turn a
folded booklet needs, and tells you which one to pick.

### Tone
A draggable curve with the image histogram behind it, and the effective curve
- everything including the press correction - drawn over the top in orange.

Presets cover the usual cases: laser default, punchy, soft with open shadows,
newsprint, photocopy look, line art, halftone at 85 or 53 lpi, error
diffusion, and a neutral pass-through for the digital edition.

Controls: **print in colour**, grey conversion (luminosity, orthochromatic,
colour filters, single channels), auto levels, black and white point, gamma,
contrast, local contrast, unsharp mask, minimum dot, maximum ink, dot gain,
and screening - contone, Floyd-Steinberg, blue-noise stochastic, ordered
Bayer, or a clustered-dot halftone at a ruling and angle you choose.

*Print in colour* keeps the three channels all the way to the PDF instead of
flattening every page to grey first. Everything after the grey step works the
same way on all three: the same curve, the same linearisation, the same ink
limits, and a screen per channel at 30 degrees apart so the dots interleave
rather than pile up. Off is the default, and off is what a mono laser wants.

### Printer
Six test sheets, each with a **Print** and a **Save…** button:

- **Linearisation target** - 0-100% in 2% steps, plus 0.5% wedges at both
  ends, a continuous gradient and a fill-in check. Corner fiducials let a
  scan be read automatically.
- **Screening comparison** - the same photograph through every halftone.
- **Detail and resolution** - hairlines from 0.05 pt, type from 4 pt,
  positive and reversed, a Siemens star and a line-pair wedge.
- **Duplex registration** - a graduated cross on the front and a pointer on
  the back. Hold the sheet to a window, read the two numbers, type them in,
  and every back page afterwards is moved to land under its front. Also
  carries scale bars in both directions, in whole centimetres and ticked at
  both ends, to catch a printer that is quietly scaling your work.
- **Linearisation check** - the ladder of greys printed through a finished
  correction, to see what error is left. Covered below.
- **Photo proof** - one photograph, six treatments, pick the winner.

**Print** sends the sheet straight to the queue you pick at the top, with
`fit-to-page` and `print-scaling` switched off, and keeps the PDF in
*Application Support/Signature Zine/test sheets* along with the patch map
the scan reader needs later. This is the one to use: a calibration target
has to come out at exactly 100%, and the usual route through a PDF viewer
is precisely where a stray "scale to fit" ruins one without saying so. The
registration sheet is the only one sent two-sided, flipping on the edge
your current imposition implies.

**Save…** writes the PDF wherever you like and opens it, for when you want
the file itself - to print from somewhere else, or to keep.

The queue you choose is remembered on the profile, since a profile
describes one particular printer.

Then read the linearisation sheet back in one of three ways: scan it and let
the app find the corners and sample all 93 patches, type in densitometer or
L\* readings, or answer three questions by eye. The result is a printer
profile - saved in *Application Support/Signature Zine/printers* and reusable
across documents - giving you the dot gain at each quarter tone, the smallest
dot the printer can actually hold, and the point where the shadows stop
separating.

#### Building the correction

**Build the correction from these readings** fits a smooth curve to the
patches and inverts it. It does not simply join the dots: ninety-odd patches
read off a scanner carry a percent or two of noise, and a correction curve
that reproduces that noise prints as banding in every gradient - the exact
fault linearisation is supposed to cure. So the builder

- averages the patches that asked for the same ink, which a full target
  prints several of,
- estimates how noisy the measurement is from how much each patch disagrees
  with the two beside it, and smooths by exactly that much and no more,
- drops patches that disagree with their neighbours - a crease, a speck of
  dirt, a dust mote on the scanner glass,
- takes paper white and the solid off the fitted curve rather than off the
  single 0% and 100% patches, so two noisy readings cannot tilt the whole
  profile,
- and keeps the result monotone, because a correction that doubles back
  posterises.

It then tells you what it found: the noise floor, how far the curve sits
from the readings, and how many patches it ignored. On a scan noisy enough
to put raw interpolation 10% out, the fitted curve lands within 1.5%.

#### Checking it on paper

A profile is a claim about your printer, and the only way to settle it is to
print. **Linearisation check sheet** prints an even ladder of greys *through*
the correction - each patch labelled with the coverage it is aiming at, not
the ink actually sent. Measure it and the error you read is the error that
is left. Under 2-3% and you are done.

If it is further out, **refine the profile from this**. The second
measurement is worth more than the first: it was taken through the
correction, so it says what the printer does where it is actually being
asked to work, and folding it back in converges instead of just repeating
the first guess. One refinement normally closes what is left. The profile
remembers how many passes it has had.

### Export
- the imposed print run, as one interleaved file or separate fronts and backs
- a digital edition: single pages or spreads, screen tone, with bookmarks
- printing and binding instructions
- a punching template with the stations measured from the head
- a folding dummy with nothing on it but big page numbers

## The tone pipeline

```
RGB  ->  grey  ->  levels, gamma, contrast, your curve
     ->  local contrast and unsharp mask, at output resolution
     ->  printer linearisation (measured, or a dot-gain model)
     ->  ink limits: minimum dot, maximum ink, clean paper white
     ->  screening  ->  the PDF
```

With *print in colour* on, the grey step is dropped and every stage after it
runs on three channels instead of one.

Imported PDF pages pass through as vectors and stay sharp. They are only
rasterised if you give a page its own tone preset, or tick *Also tone-correct
imported PDF pages* on the Export tab. Images are always resampled to the
output resolution: the device grid when a halftone screen is on, the contone
resolution otherwise.

## Printing

The Print dialog has an **Ink** choice: colour, black and white, or whatever
the queue is already set to. It is only sent to a printer that says it
understands the option, and it tells you when the document itself is still
being flattened to grey, because that decides the result before the printer
ever sees it. Calibration sheets are always sent as black and white: a target
whose greys were built out of three toners measures nothing useful.

Print at 100%. "Fit to page" will quietly ruin the imposition. Turn off toner
save and every enhancement in the driver - the correction is already in the
file, and a second one on top of it will undo the first. Run the folding
dummy before a new binding; one sheet of paper is cheaper than a whole run.

## Files

Projects are saved as `.sigzine`, which is JSON: settings, page order and
per-page overrides, with sources referenced by path rather than copied in.
Printer profiles are separate JSON files so they can be shared.

## Tests

```bash
./test.sh
```

82 tests covering the fold simulation against the classic folio, quarto and
octavo formes, page coverage for every binding, creep direction, duplex flip
geometry, the registration solver, curve monotonicity, closed-loop
linearisation, scan measurement, vector pass-through and project
round-tripping.

The linearisation builder is tested against a simulated printer: that the
fit tracks a known response, beats raw interpolation on noisy readings,
stays monotone, pools repeated patches instead of discarding them, rejects a
ruined one, survives having only five patches to work from, and that a check
sheet rendered, "printed", scanned and measured comes back linear. The
Printer tab has its own tests for the table that holds the readings, since
that is the one place a profile can silently rot; they skip themselves if Qt
cannot start.

## If something goes wrong

**Qt will not start, "could not find the Qt platform plugin".** macOS has set
the BSD `hidden` flag on the installed files and Qt cannot enumerate its own
plugin directory - iCloud Drive does this to files it is managing. `run.sh`
clears the flag on every launch, and the environment is kept out of iCloud in
the first place. To fix it by hand: `chflags -R nohidden "$SIGZINE_VENV"`.

**The backs print upside down.** Set Duplex to *Automatic* on the Layout tab
and use the flip setting it names on the instruction sheet. If the registration
sheet's BACK HEAD bar shows through near the foot of the front, the sheet is
being turned about the wrong edge - change the duplex setting, not the offset.

**The backs are offset from the fronts.** Every desktop duplexer is out by a
little. Print the duplex registration sheet, read the two numbers off it, and
enter them under *Front to back registration* on the Printer tab. A millimetre
or so is normal; several millimetres usually means the paper guides in the
tray are loose. Measure two or three sheets and use the average - sheet to
sheet variation of a few tenths is nothing to chase.

**Everything prints too light.** The pre-compensated data is meant to look
light on screen. If the paper is also light, either your dot gain is set too
high or the driver is applying its own correction on top - turn off toner
save and any "enhancement".

**The fore edge is uneven after folding.** That is creep. Raise the creep per
sheet on the Layout tab, or use fewer sheets per signature.
