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
from typing import List, Optional, Sequence, Tuple

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


def build_options(duplex: str = "none", media: Optional[str] = None,
                  collate: bool = True) -> List[str]:
    opts: List[str] = []
    opts += ["-o", f"sides={SIDES.get(duplex, 'one-sided')}"]
    # never let the driver rescale an imposition
    opts += ["-o", "fit-to-page=false", "-o", "print-scaling=none"]
    if media:
        opts += ["-o", f"media={media}"]
    if collate:
        opts += ["-o", "collate=true"]
    return opts


def print_pdf(path, printer: Optional[str] = None, copies: int = 1,
              duplex: str = "none", media: Optional[str] = None,
              title: Optional[str] = None,
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
    cmd += build_options(duplex, media)
    if extra:
        cmd += list(extra)
    cmd += [str(path)]

    code, out, err = _run(cmd, timeout=30.0)
    if code != 0:
        return False, (err or out or f"lpr exited with {code}").strip()
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
