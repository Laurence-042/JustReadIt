# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Hover-translation controller — background worker thread.

Runs entirely on a :class:`~PySide6.QtCore.QThread`.  The main thread owns
:class:`~src.overlay.TranslationOverlay` and wires signals to it.

Pipeline
--------
1. Poll mouse position every :data:`_POLL_MS` ms.
2. Detect large cursor movement (≥ :data:`_MOVE_THRESHOLD` px) → reset settle
   timer; small movement is ignored.
3. After cursor settles for :data:`_SETTLE_MS` ms: capture → small-area OCR
   probe.  If no text is found near the cursor, go back to idle.
4. On probe text hit: run the full pipeline on the captured frame:
   OCR → range detection → memory scan → Levenshtein correction →
   phash/text cache lookup → translation → cache store.
5. Emit :attr:`translation_ready` with the result.

Freeze mode
-----------
Pressing the configured hotkey (default **F9**) at any time:

1. Captures the current game-window frame.
2. Emits :attr:`freeze_triggered` — the main thread overlay displays the
   screenshot.
3. The overlay emits :attr:`~src.overlay.TranslationOverlay.hover_requested`
   signals as the user moves the mouse over the frozen image.
4. Each hover event runs the full pipeline against the frozen frame.
5. When the overlay is dismissed it emits
   :attr:`~src.overlay.TranslationOverlay.freeze_dismissed`; the controller
   returns to normal hover mode.

Typical wiring::

    from src.controller import HoverController
    from src.overlay import TranslationOverlay
    from PySide6.QtCore import QThread

    overlay = TranslationOverlay()
    ctrl = HoverController(target, translator=translator)
    thread = QThread()
    ctrl.moveToThread(thread)
    thread.started.connect(ctrl.setup)

    ctrl.translation_ready.connect(lambda text, rect, origin:
        overlay.show_translation(text, rect, origin))
    ctrl.freeze_triggered.connect(lambda img, l, t, pid, hwnd:
        overlay.enter_freeze_mode(img, l, t, pid, hwnd))
    overlay.hover_requested.connect(ctrl.on_freeze_hover)
    overlay.freeze_dismissed.connect(ctrl.on_freeze_dismissed)

    thread.start()
