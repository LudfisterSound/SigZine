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

```bash
./setup.sh      # installs PySide6, PyMuPDF, Pillow, NumPy
./run.sh        # starts the application
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
starting points (half-letter saddle stitched, quarter-letter, eight-page mini,
side stapled, digest sewn, A6 quarto, coptic, perfect bound, Japanese stab),
and every one of them is only a starting point - the Layout tab can change
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

Controls: grey conversion (luminosity, orthochromatic, colour filters,
single channels), auto levels, black and white point, gamma, contrast, local
contrast, unsharp mask, minimum dot, maximum ink, dot gain, and screening -
contone, Floyd-Steinberg, blue-noise stochastic, ordered Bayer, or a
clustered-dot halftone at a ruling and angle you choose.

### Printer
Five test sheets:

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
- **Photo proof** - one photograph, six treatments, pick the winner.

Then read the linearisation sheet back in one of three ways: scan it and let
the app find the corners and sample all 93 patches, type in densitometer or
L\* readings, or answer three questions by eye. The result is a printer
profile - saved in *Application Support/Signature Zine/printers* and reusable
across documents - giving you the dot gain at each quarter tone, the smallest
dot the printer can actually hold, and the point where the shadows stop
separating.

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

Imported PDF pages pass through as vectors and stay sharp. They are only
rasterised if you give a page its own tone preset, or tick *Also tone-correct
imported PDF pages* on the Export tab. Images are always resampled to the
output resolution: the device grid when a halftone screen is on, the contone
resolution otherwise.

## Printing

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

35 tests covering the fold simulation against the classic folio, quarto and
octavo formes, page coverage for every binding, creep direction, duplex flip
geometry, curve monotonicity, closed-loop linearisation, scan measurement,
vector pass-through and project round-tripping.

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
