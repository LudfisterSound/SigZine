"""Sending a finished PDF straight to a printer.

On macOS and Linux this talks to CUPS through lpstat and lpr, which is the
same path the system print dialog uses, minus the dialog. The important part
is that scaling is switched off: an imposition that gets shrunk to fit is
worse than useless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SIDES = {
    "none": "one-sided",
    "long edge": "two-sided-long-edge",
    "short edge": "two-sided-short-edge",
}


@dataclass
class Printer:
    name: str
    status: str = ""
    is_default: bool = False

    @property
    def label(self) -> str:
        pretty = self.name.replace("_", " ")
        bits = [pretty]
        if self.status:
            bits.append(f"- {self.status}")
        if self.is_default:
            bits.append("(default)")
        return " ".join(bits)


def describe(cmd: Sequence[str]) -> str:
    """A command line that can be pasted into a terminal as it is."""
    import shlex
    return " ".join(shlex.quote(str(c)) for c in cmd)


def _run(cmd: Sequence[str], timeout: float = 6.0) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out"
    except Exception as exc:                       # pragma: no cover
        return 1, "", str(exc)


def available() -> bool:
    if sys.platform.startswith("win"):
        return True
    return shutil.which("lpr") is not None


def default_printer() -> Optional[str]:
    code, out, _ = _run(["lpstat", "-d"])
    if code == 0 and ":" in out:
        return out.split(":", 1)[1].strip() or None
    return None


def list_printers() -> List[Printer]:
    """Every queue CUPS knows about, default first."""
    if sys.platform.startswith("win"):
        return []
    printers: List[Printer] = []
    code, out, _ = _run(["lpstat", "-p"])
    if code == 0:
        for line in out.splitlines():
            if not line.startswith("printer "):
                continue
            rest = line[len("printer "):]
            name = rest.split(" ", 1)[0]
            status = ""
            low = rest.lower()
            for word in ("is idle", "now printing", "disabled", "is busy",
                         "not accepting"):
                if word in low:
                    status = word.replace("is ", "")
                    break
            printers.append(Printer(name=name, status=status))
    if not printers:
        code, out, _ = _run(["lpstat", "-e"])
        if code == 0:
            printers = [Printer(name=n.strip()) for n in out.split() if n.strip()]
    d = default_printer()
    for p in printers:
        p.is_default = (p.name == d)
    printers.sort(key=lambda p: (not p.is_default, p.name.lower()))
    return printers


# The options every CUPS queue understands, whatever driver is behind it:
# CUPS maps them onto the queue's own settings.
#
# These do not: "fit-to-page" belongs to the cups-filters pdftopdf filter,
# which Apple's CUPS does not use, and "print-scaling" arrived in CUPS 2.4,
# newer than what macOS ships. CUPS passes an option it does not recognise
# through to the backend as a job attribute, and a driverless queue hands
# that straight to the printer in the IPP request - where a printer that
# does not support the attribute can reject the entire job. The symptom is
# a job that is accepted, sent, and then vanishes with nothing printed. So
# they are only sent to a queue that says it has them.
SCALING_OPTIONS = ("print-scaling", "fit-to-page")
_NO_SCALING = {"print-scaling": "none", "fit-to-page": "false"}

_OPTION_CACHE: Dict[str, Dict[str, List[str]]] = {}


def supported_options(printer: Optional[str] = None,
                      refresh: bool = False) -> Dict[str, List[str]]:
    """What ``lpoptions`` says this queue accepts, by option name."""
    key = printer or ""
    if not refresh and key in _OPTION_CACHE:
        return _OPTION_CACHE[key]
    cmd = ["lpoptions", "-l"] + (["-p", printer] if printer else [])
    code, out, _ = _run(cmd)
    found: Dict[str, List[str]] = {}
    if code == 0:
        for line in out.splitlines():
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            # "PageSize/Media Size: *Letter Legal A4"
            name = name.split("/", 1)[0].strip()
            if name:
                found[name] = [v.lstrip("*") for v in values.split()]
    _OPTION_CACHE[key] = found
    return found


# Colour is asked for the same careful way, and for the same reason: there is
# no one spelling of it. A driverless queue advertises the IPP attribute
# print-color-mode; a queue with a PPD behind it has ColorModel instead, and
# the values in there are the driver's own vocabulary (RGB, CMYK, Gray,
# KGray, and others besides). So the option is looked up, the value is picked
# from what the queue says it accepts, and a queue that offers neither is
# left to its own default rather than guessed at.
COLOR_OPTIONS = ("print-color-mode", "ColorModel", "ColorMode")

_COLOR_VALUES = {
    "print-color-mode": (("color",), ("monochrome", "auto-monochrome")),
    "ColorModel": (("RGB", "CMYK", "RGBW", "DeviceRGB", "Color", "CMY"),
                   ("Gray", "KGray", "Grayscale", "DeviceGray", "Black",
                    "Monochrome")),
    "ColorMode": (("color", "Color", "RGB"),
                  ("monochrome", "Monochrome", "Gray", "Grayscale")),
}


def color_option(color: bool, printer: Optional[str] = None,
                 supported: Optional[Dict[str, List[str]]] = None
                 ) -> List[str]:
    """``-o`` pair asking for colour or for black and white, if we can.

    Empty when the queue advertises no colour option we recognise, or
    advertises one but not a value for the mode asked for - a printer with
    only black toner has nothing to say yes to.
    """
    known = supported_options(printer) if supported is None else supported
    for name in COLOR_OPTIONS:
        if name not in known:
            continue
        wanted = _COLOR_VALUES[name][0 if color else 1]
        offered = known[name]
        lowered = {v.lower(): v for v in offered}
        for candidate in wanted:
            if candidate.lower() in lowered:
                return ["-o", f"{name}={lowered[candidate.lower()]}"]
        return []
    return []


def build_options(duplex: str = "none", media: Optional[str] = None,
                  collate: bool = True, copies: int = 1,
                  printer: Optional[str] = None,
                  color: Optional[bool] = None,
                  supported: Optional[Dict[str, List[str]]] = None
                  ) -> List[str]:
    """``lpr`` options for one job. ``color`` of None leaves the queue alone."""
    opts: List[str] = []
    opts += ["-o", f"sides={SIDES.get(duplex, 'one-sided')}"]
    if media:
        opts += ["-o", f"media={media}"]
    # Meaningless on a single copy, and every extra attribute is one more
    # thing for a fussy queue to object to.
    if collate and copies > 1:
        opts += ["-o", "collate=true"]

    # Never let the driver rescale an imposition - but only say so in a way
    # this particular queue understands. An imposed sheet is already the
    # size of the paper, so CUPS has no reason to scale it regardless.
    known = supported_options(printer) if supported is None else supported
    for name in SCALING_OPTIONS:
        if name in known:
            opts += ["-o", f"{name}={_NO_SCALING[name]}"]
            break

    if color is not None:
        opts += color_option(color, printer, known)
    return opts


def supports_color(printer: Optional[str] = None) -> Optional[bool]:
    """True / False if the queue says, None if it does not say at all."""
    known = supported_options(printer)
    for name in COLOR_OPTIONS:
        if name in known:
            return bool(color_option(True, printer, known))
    return None


def print_pdf(path, printer: Optional[str] = None, copies: int = 1,
              duplex: str = "none", media: Optional[str] = None,
              title: Optional[str] = None, color: Optional[bool] = None,
              extra: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
    """Submit ``path``. Returns (ok, message)."""
    path = Path(path)
    if not path.exists():
        return False, f"{path.name} is not there any more"

    if sys.platform.startswith("win"):            # pragma: no cover
        try:
            os.startfile(str(path), "print")      # type: ignore[attr-defined]
            return True, "Sent to the default Windows printer"
        except Exception as exc:
            return False, str(exc)

    if not available():
        return False, ("No lpr on this system, so there is nothing to print "
                       "through. Export the PDF and print it yourself.")

    cmd: List[str] = ["lpr"]
    if printer:
        cmd += ["-P", printer]
    if copies and copies > 1:
        cmd += ["-#", str(int(copies))]
    cmd += ["-T", (title or path.stem)[:80]]
    cmd += build_options(duplex, media, copies=copies, printer=printer,
                         color=color)
    if extra:
        cmd += list(extra)
    cmd += [str(path)]

    code, out, err = _run(cmd, timeout=30.0)
    if code != 0:
        why = (err or out or f"lpr exited with {code}").strip()
        # The command itself is the thing anyone debugging this needs, and
        # it is otherwise invisible from inside the application.
        return False, f"{why}\n\nThe command was:\n  {describe(cmd)}"
    where = printer or default_printer() or "the default printer"
    n = f"{copies} copies of " if copies > 1 else ""
    return True, f"Sent {n}{path.name} to {where}"


def queue_summary(printer: Optional[str] = None) -> str:
    cmd = ["lpstat", "-o"] + ([printer] if printer else [])
    code, out, _ = _run(cmd)
    if code != 0:
        return ""
    jobs = [l for l in out.splitlines() if l.strip()]
    if not jobs:
        return "queue empty"
    return f"{len(jobs)} job(s) waiting"