"""
from __future__ import annotations

import ctypes
import logging
import math
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from src.cache import PhashCache, TranslationCache
from src.capture import Capturer
from src.correction import best_match
from src.memory import MemoryScanner, pick_needles
from src.ocr.range_detectors import merge_boxes_text, run_detectors
from src.ocr.windows_ocr import MissingOcrLanguageError, WindowsOcr
from src.paths import translations_db_path

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from src.target import GameTarget
    from src.translators.base import Translator

_log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ---------------------------------------------------------------------------
# Tuning parameters
# ---------------------------------------------------------------------------

# Cursor must move at least this many pixels to reset the settle timer.
_MOVE_THRESHOLD: int = 20

# Cursor must remain still for this long (ms) to trigger an OCR probe.
_SETTLE_MS: int = 500

# Timer interval for mouse-position and hotkey polling (ms).
_POLL_MS: int = 80

# Half-size of the small crop used for the fast OCR probe (pixels).
_PROBE_HALF: int = 70


# ---------------------------------------------------------------------------
# HoverController
# ---------------------------------------------------------------------------

class HoverController(QObject):
    """Background controller that drives hover and freeze translation.

    Parameters
    ----------
    target:
        Frozen :class:`~src.target.GameTarget` describing the game process and
        window.  Refreshed from the Win32 API every tick via
        :meth:`~src.target.GameTarget.refresh`.
    language_tag:
        BCP-47 language tag for the Windows OCR engine (e.g. ``"ja"``).
    translator:
        Optional :class:`~src.translators.base.Translator`.  When ``None``
        the pipeline still runs OCR + memory scan but skips translation.
    source_lang:
        BCP-47 source language for the translation backend (e.g. ``"ja"``).
    target_lang:
        BCP-47 target language (e.g. ``"zh-CN"`` or ``"en"``).
    freeze_vk:
        Virtual-key code for the Freeze hotkey.  Default 0x78 = **F9**.
    poll_ms:
        Poll interval in milliseconds.

    Signals
    -------
    translation_ready(text, near_rect, screen_origin)
        Emitted on the worker thread when a translation is available.
        *near_rect* is ``(x, y, w, h)`` in game-capture image space;
        *screen_origin* is ``(left, top)`` of the game window in virtual-
        screen space.
    freeze_triggered(screenshot, window_left, window_top, pid, hwnd)
        Emitted when the freeze hotkey fires.  Arguments are passed directly
        to :meth:`~src.overlay.TranslationOverlay.enter_freeze_mode`.
    error(message)
        Emitted for recoverable errors (e.g. OCR language not installed).
    """

    translation_ready = Signal(str, object, object)   # text, near_rect, screen_origin
    freeze_triggered = Signal(object, int, int, int, int)  # img, left, top, pid, hwnd
    error = Signal(str)

    def __init__(
        self,
        target: "GameTarget",
        language_tag: str = "ja",
        translator: "Translator | None" = None,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
        freeze_vk: int = 0x78,  # VK_F9
        poll_ms: int = _POLL_MS,
    ) -> None:
        super().__init__()
        self._target = target
        self._language_tag = language_tag
        self._translator = translator
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._freeze_vk = freeze_vk
        self._poll_ms = poll_ms

        # Resources — created in setup() on the worker thread
        self._capturer: Capturer | None = None
        self._ocr: WindowsOcr | None = None
        self._scanner: MemoryScanner | None = None
        self._phash_cache = PhashCache()
        self._text_cache: TranslationCache | None = None

        # Mouse-settle tracking
        self._last_pos: tuple[int, int] = (0, 0)
        self._settle_start: float = 0.0
        self._settled = False

        # Freeze mode state
        self._in_freeze = False
        self._freeze_frame: "PILImage | None" = None

        # Freeze hotkey edge-detection: True if key was down last tick
        self._freeze_key_was_down: bool = False

        self._poll_timer: QTimer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @Slot()
    def setup(self) -> None:
        """Initialise all resources on the worker thread and start polling."""
        try:
            self._capturer = Capturer(hmonitor=self._target.hmonitor)
            self._capturer.open()
        except Exception as exc:
            self.error.emit(f"Capturer init failed: {exc}")
            return

        try:
            self._ocr = WindowsOcr(self._language_tag)
        except MissingOcrLanguageError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(f"Windows OCR init failed: {exc}")

        try:
            self._scanner = MemoryScanner(self._target.pid)
        except OSError as exc:
            self.error.emit(f"MemoryScanner init failed (memory scan disabled): {exc}")

        try:
            self._text_cache = TranslationCache(translations_db_path())
        except Exception as exc:
            _log.warning("TranslationCache init failed: %s", exc)

        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        self._last_pos = (pt.x, pt.y)
        self._settle_start = time.monotonic()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_ms)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

    @Slot()
    def teardown(self) -> None:
        """Release all resources.  Call before stopping the worker thread."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self._capturer is not None:
            self._capturer.close()
            self._capturer = None
        if self._scanner is not None:
            self._scanner.close()
            self._scanner = None
        if self._text_cache is not None:
            self._text_cache.close()
            self._text_cache = None

    # ------------------------------------------------------------------
    # Freeze-mode slots (called from main thread via queued connection)
    # ------------------------------------------------------------------

    @Slot(float, float)
    def on_freeze_hover(self, x: float, y: float) -> None:
        """Translate the text under ``(x, y)`` in the frozen frame."""
        if not self._in_freeze or self._freeze_frame is None or self._ocr is None:
            return
        try:
            self._run_pipeline(self._freeze_frame, int(x), int(y))
        except Exception as exc:
            _log.exception("Freeze hover pipeline error: %s", exc)

    @Slot()
    def on_freeze_dismissed(self) -> None:
        """Called when the freeze overlay is closed; resume normal hover mode."""
        self._in_freeze = False
        self._freeze_frame = None
        self._settled = False
        self._settle_start = time.monotonic()

    # ------------------------------------------------------------------
    # Private — poll loop
    # ------------------------------------------------------------------

    @Slot()
    def _poll(self) -> None:
        """Main polling tick — called every ``poll_ms`` ms by QTimer."""
        # ── Freeze hotkey (edge-triggered) ───────────────────────────
        key_down = bool(_user32.GetAsyncKeyState(self._freeze_vk) & 0x8000)
        if key_down and not self._freeze_key_was_down and not self._in_freeze:
            self._freeze_key_was_down = True
            self._trigger_freeze()
            return
        self._freeze_key_was_down = key_down

        if self._in_freeze:
            return  # hover events drive the pipeline in freeze mode

        # ── Mouse settle detection ────────────────────────────────────
        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = pt.x, pt.y

        dx = cx - self._last_pos[0]
        dy = cy - self._last_pos[1]
        dist = math.hypot(dx, dy)

        if dist >= _MOVE_THRESHOLD:
            # Large movement — reset settle timer
            self._last_pos = (cx, cy)
            self._settle_start = time.monotonic()
            self._settled = False
            return

        # ── Check cursor-inside-game-window ──────────────────────────
        wr = self._target.window_rect
        if not (wr.left <= cx < wr.right and wr.top <= cy < wr.bottom):
            return  # cursor is outside the game window

        # ── Settle check ─────────────────────────────────────────────
        elapsed_ms = (time.monotonic() - self._settle_start) * 1000
        if elapsed_ms < _SETTLE_MS:
            return
        if self._settled:
            return  # already ran pipeline for this settle event

        self._settled = True

        # ── Refresh target and capture ────────────────────────────────
        try:
            self._target = self._target.refresh()
        except Exception as exc:
            _log.warning("target.refresh() failed: %s", exc)

        img = self._capture_current()
        if img is None:
            self._settled = False
            return

        # Convert cursor to image coords
        cr = self._target.capture_rect
        img_x = cx - self._target.window_rect.left
        img_y = cy - self._target.window_rect.top
        img_x = max(0, min(img.width - 1, img_x))
        img_y = max(0, min(img.height - 1, img_y))

        # ── Fast OCR probe on small crop ──────────────────────────────
        if self._ocr is not None and not self._probe_has_text(img, img_x, img_y):
            return

        # ── Full pipeline ─────────────────────────────────────────────
        try:
            self._run_pipeline(img, img_x, img_y)
        except Exception as exc:
            _log.exception("Hover pipeline error: %s", exc)

    # ------------------------------------------------------------------
    # Private — freeze trigger
    # ------------------------------------------------------------------

    def _trigger_freeze(self) -> None:
        """Capture current frame and enter freeze mode."""
        try:
            self._target = self._target.refresh()
        except Exception as exc:
            _log.warning("target.refresh() before freeze failed: %s", exc)

        img = self._capture_current()
        if img is None:
            return

        self._in_freeze = True
        self._freeze_frame = img
        wr = self._target.window_rect
        self.freeze_triggered.emit(
            img,
            wr.left,
            wr.top,
            self._target.pid,
            self._target.hwnd,
        )

    # ------------------------------------------------------------------
    # Private — capture helper
    # ------------------------------------------------------------------

    def _capture_current(self) -> "PILImage | None":
        """Grab the game window; re-create Capturer on monitor change."""
        if self._capturer is None:
            return None
        try:
            return self._capturer.grab_target(self._target)
        except ValueError:
            # Window moved to a different monitor — recreate Capturer
            try:
                self._capturer.close()
                self._capturer = Capturer(hmonitor=self._target.hmonitor)
                self._capturer.open()
                return self._capturer.grab_target(self._target)
            except Exception as exc:
                self.error.emit(f"Capture failed (monitor switch): {exc}")
                return None
        except Exception as exc:
            self.error.emit(f"Capture failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Private — probe OCR
    # ------------------------------------------------------------------

    def _probe_has_text(
        self, img: "PILImage", img_x: int, img_y: int
    ) -> bool:
        """Return True if any OCR text is found near ``(img_x, img_y)``."""
        if self._ocr is None:
            return True  # assume text when OCR unavailable
        x0 = max(0, img_x - _PROBE_HALF)
        y0 = max(0, img_y - _PROBE_HALF)
        x1 = min(img.width,  img_x + _PROBE_HALF)
        y1 = min(img.height, img_y + _PROBE_HALF)
        crop = img.crop((x0, y0, x1, y1))
        try:
            boxes, _ = self._ocr.recognise(crop)
        except Exception:
            return False
        return len(boxes) > 0

    # ------------------------------------------------------------------
    # Private — full pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(
        self, img: "PILImage", img_x: int, img_y: int
    ) -> None:
        """Run the full OCR → correct → translate pipeline.

        Results are emitted via :attr:`translation_ready`.  The method is
        synchronous and runs on the worker thread.
        """
        if self._ocr is None:
            return

        # ── Full OCR ──────────────────────────────────────────────────
        try:
            _boxes, line_boxes = self._ocr.recognise(img)
        except Exception as exc:
            _log.warning("OCR failed: %s", exc)
            return

        if not line_boxes:
            return

        # ── Range detection ───────────────────────────────────────────
        region_boxes, _detector_name = run_detectors(line_boxes, img_x, img_y)
        if not region_boxes:
            return

        region_text = merge_boxes_text(region_boxes)
        if not region_text.strip():
            return

        # Bounding rect of the detected region (in image coords)
        xs  = [b.x       for b in region_boxes]
        ys  = [b.y       for b in region_boxes]
        x2s = [b.x + b.w for b in region_boxes]
        y2s = [b.y + b.h for b in region_boxes]
        margin = 8
        crop_x  = max(0, min(xs)  - margin)
        crop_y  = max(0, min(ys)  - margin)
        crop_x2 = min(img.width,  max(x2s) + margin)
        crop_y2 = min(img.height, max(y2s) + margin)
        near_rect = (crop_x, crop_y, crop_x2 - crop_x, crop_y2 - crop_y)

        # ── Phash cache (fast path) ────────────────────────────────────
        crop_img = img.crop((crop_x, crop_y, crop_x2, crop_y2))
        cached = self._phash_cache.get(crop_img)
        if cached is not None:
            wr = self._target.window_rect
            self.translation_ready.emit(cached, near_rect, (wr.left, wr.top))
            return

        # ── Memory scan + Levenshtein correction ──────────────────────
        source_text = region_text
        if self._scanner is not None:
            try:
                needles = pick_needles(region_text)
                results: list = []
                for needle in needles:
                    results = self._scanner.scan(needle)
                    if results:
                        break
                if results:
                    matched = best_match(region_text, [r.text for r in results])
                    if matched is not None:
                        source_text = matched
            except Exception as exc:
                _log.warning("Memory scan/correction failed: %s", exc)

        # ── Text cache (persistent) ────────────────────────────────────
        translation = ""
        if self._text_cache is not None:
            translation = self._text_cache.get(
                source_text, self._source_lang, self._target_lang
            ) or ""

        # ── Translation backend ────────────────────────────────────────
        if not translation and self._translator is not None:
            try:
                translation = self._translator.translate(
                    source_text,
                    source_lang=self._source_lang,
                    target_lang=self._target_lang,
                )
                if self._text_cache is not None and translation:
                    self._text_cache.put(
                        source_text,
                        self._source_lang,
                        self._target_lang,
                        translation,
                    )
            except Exception as exc:
                _log.warning("Translation failed: %s", exc)
                translation = f"[Translation error: {exc}]"

        if not translation:
            _log.debug("No translation for: %r", source_text[:60])
            return

        # ── Phash store ────────────────────────────────────────────────
        self._phash_cache.put(crop_img, translation)

        # ── Emit result ────────────────────────────────────────────────
        wr = self._target.window_rect
        self.translation_ready.emit(translation, near_rect, (wr.left, wr.top))
