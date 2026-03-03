#!/usr/bin/env python3
"""JustReadIt — entry point.

Usage::

    python main.py --debug     # PySide6 debug / test window
    python main.py             # headless mode (not yet implemented)
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="justreadit",
        description="Hover-translation tool for Light.VN-based RPG games.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Open the PySide6 debug window (capture + OCR preview, bbox overlay).",
    )
    args = parser.parse_args()

    if args.debug:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            sys.exit(
                "PySide6 is not installed.\n"
                "Install the [ui] extras:  pip install -e '.[ui]'"
            )
        # Qt 6 tries to call SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)
        # at QApplication creation.  On some systems another component (e.g. a
        # D3D/CUDA DLL loaded earlier) has already sealed the process DPI setting
        # to the same value, so Qt's redundant call returns ACCESS_DENIED and
        # logs a noisy but harmless warning.  Silence it via Qt's logging filter.
        os.environ.setdefault(
            "QT_LOGGING_RULES", "qt.qpa.window.warning=false"
        )
        app = QApplication(sys.argv)
        app.setApplicationName("JustReadIt Debug")

        from src.ui.debug_window import DebugWindow
        window = DebugWindow()
        window.show()
        sys.exit(app.exec())
    else:
        sys.exit("Headless mode is not yet implemented.  Use --debug for now.")


if __name__ == "__main__":
    main()
