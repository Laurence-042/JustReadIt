"""PySide6 debug window for JustReadIt.

Provides a live view of the pipeline:
  - Capture preview with OCR bounding-box overlay
  - Windows OCR text output panel
  - manga-ocr text output panel (GPU only)
  - Hook text panel (placeholder until Frida hook is implemented)
  - Translation panel (placeholder until translators are implemented)

Launch via ``python main.py --debug``.
"""
from __future__ import annotations

import ctypes
import io
import time

from PySide6.QtCore import (
    QObject, QSize, QThread, QTimer,
    Signal, Slot, Qt,
)
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAction, QApplication, QComboBox, QGroupBox, QLabel,
    QMainWindow, QSizePolicy, QSpinBox, QSplitter,
    QStatusBar, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)
from PIL import Image

from src.capture import Capturer
from src.target import GameTarget
from src.ocr.windows_ocr import MissingOcrLanguageError, WindowsOcr, _ensure_apartment
from src.ocr.manga_ocr_engine import GPU_AVAILABLE
from src.ocr.range_detectors import BoundingBox
from .window_picker import WindowPicker


# ---------------------------------------------------------------------------
# Background pipeline worker
# ---------------------------------------------------------------------------

class _PipelineWorker(QObject):
    """Runs capture + OCR on a background QThread.

    Signals
    -------
    result_ready(img_bytes, boxes, win_ocr_text, manga_text, elapsed_ms)
    error(message)
    """

    result_ready = Signal(bytes, list, str, str, float)
    error = Signal(str)

    def __init__(self, target: GameTarget, language_tag: str) -> None:
        super().__init__()
        self._target = target
        self._language_tag = language_tag
        self._capturer: Capturer | None = None
        self._ocr: WindowsOcr | None = None
        self._manga = None  # MangaOcrEngine | None

    # ------------------------------------------------------------------
    # Lifecycle (called on worker thread via QThread.started / explicit slots)
    # ------------------------------------------------------------------

    @Slot()
    def setup(self) -> None:
        """Initialise resources on the worker thread."""
        try:
            self._capturer = Capturer(output_idx=self._target.dxcam_output_idx)
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

        if GPU_AVAILABLE:
            try:
                from src.ocr.manga_ocr_engine import MangaOcrEngine  # deferred – torch before WinRT
                self._manga = MangaOcrEngine()
            except Exception as exc:
                self.error.emit(f"manga-ocr init warning: {exc}")

    @Slot()
    def teardown(self) -> None:
        """Release resources when the thread is stopping."""
        if self._capturer is not None:
            self._capturer.close()
            self._capturer = None

    # ------------------------------------------------------------------
    # Pipeline tick
    # ------------------------------------------------------------------

    @Slot()
    def run_tick(self) -> None:
        """Capture one frame, run OCR, emit results."""
        if self._capturer is None or self._ocr is None:
            return

        t0 = time.monotonic()
        try:
            img: Image.Image = self._capturer.grab_target(self._target)
        except Exception as exc:
            self.error.emit(f"Capture failed: {exc}")
            return

        try:
            boxes: list[BoundingBox] = self._ocr.recognise(img)
        except Exception as exc:
            self.error.emit(f"Windows OCR failed: {exc}")
            boxes = []

        win_ocr_lines = [
            f"[{b.x:4},{b.y:4}  {b.w:3}×{b.h:3}]  {b.text}"
            for b in boxes
        ]
        win_ocr_text = "\n".join(win_ocr_lines)

        manga_text = ""
        if self._manga and boxes:
            xs  = [b.x       for b in boxes]
            ys  = [b.y       for b in boxes]
            x2s = [b.x + b.w for b in boxes]
            y2s = [b.y + b.h for b in boxes]
            crop = img.crop((min(xs), min(ys), max(x2s), max(y2s)))
            try:
                manga_text = self._manga.recognize(crop)
            except Exception as exc:
                manga_text = f"[error: {exc}]"

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Encode frame as JPEG bytes so the PIL object doesn't cross thread boundary.
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        self.result_ready.emit(buf.getvalue(), boxes, win_ocr_text, manga_text, elapsed_ms)


