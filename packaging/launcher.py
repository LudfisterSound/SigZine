"""Entry point for the packaged application.

sigzine/__main__.py is written for ``python -m sigzine``, so it imports its
own package with relative imports. PyInstaller runs its entry script as a
plain top-level module with no package around it, and those imports then
fail with "attempted relative import with no known parent package". Going
in through the package by name instead costs one file and leaves the normal
way of starting the program alone.
"""
from __future__ import annotations

import sys


def self_test() -> int:
    """Prove the bundle is complete without needing a screen.

    A packaged application can start its window and still be missing a
    library that is only touched when real work happens - PyMuPDF and the
    numeric stack are both pulled in late. This runs one of each kind of
    job end to end, so a build can be checked on the machine it was built
    for:

        "Signature Zine.app/Contents/MacOS/Signature Zine" --self-test
    """
    import tempfile
    from pathlib import Path

    import numpy as np
    from PIL import Image

    from sigzine import APP_NAME, __version__
    from sigzine.core import linearise, testsheet, tone
    from sigzine.core.project import Project
    from sigzine.core.render import Renderer
    from sigzine.core.units import paper

    print(f"{APP_NAME} {__version__}")
    print(f"python {sys.version.split()[0]}  numpy {np.__version__}")
    try:
        import fitz
        print(f"pymupdf {getattr(fitz, '__doc__', '').strip() or 'ok'}")
    except Exception as exc:
        print(f"pymupdf FAILED: {exc}")
        return 1

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)

        img = testsheet.synthetic_photo(300, 220)
        done = tone.apply_tone(img, tone.ToneSettings(screen="halftone dot"))
        assert done.size[0] > 0, "tone pipeline produced nothing"
        print("tone pipeline        ok")

        pdf, smap = testsheet.linearisation_sheet(out / "lin.pdf",
                                                  paper("Letter"))
        assert pdf.exists() and len(smap.patches) > 50
        print(f"test sheet           ok ({len(smap.patches)} patches)")

        levels = [p.nominal for p in smap.patches]
        cov = np.clip(np.asarray(levels) + 0.18 * np.sin(np.pi *
                                                         np.asarray(levels)),
                      0, 1)
        grey = np.clip((1 - cov * 0.96) ** (1 / 2.2) * 255, 0, 255)
        prof, report = linearise.build_profile(levels, grey.tolist(),
                                               "scan grey 0-255")
        assert prof.is_fitted(), "profile did not fit"
        print(f"linearisation        ok (residual "
              f"{report.residual_rms * 100:.2f}%)")

        testsheet.verification_sheet(out / "check.pdf", prof, paper("Letter"))
        print("verification sheet   ok")

        project = Project()
        src = out / "page.png"
        Image.new("L", (400, 300), 128).save(src)
        for _ in range(4):
            project.add_file(src)
        plan = project.build_plan()
        written = Renderer(project).render_print(out / "imposed.pdf", plan)
        assert written and all(Path(p).exists() for p in written), \
            "no imposed PDF was written"
        print(f"imposition and PDF   ok ({len(plan.sheets)} sheets, "
              f"{len(written)} file(s))")

    print("\nall good - this build is complete.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()
    from sigzine.__main__ import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
