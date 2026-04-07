# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Application backend — single owner of all stateful resources.

:class:`AppBackend` holds the :class:`~src.controller.HoverController`,
:class:`~src.knowledge.KnowledgeBase`, active :class:`~src.translators.base.Translator`,
and both overlay widgets.  Views (:class:`~src.ui.main_window.MainWindow`,
:class:`~src.ui.debug_window.DebugWindow`) receive an ``AppBackend`` reference
at construction time, connect to its signals, and call its methods.  Views own
**no** backend resources themselves — no lifecycle flags, no ownership tokens,
no stop-before-open gymnastics.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Slot

from src.config import AppConfig
from src.controller import HoverController
from src.knowledge import KnowledgeBase
from src.overlay import FreezeOverlay, TranslationOverlay
from src.paths import knowledge_db_path
from src.translators.factory import build_translator

if TYPE_CHECKING:
    from src.target import GameTarget
    from src.translators.base import Translator

_log = logging.getLogger(__name__)
_cfg = AppConfig()


class AppBackend(QObject):
    """Owns KnowledgeBase, Translator, HoverController, and overlay widgets.

    All :class:`~src.controller.HoverController` signals are forwarded here so
    that views never need to access the controller directly.  Overlay ↔
    controller wiring is managed internally.

    Typical usage::

        backend = AppBackend()
        main_win  = MainWindow(backend)
        debug_win = DebugWindow(backend)   # both windows, same backend
    """

    # ── Forwarded controller signals ────────────────────────────────────────
    translation_ready = Signal(str, object, object)        # text, near_rect, origin
    freeze_triggered  = Signal(object, int, int, int, int) # img, l, t, pid, hwnd
    dump_triggered    = Signal()
    pipeline_debug    = Signal(object)                     # PipelineResult
    pipeline_progress = Signal(str, object, object)        # step, near_rect, origin
    cursor_moved      = Signal()
    error             = Signal(str)
    ready             = Signal()

    # ── Backend-level signals ────────────────────────────────────────────────
    running_changed = Signal(bool)    # True = pipeline started / False = stopped
    target_changed  = Signal(object)  # GameTarget | None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._target: GameTarget | None = None
        self._translator: Translator | None = None
        self._controller: HoverController | None = None
        self._worker_thread: QThread | None = None
        self._knowledge_base = KnowledgeBase.open(knowledge_db_path())
        self._translation_overlay = TranslationOverlay()
        self._freeze_overlay = FreezeOverlay()
        self._freeze_overlay.dismissed.connect(self._on_freeze_dismissed)
        # Attempt a silent translator build from saved config.
        self.rebuild_translator(silent=True)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def knowledge_base(self) -> KnowledgeBase:
        return self._knowledge_base

    @property
    def translator(self) -> Translator | None:
        return self._translator

    @property
    def target(self) -> GameTarget | None:
        return self._target

    @property
    def translation_overlay(self) -> TranslationOverlay:
        return self._translation_overlay

    @property
    def freeze_overlay(self) -> FreezeOverlay:
        return self._freeze_overlay

    @property
    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.isRunning()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_target(self, target: GameTarget) -> None:
        """Attach to *target* and (re)start the pipeline."""
        self._target = target
        self.target_changed.emit(target)
        self.start()

    def start(self) -> None:
        """(Re)start the controller using the current target and :class:`AppConfig`.

        No-op when :attr:`target` is ``None``.
        """
        if self._target is None:
            return
        self.stop()
        self._controller = HoverController(
            self._target,
            language_tag=_cfg.ocr_language,
            translator=self._translator,
            source_lang=_cfg.ocr_language,
            target_lang=_cfg.translator_target_lang,
            freeze_vk=_cfg.freeze_vk,
            dump_vk=_cfg.dump_vk,
            poll_ms=_cfg.interval_ms,
            continuous=True,
            ocr_max_long_edge=_cfg.ocr_max_size,
            memory_scan_enabled=_cfg.memory_scan_enabled,
        )
        self._worker_thread = QThread(self)
        self._controller.moveToThread(self._worker_thread)

        # Internal overlay handling — the backend fully manages the overlays.
        self._controller.translation_ready.connect(self._on_translation)
        self._controller.pipeline_progress.connect(self._on_pipeline_progress)
        self._controller.freeze_triggered.connect(self._on_freeze_triggered_internal)
        self._controller.cursor_moved.connect(self._translation_overlay.hide)

        # Wire freeze overlay ↔ controller.
        self._freeze_overlay.hover_requested.connect(self._controller.on_freeze_hover)
        self._freeze_overlay.dismissed.connect(self._controller.on_freeze_dismissed)

        # Forward all signals so views only need to connect to the backend.
        self._controller.translation_ready.connect(self.translation_ready)
        self._controller.pipeline_debug.connect(self.pipeline_debug)
        self._controller.pipeline_progress.connect(self.pipeline_progress)
        self._controller.freeze_triggered.connect(self.freeze_triggered)
        self._controller.dump_triggered.connect(self.dump_triggered)
        self._controller.cursor_moved.connect(self.cursor_moved)
        self._controller.error.connect(self.error)
        self._controller.ready.connect(self.ready)

        self._worker_thread.started.connect(self._controller.setup)
        self._worker_thread.finished.connect(self._controller.teardown)
        self._worker_thread.start()
        self.running_changed.emit(True)

    def stop(self) -> None:
        """Stop the controller but keep :attr:`target` intact for a later :meth:`start`."""
        if self._worker_thread is not None:
            try:
                self._freeze_overlay.hover_requested.disconnect(
                    self._controller.on_freeze_hover
                )
                self._freeze_overlay.dismissed.disconnect(
                    self._controller.on_freeze_dismissed
                )
            except RuntimeError:
                pass
            self._worker_thread.quit()
            if not self._worker_thread.wait(3000):
                _log.warning("Worker did not stop in 3 s — terminating.")
                self._worker_thread.terminate()
                self._worker_thread.wait(1000)
            self._worker_thread = None
            self._controller = None
            self.running_changed.emit(False)

    def rebuild_translator(self, *, silent: bool = False) -> str | None:
        """(Re-)build the translator from current :class:`AppConfig`.

        Returns an error message string on failure, ``None`` on success.
        The running controller is updated immediately via
        :meth:`~src.controller.HoverController.set_translator`.
        """
        if _cfg.translator_backend in ("none", ""):
            self._translator = None
        else:
            try:
                self._translator = build_translator(
                    _cfg,
                    knowledge_base=self._knowledge_base,
                    progress=lambda _: None,
                )
            except RuntimeError as exc:
                self._translator = None
                if not silent:
                    return str(exc)
        if self._controller is not None:
            self._controller.set_translator(self._translator)
        return None

    def set_translator(self, translator: Translator | None) -> None:
        """Set the translator directly (e.g. from a debug window with a progress UI).

        Also propagates to the running controller.
        """
        self._translator = translator
        if self._controller is not None:
            self._controller.set_translator(translator)

    def set_poll_interval(self, ms: int) -> None:
        """Persist and push to the running controller without restart."""
        _cfg.interval_ms = ms
        if self._controller is not None:
            self._controller.set_poll_interval(ms)

    def set_freeze_vk(self, vk: int) -> None:
        _cfg.freeze_vk = vk
        if self._controller is not None:
            self._controller.set_freeze_vk(vk)

    def set_dump_vk(self, vk: int) -> None:
        _cfg.dump_vk = vk
        if self._controller is not None:
            self._controller.set_dump_vk(vk)

    def set_memory_scan_enabled(self, enabled: bool) -> None:
        _cfg.memory_scan_enabled = enabled
        if self._controller is not None:
            self._controller.set_memory_scan_enabled(enabled)

    def clear_caches(self) -> None:
        if self._controller is not None:
            self._controller.clear_caches()

    def close(self) -> None:
        """Stop the pipeline and release all resources."""
        self.stop()
        self._translation_overlay.close()
        self._freeze_overlay.close()
        try:
            self._knowledge_base.close()
        except Exception:
            pass

    # ── Internal overlay handling ─────────────────────────────────────────────

    @Slot(str, object, object)
    def _on_translation(
        self, text: str, near_rect: object, screen_origin: object
    ) -> None:
        if self._freeze_overlay.is_active:
            self._freeze_overlay.show_translation(text)
        elif near_rect is not None and screen_origin is not None:
            self._translation_overlay.show_translation(text, near_rect, screen_origin)

    @Slot(str, object, object)
    def _on_pipeline_progress(
        self, step: str, near_rect: object, screen_origin: object
    ) -> None:
        if self._freeze_overlay.is_active:
            self._freeze_overlay.show_translation(f"\u23f3 {step}")
        elif near_rect is not None and screen_origin is not None:
            self._translation_overlay.show_progress(step, near_rect, screen_origin)
        else:
            self._translation_overlay.show_progress(step, None, screen_origin or (0, 0))

    @Slot(object, int, int, int, int)
    def _on_freeze_triggered_internal(
        self, screenshot: object, left: int, top: int, pid: int, hwnd: int
    ) -> None:
        self._freeze_overlay.freeze(screenshot, left, top, pid, hwnd)

    @Slot()
    def _on_freeze_dismissed(self) -> None:
        pass  # The controller handles resume via its own dismissed connection.