# ---------------------------------------------------------------------------
# Capture preview with bbox overlay
# ---------------------------------------------------------------------------

_BBOX_COLORS = [
    QColor(255,  80,  80),
    QColor( 80, 200,  80),
    QColor( 80, 130, 255),
    QColor(255, 200,  50),
    QColor(200,  80, 255),
    QColor( 80, 220, 220),
]


class _PreviewLabel(QLabel):
    """QLabel subclass that scales the captured frame and draws bbox overlays."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(400, 300))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("No capture yet.\nPick a window and press ▶ Run.")
        self._raw: QPixmap | None = None
        self._boxes: list[BoundingBox] = []
        self._orig_w = 1
        self._orig_h = 1

    def update_frame(self, img_bytes: bytes, boxes: list[BoundingBox]) -> None:
        qimg = QImage.fromData(img_bytes)
        self._raw = QPixmap.fromImage(qimg)
        self._orig_w = qimg.width()
        self._orig_h = qimg.height()
        self._boxes = boxes
        self._render()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._raw is not None:
            self._render()

    def _render(self) -> None:
        if self._raw is None:
            return
        lw, lh = self.width(), self.height()

        # Scale the raw capture to fit the label while keeping aspect ratio.
        scaled = self._raw.scaled(
            lw, lh,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        sx = scaled.width()  / self._orig_w
        sy = scaled.height() / self._orig_h
        ox = (lw - scaled.width())  // 2   # letter-box horizontal offset
        oy = (lh - scaled.height()) // 2   # letter-box vertical offset

        # Draw bounding boxes onto the scaled pixmap.
        if self._boxes:
            painter = QPainter(scaled)
            font = QFont("Consolas", 7)
            painter.setFont(font)
            for i, box in enumerate(self._boxes):
                color = _BBOX_COLORS[i % len(_BBOX_COLORS)]
                painter.setPen(QPen(color, 1))
                rx = int(box.x * sx)
                ry = int(box.y * sy)
                rw = max(1, int(box.w * sx))
                rh = max(1, int(box.h * sy))
                painter.drawRect(rx, ry, rw, rh)
                # Tiny label above the box
                label_w = min(rw, 150)
                painter.fillRect(rx, max(0, ry - 11), label_w, 11, QColor(0, 0, 0, 160))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(rx + 1, max(9, ry - 1), box.text[:24])
            painter.end()

        # Compose onto a dark canvas (letter-box background).
        canvas = QPixmap(lw, lh)
        canvas.fill(QColor(28, 28, 28))
        p2 = QPainter(canvas)
        p2.drawPixmap(ox, oy, scaled)
        p2.end()
        self.setPixmap(canvas)


# ---------------------------------------------------------------------------
# Helper: labelled text panel
# ---------------------------------------------------------------------------

def _make_panel(title: str) -> tuple[QGroupBox, QTextEdit]:
    grp = QGroupBox(title)
    te = QTextEdit()
    te.setReadOnly(True)
    te.setFont(QFont("Consolas", 9))
    lay = QVBoxLayout(grp)
    lay.setContentsMargins(3, 3, 3, 3)
    lay.addWidget(te)
    return grp, te


# ---------------------------------------------------------------------------
# Main debug window
# ---------------------------------------------------------------------------

class DebugWindow(QMainWindow):
    """Full-pipeline debug window.

    Toolbar
    -------
    Pick Window
        Minimises this window and waits for the user to click the game process.
    OCR lang
        Combo of installed Windows OCR languages.
    Interval
        Refresh interval in ms.
    ▶ Run / ■ Stop
        Start / stop the background pipeline worker.

    Panels
    ------
    Left:  Capture preview (scaled, with bbox overlay).
    Right: Windows OCR · manga-ocr · Hook · Translation (stacked vertically).
    """

    # Signal used to trigger a tick on the worker thread without polling.
    _trigger_tick = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt — Debug")
        self.resize(1400, 820)

        self._target: GameTarget | None = None
        self._worker: _PipelineWorker | None = None
        self._worker_thread: QThread | None = None

        self._run_timer = QTimer(self)
        self._run_timer.timeout.connect(self._request_tick)

        self._picker: WindowPicker | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
        from PySide6.QtWidgets import QPushButton
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self._btn_pick = QPushButton("⊕  Pick Window")
        self._btn_pick.setToolTip("Minimises this window; click the game window to select it")
        self._btn_pick.clicked.connect(self._start_picking)
        tb.addWidget(self._btn_pick)
        tb.addSeparator()

        tb.addWidget(QLabel("Target: "))
        self._lbl_target = QLabel("—")
        self._lbl_target.setMinimumWidth(260)
        tb.addWidget(self._lbl_target)
        tb.addSeparator()

        tb.addWidget(QLabel(" OCR lang: "))
        self._cmb_lang = QComboBox()
        self._cmb_lang.setToolTip("Windows OCR language to use")
        self._populate_languages()
        tb.addWidget(self._cmb_lang)
        tb.addSeparator()

        tb.addWidget(QLabel(" Interval: "))
        self._spn_interval = QSpinBox()
        self._spn_interval.setRange(200, 15000)
        self._spn_interval.setValue(1500)
        self._spn_interval.setSuffix(" ms")
        tb.addWidget(self._spn_interval)
        tb.addSeparator()

        act_run = QAction("▶ Run", self)
        act_run.setToolTip("Start the pipeline (requires a target)")
        act_run.triggered.connect(self._run)
        tb.addAction(act_run)

        act_stop = QAction("■ Stop", self)
        act_stop.triggered.connect(self._stop)
        tb.addAction(act_stop)

        # ── Central splitter ───────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        self._preview = _PreviewLabel(self)
        splitter.addWidget(self._preview)

        right = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        manga_title = (
            "manga-ocr  [GPU ready]"
            if GPU_AVAILABLE
            else "manga-ocr  [no CUDA GPU — disabled]"
        )
        grp_wocr, self._te_wocr = _make_panel("Windows OCR")
        grp_mocr, self._te_mocr = _make_panel(manga_title)
        grp_hook, self._te_hook = _make_panel("Hook  (Frida — not yet implemented)")
        grp_tl,   self._te_tl   = _make_panel("Translation  (not yet implemented)")

        if not GPU_AVAILABLE:
            self._te_mocr.setPlaceholderText("Disabled — manga-ocr requires a CUDA GPU.")
        self._te_hook.setPlaceholderText("Frida hook not yet implemented.")
        self._te_tl.setPlaceholderText("Translation plugin not yet implemented.")

        right.addWidget(grp_wocr)
        right.addWidget(grp_mocr)
        right.addWidget(grp_hook)
        right.addWidget(grp_tl)
        right.setSizes([250, 180, 120, 120])

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))

    def _populate_languages(self) -> None:
        """Fill lang combo with available Windows OCR languages."""
        try:
            import winrt.windows.media.ocr as wocr
            _ensure_apartment()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                self._cmb_lang.addItem(f"{tag}  ({lang.display_name})", userData=tag)
            # Prefer ja if installed; otherwise leave at index 0.
            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == "ja":
                    self._cmb_lang.setCurrentIndex(i)
                    break
        except Exception as exc:
            self._cmb_lang.addItem(f"(error: {exc})", userData="en")

    @property
    def _selected_language(self) -> str:
        return self._cmb_lang.currentData() or "en"

    # ------------------------------------------------------------------
    # Window picking
    # ------------------------------------------------------------------

    def _start_picking(self) -> None:
        self.statusBar().showMessage("Click the game window to select it …  (right-click to cancel)")
        self._btn_pick.setEnabled(False)
        from PySide6.QtGui import QCursor
        QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
        self.showMinimized()
        self._picker = WindowPicker(self)
        self._picker.picked.connect(self._on_window_picked)
        self._picker.cancelled.connect(self._on_pick_cancelled)
        # Short delay so the minimize animation fully completes before we
        # start polling — otherwise we'd catch the original button click.
        QTimer.singleShot(400, self._picker.start)

    @Slot(int)
    def _on_window_picked(self, pid: int) -> None:
        from PySide6.QtGui import QCursor  # noqa: F401 (restoreOverrideCursor is QApplication)
        QApplication.restoreOverrideCursor()
        self.showNormal()
        self._btn_pick.setEnabled(True)
        try:
            target = GameTarget.from_pid(pid)
        except Exception as exc:
            self.statusBar().showMessage(f"GameTarget error: {exc}", 8000)
            return
        self._set_target(target)

    @Slot()
    def _on_pick_cancelled(self) -> None:
        QApplication.restoreOverrideCursor()
        self.showNormal()
        self._btn_pick.setEnabled(True)
        self.statusBar().showMessage("Picking cancelled.", 3000)

    def _set_target(self, target: GameTarget) -> None:
        self._stop()
        self._target = target
        w = target.window_rect.width
        h = target.window_rect.height
        self._lbl_target.setText(
            f"{target.process_name}  (PID {target.pid})  [{w}×{h}]"
        )
        self.statusBar().showMessage(
            f"Target: {target.process_name}  PID={target.pid}"
            f"  output_idx={target.dxcam_output_idx}",
            5000,
        )

    # ------------------------------------------------------------------
    # Pipeline run / stop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        if self._target is None:
            self.statusBar().showMessage("Pick a window first.", 3000)
            return
        self._stop()

        lang = self._selected_language
        self._worker = _PipelineWorker(self._target, lang)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)

        self._worker.result_ready.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._trigger_tick.connect(self._worker.run_tick)
        self._worker_thread.started.connect(self._worker.setup)
        # teardown is connected to aboutToQuit / thread finished
        self._worker_thread.finished.connect(self._worker.teardown)

        self._worker_thread.start()

        interval = self._spn_interval.value()
        self._run_timer.setInterval(interval)
        self._run_timer.start()
        self.statusBar().showMessage(
            f"Running — lang={lang}  interval={interval} ms  GPU={GPU_AVAILABLE}"
        )

    def _stop(self) -> None:
        self._run_timer.stop()
        if self._worker_thread is not None:
            try:
                self._trigger_tick.disconnect()
            except RuntimeError:
                pass  # signal was never connected
            self._worker_thread.quit()
            self._worker_thread.wait(3000)
            self._worker_thread = None
            self._worker = None

    def _request_tick(self) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._trigger_tick.emit()

    # ------------------------------------------------------------------
    # Result / error handlers
    # ------------------------------------------------------------------

    @Slot(bytes, list, str, str, float)
    def _on_result(
        self,
        img_bytes: bytes,
        boxes: list,
        win_ocr_text: str,
        manga_text: str,
        elapsed_ms: float,
    ) -> None:
        self._preview.update_frame(img_bytes, boxes)
        header = f"[ {len(boxes)} boxes  —  {elapsed_ms:.0f} ms ]\n\n"
        self._te_wocr.setPlainText(header + win_ocr_text)
        if manga_text:
            self._te_mocr.setPlainText(manga_text)
        self.statusBar().showMessage(
            f"Last tick: {elapsed_ms:.0f} ms  |  {len(boxes)} boxes"
        )

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.statusBar().showMessage(f"⚠  {message}", 10000)
        self._te_wocr.append(f"\n[worker error] {message}")

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop()
        super().closeEvent(event)
