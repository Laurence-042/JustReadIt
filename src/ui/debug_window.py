"""PySide6 debug window for JustReadIt.

Provides a live view of the pipeline:
  - Capture preview with OCR bounding-box overlay
  - Windows OCR text output panel
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

from src.config import AppConfig

_cfg = AppConfig()
from PySide6.QtGui import (
    QAction, QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)
from PIL import Image

from src.capture import Capturer
from src.target import GameTarget
from src.ocr.windows_ocr import MissingOcrLanguageError, WindowsOcr, _ensure_apartment
from src.ocr.range_detectors import BoundingBox, merge_boxes_text, run_detectors
from src.hook.text_hook import TextHook, HookAttachError, HookScriptError
from src.hook.hook_search import (
    HookCandidate, HookCode, HookSearcher, HookSearchError, score_candidate,
)
from .window_picker import WindowPicker


# ---------------------------------------------------------------------------
# Language capability mapping  (BCP-47 tag → DISM capability name)
# ---------------------------------------------------------------------------

_LANG_CAPABILITIES: dict[str, str] = {
    "ja": "Language.OCR~~~ja-JP~0.0.1.0",
}

# Win32 ShellExecuteEx — used to launch an elevated PowerShell and get its
# process handle so we can poll for completion without blocking the UI thread.
class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize",         ctypes.c_ulong),
        ("fMask",          ctypes.c_ulong),
        ("hwnd",           ctypes.c_void_p),
        ("lpVerb",         ctypes.c_wchar_p),
        ("lpFile",         ctypes.c_wchar_p),
        ("lpParameters",   ctypes.c_wchar_p),
        ("lpDirectory",    ctypes.c_wchar_p),
        ("nShow",          ctypes.c_int),
        ("hInstApp",       ctypes.c_void_p),
        ("lpIDList",       ctypes.c_void_p),
        ("lpClass",        ctypes.c_wchar_p),
        ("hkeyClass",      ctypes.c_void_p),
        ("dwHotKey",       ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess",       ctypes.c_void_p),
    ]

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_TIMEOUT            = 0x00000102
_kernel32_ui = ctypes.WinDLL("kernel32", use_last_error=True)
_user32_ui   = ctypes.WinDLL("user32",   use_last_error=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ---------------------------------------------------------------------------
# Background pipeline worker
# ---------------------------------------------------------------------------

class _PipelineWorker(QObject):
    """Runs capture + OCR on a background QThread.

    Signals
    -------
    result_ready(img_bytes, boxes, win_ocr_text, elapsed_ms)
    error(message)
    """

    result_ready = Signal(bytes, list, object, str, str, str, float)  # img, boxes, crop_rect|None, win_ocr, region_text, hook_text, ms
    error = Signal(str)
    ready = Signal()  # emitted once setup() completes — used to defer timer start

    def __init__(self, target: GameTarget, language_tag: str, *, diagnostic: bool = False, hook_code: HookCode | None = None) -> None:
        super().__init__()
        self._target = target
        self._language_tag = language_tag
        self._diagnostic = diagnostic
        self._hook_code = hook_code
        self._capturer: Capturer | None = None
        self._ocr: WindowsOcr | None = None
        self._hook: TextHook | None = None
        self._hook_error: str = ""  # persists across ticks so the panel stays informative

    # ------------------------------------------------------------------
    # Lifecycle (called on worker thread via QThread.started / explicit slots)
    # ------------------------------------------------------------------

    @Slot()
    def setup(self) -> None:
        """Initialise resources on the worker thread."""
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
            self._hook = TextHook(self._target.pid, diagnostic=self._diagnostic, hook_code=self._hook_code)
            self._hook.attach()
        except (HookAttachError, HookScriptError) as exc:
            self._hook_error = str(exc)
            self.error.emit(f"Hook: {exc}")
        except Exception as exc:
            self._hook_error = f"{type(exc).__name__}: {exc}"
            self.error.emit(f"Hook: {self._hook_error}")

        self.ready.emit()  # signal that all resources are initialised

    @Slot()
    def teardown(self) -> None:
        """Release resources when the thread is stopping."""
        if self._hook is not None:
            self._hook.detach()
            self._hook = None
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

        # Refresh window rect every tick so we always capture the current
        # window position / size (handles window moves, resizes, DPI changes).
        try:
            self._target = self._target.refresh()
        except Exception:
            pass  # use last known position on transient failure

        t0 = time.monotonic()
        try:
            img: Image.Image = self._capturer.grab_target(self._target)
        except ValueError:
            # Target moved to a different monitor.  _target already has the
            # correct hmonitor/capture_rect from the refresh above — just
            # recreate the Capturer for the new output and retry once.
            try:
                self._capturer.close()
                self._capturer = Capturer(hmonitor=self._target.hmonitor)
                self._capturer.open()
                img = self._capturer.grab_target(self._target)
            except Exception as exc:
                self.error.emit(f"Capture failed (monitor switch): {exc}")
                return
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
        lang_info = f"lang={self._ocr.language_tag}" if self._ocr else "lang=?"
        win_ocr_text = f"[ {lang_info} ]\n" + "\n".join(win_ocr_lines)

        # ── Region detection ──────────────────────────────────────────
        region_text = ""
        crop_rect: tuple[int, int, int, int] | None = None
        if boxes:
            _pt = _POINT()
            _user32_ui.GetCursorPos(ctypes.byref(_pt))
            cr = self._target.capture_rect
            cursor_x = _pt.x - cr.left
            cursor_y = _pt.y - cr.top
            # When the cursor is outside the captured frame (e.g. user is
            # hovering over the debug window) fall back to the bottom-centre
            # of the image — where VN dialog boxes typically sit.
            if not (0 <= cursor_x < img.width and 0 <= cursor_y < img.height):
                cursor_x = img.width // 2
                cursor_y = int(img.height * 0.75)

            dialog_boxes = run_detectors(boxes, cursor_x, cursor_y)
            if dialog_boxes:
                region_text = merge_boxes_text(dialog_boxes)
                xs  = [b.x       for b in dialog_boxes]
                ys  = [b.y       for b in dialog_boxes]
                x2s = [b.x + b.w for b in dialog_boxes]
                y2s = [b.y + b.h for b in dialog_boxes]
                margin = 8
                crop_rect = (
                    max(0, min(xs)  - margin),
                    max(0, min(ys)  - margin),
                    min(img.width,  max(x2s) + margin),
                    min(img.height, max(y2s) + margin),
                )

        # ── Hook texts ────────────────────────────────────────────────
        if self._hook_error:
            hook_text = f"[attach failed]\n{self._hook_error}"
        elif self._hook is None:
            hook_text = "[hook not initialised]"
        elif not self._hook.attached:
            hook_text = "[hook detached — target process may have exited]"
        elif self._diagnostic:
            # Diagnostic mode — diag messages are the primary output.
            diag = self._hook.diag
            hook_texts = self._hook.texts
            hook_text = "── DIAGNOSTIC MODE ──\n\n" + (diag or "(waiting for data…)")
            if hook_texts:
                hook_text += "\n\n── Captured Text ──\n" + "\n".join(hook_texts[-50:])
        else:
            diag = self._hook.diag
            hook_texts = self._hook.texts
            if hook_texts:
                header = f"[{diag}]\n\n" if diag else ""
                hook_text = header + "\n".join(hook_texts[-50:])
            else:
                hook_text = f"[attached — no text captured yet]\n\n{diag}" if diag else "[attached — no text captured yet]"

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Encode frame as JPEG bytes so the PIL object doesn't cross thread boundary.
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        self.result_ready.emit(buf.getvalue(), boxes, crop_rect, win_ocr_text, region_text, hook_text, elapsed_ms)


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
        self._crop_rect: tuple[int, int, int, int] | None = None
        self._orig_w = 1
        self._orig_h = 1

    def update_frame(
        self,
        img_bytes: bytes,
        boxes: list[BoundingBox],
        crop_rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        qimg = QImage.fromData(img_bytes)
        self._raw = QPixmap.fromImage(qimg)
        self._orig_w = qimg.width()
        self._orig_h = qimg.height()
        self._boxes = boxes
        self._crop_rect = crop_rect
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

        # Draw the detected region as a dashed yellow rectangle.
        if self._crop_rect is not None:
            cl, ct, cr, cb = self._crop_rect
            painter2 = QPainter(scaled)
            pen = QPen(QColor(255, 255, 100), 2, Qt.PenStyle.DashLine)
            painter2.setPen(pen)
            painter2.drawRect(
                int(cl * sx), int(ct * sy),
                max(1, int((cr - cl) * sx)),
                max(1, int((cb - ct) * sy)),
            )
            painter2.end()

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
# Hook-search dialog
# ---------------------------------------------------------------------------

class _HookSearchDialog(QDialog):
    """Two-phase dialog for discovering engine-specific hook sites.

    Phase 1 -- DLL injection + bulk patch  (starts immediately)
        ``hook_engine.dll`` is injected into the game, functions are scanned
        for prologues and patched.  The status label shows progress; the
        progress bar becomes determinate once patching is complete.

    Phase 2 -- Candidate collection  (automatic, while user plays the game)
        Each time the game calls a patched function with a CJK string on its
        call stack, a candidate appears in the list.  The user simply plays
        the game for ~30 s, then selects the best candidate and clicks OK.
    """

    def __init__(self, target: "GameTarget", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Search Hook Sites")
        self.resize(860, 540)

        self._target = target
        self._searcher: HookSearcher | None = None
        self.selected_code: HookCode | None = None
        self._last_cand_count = -1

        # ── Layout ──────────────────────────────────────────────────
        root = QVBoxLayout(self)

        self._lbl_status = QLabel(
            "Injecting hook DLL and scanning function prologues…"
        )
        self._lbl_status.setWordWrap(True)
        root.addWidget(self._lbl_status)

        self._prog = QProgressBar()
        self._prog.setRange(0, 0)   # indeterminate spinner
        self._prog.setFixedHeight(14)
        root.addWidget(self._prog)

        root.addWidget(QLabel(
            "Hook candidates  (play the game — candidates appear automatically):"
        ))
        self._lst = QListWidget()
        self._lst.setFont(QFont("Consolas", 9))
        self._lst.itemDoubleClicked.connect(self._accept_selection)
        root.addWidget(self._lst, 1)

        self._te_diag = QTextEdit()
        self._te_diag.setReadOnly(True)
        self._te_diag.setFont(QFont("Consolas", 8))
        self._te_diag.setFixedHeight(80)
        self._te_diag.setPlaceholderText("DLL diagnostic output…")
        root.addWidget(self._te_diag)

        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.accepted.connect(self._accept_selection)
        self._btn_box.rejected.connect(self.reject)
        self._btn_ok = self._btn_box.button(QDialogButtonBox.StandardButton.Ok)
        self._btn_ok.setEnabled(False)
        root.addWidget(self._btn_box)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)

        self._start()

    # ------------------------------------------------------------------

    def _start(self) -> None:
        try:
            self._searcher = HookSearcher(self._target.pid)
            self._searcher.start()
        except HookSearchError as exc:
            self._lbl_status.setText(f"⚠ Could not start: {exc}")
            self._prog.setVisible(False)
            return
        self._timer.start()

    def _refresh(self) -> None:
        if self._searcher is None:
            return

        diags = self._searcher.diags()
        if diags:
            self._te_diag.setPlainText("\n".join(diags))

        # Patching done → stop spinner
        if self._searcher.scan_complete and self._prog.maximum() == 0:
            self._prog.setRange(0, 1)
            self._prog.setValue(1)
            self._lbl_status.setText(
                "DLL patching complete.  "
                "Play the game — candidates appear as dialogue functions fire.  "
                "Select the best one and click OK."
            )

        # Populate / refresh candidate list
        candidates = self._searcher.ranked_candidates()
        visible = [c for c in candidates if c.score > 0]
        if len(visible) != self._last_cand_count:
            self._last_cand_count = len(visible)
            cur_key = (
                self._lst.currentItem().data(32)
                if self._lst.currentItem()
                else None
            )
            self._lst.clear()
            for c in visible:
                item = QListWidgetItem(c.display_label())
                item.setData(32, c.to_hook_code().to_str())
                self._lst.addItem(item)
                if item.data(32) == cur_key:
                    self._lst.setCurrentItem(item)
            if visible and self._lst.currentItem() is None:
                self._lst.setCurrentRow(0)
            self._btn_ok.setEnabled(bool(visible))

    def _accept_selection(self) -> None:
        item = self._lst.currentItem()
        if item is None and self._lst.count() > 0:
            item = self._lst.item(0)
        if item is not None:
            try:
                self.selected_code = HookCode.from_str(item.data(32))
            except ValueError:
                pass
        self._cleanup()
        self.accept()

    def reject(self) -> None:  # type: ignore[override]
        self._cleanup()
        super().reject()

    def _cleanup(self) -> None:
        self._timer.stop()
        if self._searcher is not None:
            self._searcher.stop()
            self._searcher = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._cleanup()
        super().closeEvent(event)





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
        Right: Windows OCR · Hook · Translation (stacked vertically).
    """

    # Signal used to trigger a tick on the worker thread without polling.
    _trigger_tick = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt — Debug")
        self.resize(1400, 820)

        # Centre on the primary screen, regardless of which screen Qt picks
        # as the default window position.
        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(primary.availableGeometry().center() - self.rect().center())

        self._target: GameTarget | None = None
        self._worker: _PipelineWorker | None = None
        self._worker_thread: QThread | None = None

        self._run_timer = QTimer(self)
        self._run_timer.timeout.connect(self._request_tick)

        self._picker: WindowPicker | None = None

        # Hook search state (auto-starts on pick window)
        self._searcher: HookSearcher | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setInterval(500)
        self._search_timer.timeout.connect(self._refresh_candidates)
        self._last_cand_count: int = -1
        self._last_diag_count: int = 0

        self._install_proc_handle: int | None = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(500)
        self._install_timer.timeout.connect(self._poll_install)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
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

        tb.addSeparator()

        act_diag = QAction("🔍 Diagnose", self)
        act_diag.setToolTip(
            "Run deep diagnostics: enumerate modules, scan exports, "
            "hook glyph/font APIs to determine how the engine renders text"
        )
        act_diag.triggered.connect(self._diagnose)
        tb.addAction(act_diag)

        act_search = QAction("⟳ Re-search", self)
        act_search.setToolTip(
            "Re-run hook search for the current target.  "
            "Search starts automatically when you pick a window."
        )
        act_search.triggered.connect(self._start_search)
        tb.addAction(act_search)

        tb.addSeparator()
        tb.addWidget(QLabel(" Hook: "))
        self._lbl_hook_code = QLabel("(Win32 default)")
        self._lbl_hook_code.setToolTip("Currently configured engine-specific hook (or Win32 default)")
        self._lbl_hook_code.setMaximumWidth(300)
        tb.addWidget(self._lbl_hook_code)

        # ── Restore persisted settings ───────────────────────────────────────────────
        saved_lang = _cfg.ocr_language
        saved_interval = _cfg.interval_ms
        self._spn_interval.setValue(saved_interval)
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # Restore saved hook code label
        saved_hook = _cfg.hook_code
        if saved_hook:
            try:
                hc = HookCode.from_str(saved_hook)
                self._lbl_hook_code.setText(f"+{hc.rva:#x}:{hc.access_pattern} ({hc.encoding})")
                self._lbl_hook_code.setToolTip(saved_hook)
            except ValueError:
                pass

        # ── Install progress bar (hidden until a capability install runs) ──────
        self._install_bar = QWidget()
        _ibl = QHBoxLayout(self._install_bar)
        _ibl.setContentsMargins(6, 3, 6, 3)
        self._install_lbl = QLabel("Installing …")
        self._install_prog = QProgressBar()
        self._install_prog.setRange(0, 0)   # indeterminate spinner
        self._install_prog.setFixedHeight(16)
        _ibl.addWidget(self._install_lbl)
        _ibl.addWidget(self._install_prog, 1)
        self._install_bar.setVisible(False)

        # ── Central splitter ────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        central = QWidget(self)
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self._install_bar)
        central_lay.addWidget(splitter)
        self.setCentralWidget(central)

        self._preview = _PreviewLabel(self)
        splitter.addWidget(self._preview)

        # ── Middle column: OCR / hook output panels ─────────────────────────
        mid = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(mid)

        grp_wocr, self._te_wocr = _make_panel("Windows OCR")
        grp_region, self._te_region = _make_panel("Detected Region")
        grp_hook, self._te_hook = _make_panel("Hook  (Frida)")
        grp_tl,   self._te_tl   = _make_panel("Translation  (not yet implemented)")

        self._te_region.setPlaceholderText("Region text will appear after range detection.")
        self._te_hook.setPlaceholderText("Hook will attach automatically when pipeline starts.")
        self._te_tl.setPlaceholderText("Translation plugin not yet implemented.")

        mid.addWidget(grp_wocr)
        mid.addWidget(grp_region)
        mid.addWidget(grp_hook)
        mid.addWidget(grp_tl)
        mid.setSizes([280, 160, 160, 120])

        # ── Right column: hook candidates ────────────────────────────────────
        grp_cands = QGroupBox("Hook Candidates  (auto-starts on Pick Window)")
        _cands_lay = QVBoxLayout(grp_cands)
        _cands_lay.setContentsMargins(3, 3, 3, 3)
        self._lbl_search_status = QLabel("No active search — pick a window first.")
        self._lbl_search_status.setWordWrap(True)
        _cands_lay.addWidget(self._lbl_search_status)
        self._cands_search = QLineEdit()
        self._cands_search.setPlaceholderText("Filter by text content…")
        self._cands_search.setClearButtonEnabled(True)
        self._cands_search.textChanged.connect(self._apply_cands_filter)
        _cands_lay.addWidget(self._cands_search)
        self._lst_candidates = QListWidget()
        self._lst_candidates.setFont(QFont("Consolas", 9))
        self._lst_candidates.itemDoubleClicked.connect(self._confirm_candidate)
        _cands_lay.addWidget(self._lst_candidates, 1)
        self._te_search_diag = QTextEdit()
        self._te_search_diag.setReadOnly(True)
        self._te_search_diag.setFont(QFont("Consolas", 8))
        self._te_search_diag.setFixedHeight(56)
        self._te_search_diag.setPlaceholderText("DLL diagnostic output…")
        _cands_lay.addWidget(self._te_search_diag)
        _btn_confirm = QPushButton("✓  Confirm selected candidate")
        _btn_confirm.clicked.connect(self._confirm_candidate)
        _cands_lay.addWidget(_btn_confirm)
        splitter.addWidget(grp_cands)

        # preview : mid-col : candidates  →  5 : 3 : 2
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))

        # Connect AFTER populating to avoid a spurious install prompt on startup.
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)

    def _populate_languages(self) -> None:
        """Fill lang combo with available Windows OCR languages.

        Installed languages appear normally.  Languages in ``_LANG_CAPABILITIES``
        that are not yet installed are appended with a "⬇ select to install" marker.
        Default selection is en-US.
        """
        try:
            import winrt.windows.media.ocr as wocr
            import winrt.windows.globalization as glob
            _ensure_apartment()

            installed_tags: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed_tags.add(tag)
                self._cmb_lang.addItem(f"{tag}  ({lang.display_name})", userData=tag)

            # Append auto-installable languages that are not yet present.
            for tag, capability in _LANG_CAPABILITIES.items():
                if tag in installed_tags:
                    continue
                try:
                    display = glob.Language(tag).display_name
                except Exception:
                    display = tag
                self._cmb_lang.addItem(
                    f"{tag}  ({display})  ⬇ select to install via DISM (~6 MB)",
                    userData=tag,
                )

            # Default to en-US.
            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == "en-US":
                    self._cmb_lang.setCurrentIndex(i)
                    break
        except Exception as exc:
            self._cmb_lang.addItem(f"(error: {exc})", userData="en-US")

    @property
    def _selected_language(self) -> str:
        return self._cmb_lang.currentData() or "en-US"

    # ------------------------------------------------------------------
    # Language auto-install
    # ------------------------------------------------------------------

    @Slot(int)
    def _on_lang_changed(self, index: int) -> None:
        """Handle language combo change.

        - If the selected language is not yet installed, offer to install it.
        - If the pipeline is currently running, restart it with the new language.
        """
        tag = self._cmb_lang.itemData(index)
        if not tag:
            return

        # Check whether the language needs installation.
        if tag in _LANG_CAPABILITIES:
            try:
                import winrt.windows.media.ocr as wocr
                import winrt.windows.globalization as glob
                _ensure_apartment()
                if not wocr.OcrEngine.is_language_supported(glob.Language(tag)):
                    self._start_install(tag)
                    return
            except Exception:
                pass

        # Restart the pipeline if it is running so the new language takes effect.
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self.statusBar().showMessage(f"Restarting pipeline with lang={tag} …")
            self._run()

        # Persist the selection.
        _cfg.ocr_language = tag

    def _start_install(self, lang_tag: str) -> None:
        """Ask for confirmation, then launch an elevated PowerShell to install
        the DISM capability for *lang_tag*, and track progress via a timer."""
        capability = _LANG_CAPABILITIES[lang_tag]
        reply = QMessageBox.question(
            self,
            "Install Windows OCR Language Pack",
            f"The OCR language pack for ‘{lang_tag}’ is not installed.\n\n"
            f"Capability:  {capability}\n\n"
            "Install now?  (~6 MB, OCR data only — does not change system language)\n"
            "An administrator (UAC) elevation prompt will appear.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        args = (
            f"-NoProfile -ExecutionPolicy Bypass "
            f"-Command \"Add-WindowsCapability -Online -Name '{capability}'\""
        )
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize     = ctypes.sizeof(sei)
        sei.fMask      = _SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb     = "runas"
        sei.lpFile     = "powershell.exe"
        sei.lpParameters = args
        sei.nShow      = 1  # SW_SHOWNORMAL
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not ok or not sei.hProcess:
            self.statusBar().showMessage(
                "Could not launch installer — UAC denied or PowerShell not found.", 8000
            )
            return

        self._install_proc_handle = sei.hProcess
        self._install_lbl.setText(
            f"Installing {capability} …  "
            "(this may take a minute — do not close this window)"
        )
        self._install_bar.setVisible(True)
        self._install_timer.start()
        self.statusBar().showMessage(f"Installing {capability} …")

    @Slot()
    def _poll_install(self) -> None:
        """Check every 500 ms whether the installer process has exited."""
        if self._install_proc_handle is None:
            self._install_timer.stop()
            return
        result = _kernel32_ui.WaitForSingleObject(
            ctypes.c_void_p(self._install_proc_handle), 0
        )
        if result != _WAIT_TIMEOUT:
            self._finish_install()

    def _finish_install(self) -> None:
        """Called when the installer process exits; hide bar and refresh combo."""
        self._install_timer.stop()
        if self._install_proc_handle is not None:
            _kernel32_ui.CloseHandle(ctypes.c_void_p(self._install_proc_handle))
            self._install_proc_handle = None
        self._install_bar.setVisible(False)

        # Refresh combo to reflect the newly installed capability.
        current_tag = self._cmb_lang.currentData()
        self._cmb_lang.currentIndexChanged.disconnect(self._on_lang_changed)
        self._cmb_lang.clear()
        self._populate_languages()
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == current_tag:
                self._cmb_lang.setCurrentIndex(i)
                break
        self.statusBar().showMessage(
            "Language pack installation complete — press ▶ Run to start.", 8000
        )

    # ------------------------------------------------------------------
    # Window picking
    # ------------------------------------------------------------------

    def _start_picking(self) -> None:
        self.statusBar().showMessage("Click the game window to select it …  (right-click to cancel)")
        self._btn_pick.setEnabled(False)
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
        # Auto-start hook search so candidates accumulate while the user plays.
        self._start_search()

    # ------------------------------------------------------------------
    # Pipeline run / stop
    # ------------------------------------------------------------------

    def _run(self, *, diagnostic: bool = False, hook_code: HookCode | None = None) -> None:
        if self._target is None:
            self.statusBar().showMessage("Pick a window first.", 3000)
            return
        self._stop()

        # If no explicit hook_code provided, load from config.
        if hook_code is None and not diagnostic:
            saved = _cfg.hook_code
            if saved:
                try:
                    hook_code = HookCode.from_str(saved)
                except ValueError:
                    pass

        lang = self._selected_language
        self._worker = _PipelineWorker(self._target, lang, diagnostic=diagnostic, hook_code=hook_code)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)

        self._worker.result_ready.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.ready.connect(self._on_worker_ready)
        self._trigger_tick.connect(self._worker.run_tick)
        self._worker_thread.started.connect(self._worker.setup)
        # teardown is connected to aboutToQuit / thread finished
        self._worker_thread.finished.connect(self._worker.teardown)

        self._worker_thread.start()
        # Timer is started by _on_worker_ready once setup() completes.
        self.statusBar().showMessage(
            f"Starting — lang={lang}  interval={self._spn_interval.value()} ms"
            + ("  [DIAGNOSTIC]" if diagnostic else "")
        )

    def _diagnose(self) -> None:
        """Start the pipeline in diagnostic mode."""
        self._run(diagnostic=True)

    def _start_search(self) -> None:
        """Start or restart the hook search for the current target."""
        self._stop_search()
        if self._target is None:
            self.statusBar().showMessage("Pick a window first.", 3000)
            return
        try:
            self._searcher = HookSearcher(self._target.pid)
            self._searcher.start()
            self._lbl_search_status.setText(
                "Scanning…  play the game — candidates appear as dialogue fires."
            )
            self._last_cand_count = -1
            self._last_diag_count = 0
            self._te_search_diag.clear()
            self._lst_candidates.clear()
            self._search_timer.start()
        except HookSearchError as exc:
            self._lbl_search_status.setText(f"⚠ Search failed: {exc}")
            self.statusBar().showMessage(f"Hook search failed: {exc}", 8000)

    def _stop_search(self) -> None:
        """Stop and clean up the active HookSearcher."""
        self._search_timer.stop()
        if self._searcher is not None:
            self._searcher.stop()
            self._searcher = None

    @Slot()
    def _refresh_candidates(self) -> None:
        """Refresh the candidates list from the active HookSearcher (500 ms tick)."""
        if self._searcher is None:
            return
        # Append only new diag lines (preserves scroll position)
        diags = self._searcher.diags()
        new_lines = diags[self._last_diag_count:]
        if new_lines:
            self._last_diag_count = len(diags)
            sb = self._te_search_diag.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            cursor = self._te_search_diag.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self._te_search_diag.setTextCursor(cursor)
            self._te_search_diag.insertPlainText(
                ("\n" if self._te_search_diag.toPlainText() else "") +
                "\n".join(new_lines)
            )
            if at_bottom:
                sb.setValue(sb.maximum())
        if self._searcher.scan_complete:
            self._lbl_search_status.setText(
                "Scan complete.  Play the game \u2014 candidates update automatically.  "
                "Double-click or select + Confirm."
            )
        candidates = self._searcher.ranked_candidates()  # sorted by RVA ascending
        visible = [c for c in candidates if score_candidate(c.text) > 0]
        if len(visible) == self._last_cand_count:
            return
        self._last_cand_count = len(visible)
        cur_key = (
            self._lst_candidates.currentItem().data(32)
            if self._lst_candidates.currentItem()
            else None
        )
        self._lst_candidates.clear()
        filt = self._cands_search.text().lower()
        for c in visible:
            item = QListWidgetItem(c.display_label())
            item.setData(32, c.to_hook_code().to_str())
            self._lst_candidates.addItem(item)
            if filt and filt not in c.text.lower() and filt not in c.display_label().lower():
                item.setHidden(True)
            if item.data(32) == cur_key:
                self._lst_candidates.setCurrentItem(item)
        if self._lst_candidates.count() > 0 and self._lst_candidates.currentItem() is None:
            self._lst_candidates.setCurrentRow(0)

    def _apply_cands_filter(self) -> None:
        """Re-apply text filter to existing list items without a full rebuild."""
        filt = self._cands_search.text().lower()
        for i in range(self._lst_candidates.count()):
            item = self._lst_candidates.item(i)
            if item is None:
                continue
            label = item.text().lower()
            item.setHidden(bool(filt) and filt not in label)

    @Slot()
    def _confirm_candidate(self) -> None:
        """Confirm the selected candidate as the active hook code and start pipeline."""
        item = self._lst_candidates.currentItem()
        if item is None and self._lst_candidates.count() > 0:
            item = self._lst_candidates.item(0)
        if item is None:
            self.statusBar().showMessage("No candidate selected.", 3000)
            return
        try:
            code = HookCode.from_str(item.data(32))
        except ValueError as exc:
            self.statusBar().showMessage(f"Invalid hook code: {exc}", 5000)
            return
        _cfg.hook_code = code.to_str()
        self._lbl_hook_code.setText(f"+{code.rva:#x}:{code.access_pattern} ({code.encoding})")
        self._lbl_hook_code.setToolTip(code.to_str())
        self._lbl_search_status.setText(f"Confirmed: +{code.rva:#x}:{code.access_pattern}")
        self._stop_search()
        self.statusBar().showMessage(f"Hook confirmed: {code.to_str()} \u2014 starting pipeline\u2026", 6000)
        self._run(hook_code=code)

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

    @Slot()
    def _on_worker_ready(self) -> None:
        """Called (cross-thread) once the worker has finished setup."""
        interval = self._spn_interval.value()
        self._run_timer.setInterval(interval)
        self._run_timer.start()
        lang = self._selected_language
        self.statusBar().showMessage(
            f"Running — lang={lang}  interval={interval} ms"
        )

    def _request_tick(self) -> None:
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._trigger_tick.emit()

    # ------------------------------------------------------------------
    # Result / error handlers
    # ------------------------------------------------------------------

    @Slot(bytes, list, object, str, str, str, float)
    def _on_result(
        self,
        img_bytes: bytes,
        boxes: list,
        crop_rect: object,  # tuple[int,int,int,int] | None
        win_ocr_text: str,
        region_text: str,
        hook_text: str,
        elapsed_ms: float,
    ) -> None:
        self._preview.update_frame(img_bytes, boxes, crop_rect)
        header = f"[ {len(boxes)} boxes  —  {elapsed_ms:.0f} ms ]\n\n"
        self._te_wocr.setPlainText(header + win_ocr_text)
        if region_text:
            self._te_region.setPlainText(region_text)
        self._te_hook.setPlainText(hook_text)
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
        self._stop_search()
        # Persist interval so it survives restarts.
        _cfg.interval_ms = self._spn_interval.value()
        super().closeEvent(event)
