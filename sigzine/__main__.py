"""Entry point: python -m sigzine"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QGuiApplication
    from . import APP_NAME
    from .ui.main_window import MainWindow

    QGuiApplication.setApplicationDisplayName(APP_NAME)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    opening = [a for a in sys.argv[1:] if a.endswith(".sigzine")]
    win = MainWindow(start_dialog=not opening)
    win.show()
    if opening:
        win.open_project(opening[0])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
