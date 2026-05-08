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
import dataclasses
import io
import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Generic, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, QTimer, Signal, Slot

from src.cache import PhashCache, PipelineRecord, TranslationCache
from src.capture import Capturer
from src.correction import best_match_with_details
from src.memory import MemoryScanner, pick_needles
from src.ocr.base import MissingOcrEngineError, OcrProvider
from src.ocr.factory import build_ocr_engine
from src.ocr.range_detectors import merge_boxes_text, run_detectors
from src.ocr.windows_ocr import _ensure_apartment
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
# Pipeline result types
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True)
class StepResult(Generic[_T]):
    """One pipeline step's output value paired with its wall-clock duration."""
    value: _T
    ms: float = 0.0


@dataclasses.dataclass(frozen=True)
class OcrOutput:
    """Outputs produced by the Windows OCR step."""
    boxes:      list   # word-level BoundingBox list
    line_boxes: list   # line-level BoundingBox list
    text:       str    # formatted debug text


@dataclasses.dataclass(frozen=True)
class RangeOutput:
    """Outputs produced by the range-detection step."""
    region_text:   str
    detector_name: str
    crop_rect:     tuple[int, int, int, int] | None


@dataclasses.dataclass(frozen=True)
class PipelineResult:
    """All intermediate data from one pipeline run, emitted via
    :attr:`HoverController.pipeline_debug` for debug panels."""
    img_bytes: bytes
    ocr:          StepResult[OcrOutput]
    range_det:    StepResult[RangeOutput]  # 'range_det' avoids shadowing builtin
    scan:         StepResult[str]   # mem_text debug string
    scan_results: list               # list[ScanResult] — raw hits from MemoryScanner
    needle:       str                # needle string that produced the scan hits
    corr:         StepResult[str]   # corrected_text
    translate:    StepResult[str]   # translated_text
    elapsed_ms:   float              # total wall time for the run


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

# Thread-local storage for per-thread OcrProvider instances used by PipelineRunnable.
_THREAD_OCR: threading.local = threading.local()


# ---------------------------------------------------------------------------
# Pipeline signals carrier
# ---------------------------------------------------------------------------

class _PipelineSignals(QObject):
    """Signal carrier shared by all :class:`PipelineRunnable` instances
    belonging to one :class:`HoverController`.

    A separate QObject is used so that signals emitted from QThreadPool
    threads are automatically routed to the controller's worker thread via
    Qt's queued-connection mechanism (the receiver's thread affinity).
    """

    #: Emitted when the full pipeline completes (or a cache hit is found).
    result_ready = Signal(int, object)               # run_id, PipelineRecord
    #: Emitted with a fully-built PipelineResult for the debug panel.
    debug_ready  = Signal(int, object)               # run_id, PipelineResult
    #: Emitted at each slow step so the overlay can show progress.
    progress     = Signal(int, str, object, object)  # run_id, step, near_rect, origin


# ---------------------------------------------------------------------------
# Per-run pipeline executor (QRunnable)
# ---------------------------------------------------------------------------


