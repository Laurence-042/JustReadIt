#!/usr/bin/env python3
"""JustReadIt — entry point.

Usage::

    python main.py           # compact user window (default)
    python main.py --debug   # full pipeline debug window
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

_log = logging.getLogger(__name__)


def _qt_env_setup() -> None:
    """Suppress the harmless DPI-awareness warning from Qt 6."""
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window.warning=false")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="JustReadIt — hover-translation tool for Light.VN games",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Open the full pipeline debug window instead of the user window.",
    )
    args = parser.parse_args()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is not installed.\n"
            "Install the [ui] extras:  pip install -e '.[ui]'",
            file=sys.stderr,
        )
        sys.exit(1)

    _qt_env_setup()
    app = QApplication(sys.argv)
    app.setApplicationName("JustReadIt")

    from src.app_backend import AppBackend
    backend = AppBackend()

    if args.debug:
        from src.ui.debug_window import DebugWindow
        window = DebugWindow(backend)
    else:
        from src.ui.main_window import MainWindow
        window = MainWindow(backend)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
