#!/usr/bin/env python3
"""JustReadIt — entry point.

Usage::

    python main.py --pid 1234                         # hover translation mode
    python main.py --name Game.exe                    # hover translation mode
    python main.py --pid 1234 --target-lang zh-CN     # translate to Chinese
    python main.py --debug                            # PySide6 debug window
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


def _run_debug() -> int:
    """Launch the PySide6 debug window."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is not installed.\n"
            "Install the [ui] extras:  pip install -e '.[ui]'",
            file=sys.stderr,
        )
        return 1

    _qt_env_setup()
    app = QApplication(sys.argv)
    app.setApplicationName("JustReadIt Debug")

    from src.ui.debug_window import DebugWindow
    window = DebugWindow()
    window.show()
    return app.exec()


def _run_hover(
    pid: int | None,
    name: str | None,
    target_lang: str,
    source_lang: str,
) -> int:
    """Launch the hover-translation overlay (headless mode)."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is not installed.\n"
            "Install the [ui] extras:  pip install -e '.[ui]'",
            file=sys.stderr,
        )
        return 1

    _qt_env_setup()
    app = QApplication(sys.argv)
    app.setApplicationName("JustReadIt")
    # No main window — the overlay is a tool window.
    app.setQuitOnLastWindowClosed(False)

    # ── Resolve GameTarget ────────────────────────────────────────────
    from src.target import (
        AmbiguousProcessNameError, GameTarget,
        ProcessNotFoundError, WindowNotFoundError,
    )
    try:
        if pid is not None:
            target = GameTarget.from_pid(pid)
        elif name is not None:
            target = GameTarget.from_name(name)
        else:
            print(
                "Specify a target with --pid PID or --name PROCESS_NAME.",
                file=sys.stderr,
            )
            return 1
    except AmbiguousProcessNameError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (ProcessNotFoundError, WindowNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Load config ───────────────────────────────────────────────────
    from src.config import AppConfig
    cfg = AppConfig()
    ocr_lang   = cfg.ocr_language
    freeze_vk  = cfg.freeze_vk
    auto_hide  = cfg.overlay_auto_hide_ms
    settle_ms  = cfg.settle_ms

    # ── Build translator ──────────────────────────────────────────────
    from src.translators.factory import build_translator
    translator = None
    try:
        translator = build_translator(cfg)
        if translator is None:
            _log.info("No translation backend configured; running OCR-only mode.")
    except RuntimeError as exc:
        _log.warning("Translator init failed: %s — running OCR-only mode.", exc)

    # ── Create overlay ────────────────────────────────────────────────
    from PySide6.QtCore import QThread
    from src.overlay import TranslationOverlay
    from src.controller import HoverController

    overlay = TranslationOverlay(auto_hide_ms=auto_hide)

    # ── Create and wire controller ────────────────────────────────────
    ctrl = HoverController(
        target=target,
        language_tag=ocr_lang,
        translator=translator,
        source_lang=source_lang,
        target_lang=target_lang,
        freeze_vk=freeze_vk,
    )
    thread = QThread()
    ctrl.moveToThread(thread)
    thread.started.connect(ctrl.setup)
    thread.finished.connect(ctrl.teardown)

    def _on_translation_ready(text: str, near_rect: object, screen_origin: object) -> None:
        if overlay.isVisible() and not overlay._is_freeze_mode:
            # Already showing a freeze overlay — update translation on it
            overlay.show_freeze_translation(text)
        else:
            rx, ry, rw, rh = near_rect  # type: ignore[misc]
            ox, oy = screen_origin       # type: ignore[misc]
            overlay.show_translation(text, (rx, ry, rw, rh), (ox, oy))

    def _on_freeze_triggered(
        img: object, left: int, top: int, fpid: int, hwnd: int
    ) -> None:
        overlay.enter_freeze_mode(img, left, top, fpid, hwnd)  # type: ignore[arg-type]

    ctrl.translation_ready.connect(_on_translation_ready)
    ctrl.freeze_triggered.connect(_on_freeze_triggered)
    ctrl.error.connect(lambda msg: _log.error("Controller error: %s", msg))

    overlay.hover_requested.connect(ctrl.on_freeze_hover)
    overlay.freeze_dismissed.connect(ctrl.on_freeze_dismissed)

    # ── Graceful shutdown ─────────────────────────────────────────────
    def _shutdown() -> None:
        thread.quit()
        thread.wait(3000)

    app.aboutToQuit.connect(_shutdown)

    thread.start()
    _log.info(
        "JustReadIt started — target PID %d (%s), freeze key VK=0x%X",
        target.pid,
        target.process_name,
        freeze_vk,
    )
    return app.exec()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="justreadit",
        description="Hover-translation tool for Light.VN-based RPG games.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Open the PySide6 debug window (capture + OCR preview, bbox overlay).",
    )
    parser.add_argument(
        "--pid",
        type=int,
        metavar="PID",
        help="Target process ID.",
    )
    parser.add_argument(
        "--name",
        metavar="PROCESS",
        help="Target process name (e.g. 'Game.exe').  Raises an error when "
             "multiple processes match; use --pid in that case.",
    )
    parser.add_argument(
        "--target-lang",
        default=None,
        metavar="LANG",
        help="BCP-47 target language for translation (default: from config, "
             "fallback 'zh-CN').  Example: en, zh-CN, ko.",
    )
    parser.add_argument(
        "--source-lang",
        default="ja",
        metavar="LANG",
        help="BCP-47 source language (default: ja).",
    )
    args = parser.parse_args()

    if args.debug:
        sys.exit(_run_debug())
    elif args.pid is not None or args.name is not None:
        # Resolve target_lang: CLI > config > hardcoded fallback
        if args.target_lang:
            target_lang = args.target_lang
        else:
            try:
                from src.config import AppConfig
                target_lang = AppConfig().translator_target_lang or "zh-CN"
            except Exception:
                target_lang = "zh-CN"
        sys.exit(
            _run_hover(
                pid=args.pid,
                name=args.name,
                target_lang=target_lang,
                source_lang=args.source_lang,
            )
        )
    else:
        parser.print_help()
        sys.exit(
            "\n\nSpecify --pid PID or --name PROCESS to start hover translation,\n"
            "or --debug to open the debug window."
        )


if __name__ == "__main__":
    main()