class PipelineRunnable(QRunnable):
    """Executes one full OCR → memory-scan → translate pipeline run.

    Instantiated by :class:`HoverController` on each cursor settle event
    and submitted to :func:`~PySide6.QtCore.QThreadPool.globalInstance`.
    Results are emitted via *signals* and filtered by *run_id* in the
    controller slots so that stale results from earlier settle events are
    silently discarded when the cursor moves.

    Each QThreadPool worker thread maintains a lazily-created
    :class:`~src.ocr.base.OcrProvider` instance via
    :data:`_THREAD_OCR` thread-local storage, avoiding COM-apartment
    conflicts while allowing concurrent memory-scan + translation runs.
    """

    def __init__(
        self,
        *,
        img: PILImage,
        img_x: int,
        img_y: int,
        language_tag: str,
        ocr_engine: str,
        ocr_max_long_edge: int,
        scanner: "MemoryScanner | None",
        translator: "Translator | None",
        phash_cache: PhashCache,
        text_cache: TranslationCache | None,
        source_lang: str,
        target_lang: str,
        memory_scan_enabled: bool,
        origin: tuple[int, int],
        run_id: int,
        signals: _PipelineSignals,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._img = img
        self._img_x = img_x
        self._img_y = img_y
        self._language_tag = language_tag
        self._ocr_engine = ocr_engine
        self._ocr_max_long_edge = ocr_max_long_edge
        self._scanner = scanner
        self._translator = translator
        self._phash_cache = phash_cache
        self._text_cache = text_cache
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._memory_scan_enabled = memory_scan_enabled
        self._origin = origin
        self._run_id = run_id
        self._signals = signals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_thread_ocr(self) -> "OcrProvider | None":
        """Return (or lazily create) a per-thread :class:`~src.ocr.base.OcrProvider` instance."""
        local = _THREAD_OCR
        tag, edge, engine = self._language_tag, self._ocr_max_long_edge, self._ocr_engine
        if (
            getattr(local, "tag",    None) != tag
            or getattr(local, "edge",   None) != edge
            or getattr(local, "engine", None) != engine
        ):
            try:
                local.ocr: OcrProvider = build_ocr_engine(engine, tag, edge)
                local.tag = tag
                local.edge = edge
                local.engine = engine
            except Exception as exc:
                local.ocr = None  # type: ignore[assignment]
                local.tag = None
                local.edge = None
                local.engine = None
                _log.warning("Thread OCR init failed: %s", exc)
        return getattr(local, "ocr", None)

    def _emit_debug(
        self,
        record: PipelineRecord,
        t0: float,
        scan_results: list | None = None,
    ) -> None:
        """Build a :class:`PipelineResult` from *record* and emit ``debug_ready``."""
        elapsed_ms = (time.monotonic() - t0) * 1000
        buf = io.BytesIO()
        self._img.save(buf, format="JPEG", quality=75)

        ocr_step = StepResult(
            OcrOutput(record.ocr_boxes, record.ocr_line_boxes, record.ocr_debug),
            record.ocr_ms,
        )
        range_step = StepResult(
            RangeOutput(record.region_text, record.detector_name, record.crop_rect),
            record.range_ms,
        )
        if record.from_cache:
            corr_val = (
                f"[cache hit]\n{record.corrected_text}"
                if record.corrected_text
                else record.region_text
            )
        else:
            corr_val = record.corrected_text

        result = PipelineResult(
            img_bytes=buf.getvalue(),
            ocr=ocr_step,
            range_det=range_step,
            scan=StepResult(record.mem_text, record.scan_ms),
            scan_results=scan_results or [],
            needle=record.needle,
            corr=StepResult(corr_val, record.corr_ms),
            translate=StepResult(record.translated_text, record.translate_ms),
            elapsed_ms=elapsed_ms,
        )
        self._signals.debug_ready.emit(self._run_id, result)

    # ------------------------------------------------------------------
    # QRunnable entry point
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: C901
        """Execute the full pipeline; emit results via :attr:`_signals`."""
        _ensure_apartment()
        t0 = time.monotonic()
        img = self._img
        img_x, img_y = self._img_x, self._img_y
        run_id = self._run_id
        origin = self._origin

        # ── Full OCR ─────────────────────────────────────────────────
        ocr = self._get_thread_ocr()
        t = time.monotonic()
        boxes: list = []
        line_boxes: list = []
        if ocr is not None:
            try:
                boxes, line_boxes = ocr.recognise(img)
            except Exception as exc:
                _log.warning("OCR failed in runnable: %s", exc)
        win_ocr_lines = [
            f"[{b.x:4},{b.y:4}  {b.w:3}\u00d7{b.h:3}]  {b.text}"
            for b in boxes
        ]
        lang_info = f"lang={ocr.language_tag}" if ocr else "lang=?"
        ocr_debug = f"[ {lang_info} ]\n" + "\n".join(win_ocr_lines)
        ocr_ms = (time.monotonic() - t) * 1000

        if not line_boxes:
            record = PipelineRecord(
                region_text="", corrected_text="", translated_text="",
                ocr_debug=ocr_debug, ocr_boxes=boxes, ocr_line_boxes=line_boxes,
                ocr_ms=ocr_ms,
            )
            self._emit_debug(record, t0)
            return

        # ── Range detection ──────────────────────────────────────────
        t = time.monotonic()
        region_boxes, detector_name = run_detectors(line_boxes, img_x, img_y)
        region_text = merge_boxes_text(region_boxes) if region_boxes else ""
        crop_rect: tuple[int, int, int, int] | None = None
        if region_boxes and region_text.strip():
            xs  = [b.x       for b in region_boxes]
            ys  = [b.y       for b in region_boxes]
            x2s = [b.x + b.w for b in region_boxes]
            y2s = [b.y + b.h for b in region_boxes]
            margin = 8
            crop_rect = (
                max(0, min(xs)  - margin),
                max(0, min(ys)  - margin),
                min(img.width,  max(x2s) + margin),
                min(img.height, max(y2s) + margin),
            )
        range_ms = (time.monotonic() - t) * 1000

        near_rect = (
            crop_rect[0],
            crop_rect[1],
            crop_rect[2] - crop_rect[0],
            crop_rect[3] - crop_rect[1],
        ) if crop_rect else (0, 0, 0, 0)

        if not region_text.strip():
            record = PipelineRecord(
                region_text="", corrected_text="", translated_text="",
                ocr_debug=ocr_debug, ocr_boxes=boxes, ocr_line_boxes=line_boxes,
                detector_name=detector_name, crop_rect=crop_rect,
                ocr_ms=ocr_ms, range_ms=range_ms,
            )
            self._emit_debug(record, t0)
            return

        # ── Phash cache check (by region_text) ───────────────────────
        cached = self._phash_cache.get(region_text)
        if cached is not None:
            hit = PipelineRecord(
                region_text=region_text,
                corrected_text=cached.corrected_text,
                translated_text=cached.translated_text,
                near_rect=near_rect,
                memory_hits=cached.memory_hits,
                needle=cached.needle,
                ocr_debug=ocr_debug,
                ocr_boxes=boxes,
                ocr_line_boxes=line_boxes,
                detector_name=detector_name,
                crop_rect=crop_rect,
                mem_text=f"[cache hit]\n{cached.mem_text}",
                from_cache=True,
                ocr_ms=ocr_ms,
                range_ms=range_ms,
            )
            self._emit_debug(hit, t0)
            self._signals.result_ready.emit(run_id, hit)
            return

        # ── Memory scan + Levenshtein correction ─────────────────────
        self._signals.progress.emit(run_id, "正在扫描内存\u2026", near_rect, origin)
        mem_text = ""
        corrected_text = region_text
        scan_ms = corr_ms = 0.0
        results: list = []
        used_needle = ""
        if self._scanner is not None and region_text and self._memory_scan_enabled:
            try:
                needles = pick_needles(region_text)
                t = time.monotonic()
                used_needle, results = self._scanner.scan_any(needles)
                scan_ms = (time.monotonic() - t) * 1000

                t = time.monotonic()
                candidates = [r.text for r in results]
                matched = best_match_with_details(region_text, candidates, used_needle)
                corr_ms = (time.monotonic() - t) * 1000
                if matched is not None:
                    enc = results[0].encoding if results else "?"
                    corrected_text = matched.text
                    previews = "\n\n".join(r.text[:400] for r in results[:5])
                    mem_text = (
                        f"[match \u2713  enc={enc}  "
                        f"hits={len(results)}  "
                        f"needle={used_needle!r}  "
                        f"tried={len(needles)}  "
                        f"phase={matched.phase}  "
                        f"score={matched.score:.1f}/{matched.threshold:.1f}]"
                        f"\n\n{previews}"
                    )
                elif results:
                    previews = "\n".join(
                        f"  [{r.encoding}] {r.text[:200]!r}" for r in results[:5]
                    )
                    mem_text = (
                        f"[no match  hits={len(results)}  "
                        f"needle={used_needle!r}  "
                        f"tried={len(needles)}]\n{previews}"
                    )
                elif needles:
                    mem_text = f"[no hits  needles={needles!r}]"
                else:
                    mem_text = "[no needles from OCR text]"
            except Exception as exc:
                mem_text = f"[scan error: {exc}]"
                corrected_text = region_text

        # ── Phash cache check (by corrected_text) ────────────────────
        if corrected_text != region_text:
            cached = self._phash_cache.get(corrected_text)
            if cached is not None:
                hit = PipelineRecord(
                    region_text=region_text,
                    corrected_text=cached.corrected_text,
                    translated_text=cached.translated_text,
                    near_rect=near_rect,
                    memory_hits=cached.memory_hits,
                    needle=used_needle or cached.needle,
                    ocr_debug=ocr_debug,
                    ocr_boxes=boxes,
                    ocr_line_boxes=line_boxes,
                    detector_name=detector_name,
                    crop_rect=crop_rect,
                    mem_text=f"[cache hit (corrected)]\n{mem_text}",
                    from_cache=True,
                    ocr_ms=ocr_ms,
                    range_ms=range_ms,
                    scan_ms=scan_ms,
                    corr_ms=corr_ms,
                )
                self._emit_debug(hit, t0, scan_results=results)
                self._signals.result_ready.emit(run_id, hit)
                return

        # ── Text cache (persistent) ───────────────────────────────────
        translation = ""
        if self._text_cache is not None:
            translation = (
                self._text_cache.get(corrected_text, self._source_lang, self._target_lang)
                or ""
            )

        # ── Translation backend ───────────────────────────────────────
        t = time.monotonic()
        if not translation and self._translator is not None and corrected_text:
            self._signals.progress.emit(run_id, "正在翻译\u2026", near_rect, origin)
            try:
                translation = self._translator.translate(
                    corrected_text,
                    source_lang=self._source_lang,
                    target_lang=self._target_lang,
                )
                if self._text_cache is not None and translation:
                    self._text_cache.put(
                        corrected_text,
                        self._source_lang,
                        self._target_lang,
                        translation,
                    )
            except Exception as exc:
                _log.warning("Translation failed: %s", exc)
                translation = f"[translation error: {exc}]"
        translate_ms = (time.monotonic() - t) * 1000

        # ── Build and emit final record ───────────────────────────────
        record = PipelineRecord(
            region_text=region_text,
            corrected_text=corrected_text,
            translated_text=translation,
            near_rect=near_rect,
            memory_hits=[r.text for r in results],
            needle=used_needle,
            ocr_debug=ocr_debug,
            ocr_boxes=boxes,
            ocr_line_boxes=line_boxes,
            detector_name=detector_name,
            crop_rect=crop_rect,
            mem_text=mem_text,
            from_cache=False,
            ocr_ms=ocr_ms,
            range_ms=range_ms,
            scan_ms=scan_ms,
            corr_ms=corr_ms,
            translate_ms=translate_ms,
        )
        self._emit_debug(record, t0, scan_results=results)
        if translation and not translation.startswith("["):
            self._signals.result_ready.emit(run_id, record)


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
    continuous:
        When ``True`` the controller runs the full pipeline on **every** poll
        tick (no settle detection or OCR probe gate).  Intended for debug/UI
        use where live feedback is preferred over conservative API usage.

    Signals
    -------
    translation_ready(text, near_rect, screen_origin)
        Emitted on the worker thread when a translation is available.
        *near_rect* is ``(x, y, w, h)`` in game-capture image space;
        *screen_origin* is ``(left, top)`` of the game window in virtual-
        screen space.
    freeze_triggered(screenshot, window_left, window_top, pid, hwnd)
        Emitted when the freeze hotkey fires.  Arguments are passed directly
        to :meth:`~src.overlay.FreezeOverlay.freeze`.
    pipeline_debug(result)
        Emitted after every pipeline run with a :class:`PipelineResult`
        containing all intermediate data for debug panels.
        Only useful when a UI consumes it — no-ops otherwise.
    error(message)
        Emitted for recoverable errors (e.g. OCR language not installed).
    ready()
        Emitted after :meth:`setup` completes successfully.
    """

    translation_ready = Signal(str, object, object)   # text, near_rect, screen_origin
    freeze_triggered = Signal(object, int, int, int, int)  # img, left, top, pid, hwnd
    dump_triggered = Signal()           # debug-dump hotkey pressed
    pipeline_debug = Signal(object)     # PipelineResult — for debug panels
    pipeline_record = Signal(object)    # PipelineRecord — for dataset recording
    pipeline_progress = Signal(str, object, object)  # step_label, near_rect, screen_origin
    cursor_moved = Signal()  # emitted on large cursor movement; hide overlay before next capture
    paused_changed = Signal(bool)  # True = paused, False = resumed
    error = Signal(str)
    ready = Signal()

    def __init__(
        self,
        target: "GameTarget",
        language_tag: str = "ja",
        translator: "Translator | None" = None,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
        freeze_vk: int = 0x78,  # VK_F9
        dump_vk: int = 0x77,    # VK_F8
        poll_ms: int = _POLL_MS,
        continuous: bool = False,
        ocr_engine: str = "windows",
        ocr_max_long_edge: int = 1920,
        memory_scan_enabled: bool = True,
    ) -> None:
        super().__init__()
        self._target = target
        self._language_tag = language_tag
        self._translator = translator
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._freeze_vk = freeze_vk
        self._dump_vk = dump_vk
        self._poll_ms = poll_ms
        self._continuous = continuous
        self._ocr_engine = ocr_engine
        self._ocr_max_long_edge = ocr_max_long_edge
        self._memory_scan_enabled: bool = memory_scan_enabled
        self._paused: bool = False

        # Resources — created in setup() on the worker thread
        self._capturer: Capturer | None = None
        self._ocr: OcrProvider | None = None
        self._scanner: MemoryScanner | None = None
        self._phash_cache = PhashCache()
        self._text_cache: TranslationCache | None = None

        # Mouse-settle tracking
        self._last_pos: tuple[int, int] = (0, 0)
        self._settle_start: float = 0.0
        # True after a pipeline runnable has been dispatched for the current
        # settle event; reset on cursor movement, pause/resume, freeze dismiss.
        self._dispatched: bool = False

        # Monotonically increasing counter; each cursor settle increments it.
        # PipelineRunnable embeds the run_id at creation; controller slots
        # discard results where run_id != _current_run_id (last-write-wins).
        self._current_run_id: int = 0

        # Signal carrier shared by all runnables; created in setup().
        self._pipeline_signals: _PipelineSignals | None = None

        # Freeze hotkey edge-detection: True if key was down last tick
        self._freeze_key_was_down: bool = False
        # Dump hotkey edge-detection
        self._dump_key_was_down: bool = False

        self._poll_timer: QTimer | None = None
        self._hotkey_timer: QTimer | None = None

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
            self._ocr = build_ocr_engine(
                self._ocr_engine,
                self._language_tag,
                self._ocr_max_long_edge,
            )
        except MissingOcrEngineError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(f"OCR init failed: {exc}")

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

        # Initialise the signal carrier and connect its signals to slots on
        # this object (same thread — Qt uses queued connections for emissions
        # that arrive from QThreadPool worker threads).
        self._pipeline_signals = _PipelineSignals()
        self._pipeline_signals.result_ready.connect(self._on_pipeline_result)
        self._pipeline_signals.debug_ready.connect(self._on_pipeline_debug)
        self._pipeline_signals.progress.connect(self._on_pipeline_progress)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_ms)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        # Dedicated fast-poll timer for hotkeys — 100 ms ensures even
        # brief key presses are caught regardless of pipeline interval.
        self._hotkey_timer = QTimer(self)
        self._hotkey_timer.setInterval(100)
        self._hotkey_timer.timeout.connect(self._poll_hotkeys)
        self._hotkey_timer.start()

        self.ready.emit()

    @Slot()
    def teardown(self) -> None:
        """Release all resources.  Call before stopping the worker thread."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self._hotkey_timer is not None:
            self._hotkey_timer.stop()
            self._hotkey_timer = None
        if self._pipeline_signals is not None:
            self._pipeline_signals.result_ready.disconnect(self._on_pipeline_result)
            self._pipeline_signals.debug_ready.disconnect(self._on_pipeline_debug)
            self._pipeline_signals.progress.disconnect(self._on_pipeline_progress)
            self._pipeline_signals = None
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
    # Runtime parameter updates (thread-safe via queued connection)
    # ------------------------------------------------------------------

    @Slot(object)
    def set_translator(self, translator: "Translator | None") -> None:
        """Replace the active translator at runtime."""
        self._translator = translator

    @Slot(int)
    def set_poll_interval(self, ms: int) -> None:
        """Change the poll timer interval while running."""
        self._poll_ms = ms
        if self._poll_timer is not None:
            self._poll_timer.setInterval(ms)

    @Slot(int)
    def set_freeze_vk(self, vk: int) -> None:
        """Change the freeze-mode hotkey virtual-key code."""
        self._freeze_vk = vk

    @Slot(int)
    def set_dump_vk(self, vk: int) -> None:
        """Change the debug-dump hotkey virtual-key code."""
        self._dump_vk = vk

    @Slot(bool)
    def set_memory_scan_enabled(self, enabled: bool) -> None:
        """Enable or disable the ReadProcessMemory scan step at runtime.

        When disabled the pipeline uses OCR text directly, skipping the
        memory-scan and Levenshtein-correction steps.  The scanner object
        remains open so re-enabling takes effect immediately.
        """
        self._memory_scan_enabled = enabled

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        """Pause or resume the translation pipeline.

        When paused the poll timer still runs (hotkeys remain responsive)
        but ``_poll`` returns immediately without capturing or translating.
        """
        if self._paused == paused:
            return
        self._paused = paused
        self.paused_changed.emit(paused)
        _log.info("Pipeline %s.", "paused" if paused else "resumed")
        if not paused:
            # Reset dispatch state so the next settle triggers a fresh run.
            self._dispatched = False
            self._settle_start = time.monotonic()

    @property
    def paused(self) -> bool:
        """Whether the translation pipeline is currently paused."""
        return self._paused

    # ------------------------------------------------------------------
    # Freeze-mode slots (called from main thread via queued connection)
    # ------------------------------------------------------------------

    @Slot(float, float)
    def on_freeze_hover(self, x: float, y: float) -> None:  # noqa: ARG002
        """No-op: the regular poll loop captures the freeze overlay via DXGI,
        so no special freeze-hover pipeline path is needed."""

    @Slot()
    def on_freeze_dismissed(self) -> None:
        """Called when the freeze overlay is closed; resume normal hover mode."""
        self._dispatched = False
        self._settle_start = time.monotonic()

    @Slot()
    def clear_caches(self) -> None:
        """Flush both the in-memory phash cache and the persistent translation
        cache.  Safe to call from the main thread via a queued connection."""
        self._phash_cache.clear()
        if self._text_cache is not None:
            self._text_cache.clear()
        _log.info("Translation caches cleared.")

    # ------------------------------------------------------------------
    # Private — poll loop
    # ------------------------------------------------------------------

    @Slot()
    def _poll_hotkeys(self) -> None:
        """Fast hotkey poll — runs every 100 ms independent of pipeline interval."""
        # ── Freeze hotkey (edge-triggered) ─────────────────────────────────────────
        # Always emit freeze_triggered on a rising edge; AppBackend decides whether
        # to show a new freeze frame or dismiss the existing overlay.
        key_down = bool(_user32.GetAsyncKeyState(self._freeze_vk) & 0x8000)
        if key_down and not self._freeze_key_was_down:
            self._trigger_freeze()
        self._freeze_key_was_down = key_down
        # ── Debug-dump hotkey (edge-triggered) ─────────────────
        dump_down = bool(_user32.GetAsyncKeyState(self._dump_vk) & 0x8000)
        if dump_down and not self._dump_key_was_down:
            self.dump_triggered.emit()
        self._dump_key_was_down = dump_down

    @Slot()
    def _poll(self) -> None:
        """Main polling tick — called every ``poll_ms`` ms by QTimer."""
        if self._paused:
            return
        # Always refresh target geometry first so cursor hit-testing and
        # overlay origin use up-to-date window coordinates after moving the
        # game window across monitors.
        try:
            self._target = self._target.refresh()
        except Exception as exc:
            _log.warning("target.refresh() failed: %s", exc)

        if self._continuous:
            # Continuous mode: always capture and dispatch a runnable.
            img = self._capture_current()
            if img is None:
                return
            pt = _POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            wr = self._target.window_rect
            img_x = pt.x - wr.left
            img_y = pt.y - wr.top
            if not (0 <= img_x < img.width and 0 <= img_y < img.height):
                img_x = img.width // 2
                img_y = int(img.height * 0.75)
            self._dispatch_pipeline(img, img_x, img_y)
            return

        # ── Mouse settle detection ────────────────────────────────────
        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = pt.x, pt.y

        dx = cx - self._last_pos[0]
        dy = cy - self._last_pos[1]
        dist = math.hypot(dx, dy)

        if dist >= _MOVE_THRESHOLD:
            # Large movement — reset settle timer and notify overlay to hide
            # *before* the next capture so the bubble is not caught by DXGI.
            self._last_pos = (cx, cy)
            self._settle_start = time.monotonic()
            self._dispatched = False
            self.cursor_moved.emit()
            return

        # ── Check cursor-inside-game-window ──────────────────────────
        wr = self._target.window_rect
        if not (wr.left <= cx < wr.right and wr.top <= cy < wr.bottom):
            return  # cursor is outside the game window

        # ── Settle check ─────────────────────────────────────────────
        elapsed_ms = (time.monotonic() - self._settle_start) * 1000
        if elapsed_ms < _SETTLE_MS:
            return
        if self._dispatched:
            return  # already dispatched a runnable for this settle event

        self._dispatched = True

        img = self._capture_current()
        if img is None:
            self._dispatched = False
            return

        # Convert cursor to image coords
        img_x = cx - self._target.window_rect.left
        img_y = cy - self._target.window_rect.top
        img_x = max(0, min(img.width - 1, img_x))
        img_y = max(0, min(img.height - 1, img_y))

        # ── Fast OCR probe on small crop ──────────────────────────────
        if self._ocr is not None and not self._probe_has_text(img, img_x, img_y):
            return

        # ── Dispatch full pipeline to thread pool ─────────────────────
        self._dispatch_pipeline(img, img_x, img_y)

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
    # Private — pipeline dispatch
    # ------------------------------------------------------------------

    def _dispatch_pipeline(
        self, img: "PILImage", img_x: int, img_y: int
    ) -> None:
        """Submit a :class:`PipelineRunnable` to the global thread pool."""
        if self._pipeline_signals is None:
            return
        self._current_run_id += 1
        wr = self._target.window_rect
        runnable = PipelineRunnable(
            img=img,
            img_x=img_x,
            img_y=img_y,
            language_tag=self._language_tag,
            ocr_engine=self._ocr_engine,
            ocr_max_long_edge=self._ocr_max_long_edge,
            scanner=self._scanner,
            translator=self._translator,
            phash_cache=self._phash_cache,
            text_cache=self._text_cache,
            source_lang=self._source_lang,
            target_lang=self._target_lang,
            memory_scan_enabled=self._memory_scan_enabled,
            origin=(wr.left, wr.top),
            run_id=self._current_run_id,
            signals=self._pipeline_signals,
        )
        QThreadPool.globalInstance().start(runnable)

    # ------------------------------------------------------------------
    # Private — pipeline result slots (run on worker thread via queued connection)
    # ------------------------------------------------------------------

    @Slot(int, object)
    def _on_pipeline_result(self, run_id: int, record: object) -> None:
        """Handle a completed pipeline run: update cache and emit signals."""
        if run_id != self._current_run_id:
            return  # stale result from an earlier settle event
        if not isinstance(record, PipelineRecord):
            return
        if not record.from_cache:
            # Write to phash cache (indexes both region_text and corrected_text).
            self._phash_cache.put(record)
        if record.translated_text:
            wr = self._target.window_rect
            self.translation_ready.emit(
                record.translated_text, record.near_rect, (wr.left, wr.top),
            )
        # Forward to backend for dataset recording (backend filters by from_cache).
        self.pipeline_record.emit(record)

    @Slot(int, object)
    def _on_pipeline_debug(self, run_id: int, result: object) -> None:
        """Forward a PipelineResult to the pipeline_debug signal."""
        if run_id != self._current_run_id:
            return
        self.pipeline_debug.emit(result)

    @Slot(int, str, object, object)
    def _on_pipeline_progress(
        self, run_id: int, step: str, near_rect: object, origin: object
    ) -> None:
        """Forward a pipeline progress update to the pipeline_progress signal."""
        if run_id != self._current_run_id:
            return
        self.pipeline_progress.emit(step, near_rect, origin)

