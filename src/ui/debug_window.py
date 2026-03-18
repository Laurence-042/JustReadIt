"""PySide6 debug window for JustReadIt.

Provides a live view of the pipeline:
  - Capture preview with OCR bounding-box overlay
  - Windows OCR text output panel
  - Memory scan result panel
    - Levenshtein corrected text panel
  - Translation panel (placeholder until translators are implemented)

Launch via ``python main.py --debug``.
"""
from __future__ import annotations

import ctypes
import io
import logging
import time

from PySide6.QtCore import (
    QObject, QSize, QThread, QTimer,
    Signal, Slot, Qt,
)

from src.config import AppConfig

_cfg = AppConfig()
_log = logging.getLogger(__name__)
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QTextEdit, QToolBar,
    QVBoxLayout, QWidget,
)
from PIL import Image

from src.capture import Capturer
from src.target import GameTarget
from src.ocr.windows_ocr import MissingOcrLanguageError, WindowsOcr, _ensure_apartment
from src.ocr.range_detectors import BoundingBox, merge_boxes_text, run_detectors
from src.memory import MemoryScanner, pick_needles
from src.correction import best_match_with_details
from src.translators.base import Translator
from src.translators.factory import build_translator
from src.translators.openai_translator import DEFAULT_SYSTEM_PROMPT
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
    """Runs capture + OCR + memory scan + translation on a background QThread.

    Signals
    -------
    result_ready(img_bytes, word_boxes, line_boxes, crop_rect, win_ocr_text,
                 region_text, detector_name, mem_text, corrected_text,
                 translated_text, elapsed_ms)
    error(message)
    """

    result_ready = Signal(bytes, list, list, object, str, str, str, str, str, str, float)
    error = Signal(str)
    ready = Signal()

    def __init__(
        self,
        target: GameTarget,
        language_tag: str,
        translator: Translator | None = None,
        target_lang: str = "en",
    ) -> None:
        super().__init__()
        self._target = target
        self._language_tag = language_tag
        self._translator = translator
        self._target_lang = target_lang
        self._capturer: Capturer | None = None
        self._ocr: WindowsOcr | None = None
        self._scanner: MemoryScanner | None = None

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
            self._scanner = MemoryScanner(self._target.pid)
        except OSError as exc:
            self.error.emit(f"MemoryScanner init failed: {exc}")

        self.ready.emit()

    @Slot()
    def teardown(self) -> None:
        """Release resources when the thread is stopping."""
        if self._capturer is not None:
            self._capturer.close()
            self._capturer = None
        if self._scanner is not None:
            self._scanner.close()
            self._scanner = None

    # ------------------------------------------------------------------
    # Pipeline tick
    # ------------------------------------------------------------------

    @Slot()
    def run_tick(self) -> None:
        """Capture one frame, run OCR, run memory scan, emit results."""
        if self._capturer is None or self._ocr is None:
            return

        # Refresh window rect every tick so we always capture the current
        # window position / size (handles window moves, resizes, DPI changes).
        try:
            self._target = self._target.refresh()
        except Exception as exc:
            _log.warning("target.refresh() failed, using last known position: %s", exc)

        t0 = time.monotonic()
        try:
            img: Image.Image = self._capturer.grab_target(self._target)
        except ValueError:
            # Target moved to a different monitor — recreate Capturer.
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
            boxes, line_boxes = self._ocr.recognise(img)
        except Exception as exc:
            self.error.emit(f"Windows OCR failed: {exc}")
            boxes, line_boxes = [], []

        win_ocr_lines = [
            f"[{b.x:4},{b.y:4}  {b.w:3}×{b.h:3}]  {b.text}"
            for b in boxes
        ]
        lang_info = f"lang={self._ocr.language_tag}" if self._ocr else "lang=?"
        win_ocr_text = f"[ {lang_info} ]\n" + "\n".join(win_ocr_lines)

        # ── Region detection ──────────────────────────────────────────
        region_text = ""
        detector_name = ""
        crop_rect: tuple[int, int, int, int] | None = None
        if line_boxes:
            _pt = _POINT()
            _user32_ui.GetCursorPos(ctypes.byref(_pt))
            cr = self._target.capture_rect
            cursor_x = _pt.x - cr.left
            cursor_y = _pt.y - cr.top
            # When the cursor is outside the captured frame, fall back to
            # bottom-centre — where VN dialog boxes typically sit.
            if not (0 <= cursor_x < img.width and 0 <= cursor_y < img.height):
                cursor_x = img.width // 2
                cursor_y = int(img.height * 0.75)

            dialog_boxes, detector_name = run_detectors(line_boxes, cursor_x, cursor_y)
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

        # ── Memory scan ──────────────────────────────────────────────
        mem_text = ""
        corrected_text = region_text
        if region_text and self._scanner is not None:
            try:
                needles = pick_needles(region_text)
                results: list = []
                used_needle = ""
                for needle in needles:
                    results = self._scanner.scan(needle)
                    if results:
                        used_needle = needle
                        break
                    used_needle = needle  # remember last tried

                candidates = [r.text for r in results]
                matched = best_match_with_details(region_text, candidates)
                if matched is not None:
                    enc = results[0].encoding if results else "?"
                    corrected_text = matched.text
                    previews = "\n\n".join(
                        r.text[:400] for r in results[:5]
                    )
                    mem_text = (
                        f"[match ✓  enc={enc}  "
                        f"hits={len(results)}  "
                        f"needle={used_needle!r}  "
                        f"tried={len(needles)}  "
                        f"phase={matched.phase}  "
                        f"score={matched.score:.1f}/{matched.threshold:.1f}]"
                        f"\n\n{previews}"
                    )
                elif results:
                    previews = "\n".join(
                        f"  [{r.encoding}] {r.text[:200]!r}"
                        for r in results[:5]
                    )
                    mem_text = (
                        f"[no match  hits={len(results)}  "
                        f"needle={used_needle!r}  "
                        f"tried={len(needles)}]\n{previews}"
                    )
                elif needles:
                    mem_text = (
                        f"[no hits  needles={needles!r}]"
                    )
                else:
                    mem_text = "[no needles from OCR text]"
            except Exception as exc:
                mem_text = f"[scan error: {exc}]"
                corrected_text = region_text

        # ── Translation ──────────────────────────────────────────────
        translated_text = ""
        if self._translator is not None and corrected_text:
            try:
                translated_text = self._translator.translate(
                    corrected_text, target_lang=self._target_lang
                )
            except Exception as exc:
                translated_text = f"[translation error: {exc}]"

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Encode frame as JPEG bytes so the PIL object doesn't cross
        # thread boundary.
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        self.result_ready.emit(
            buf.getvalue(), boxes, line_boxes, crop_rect,
            win_ocr_text, region_text, detector_name, mem_text, corrected_text,
            translated_text, elapsed_ms,
        )


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

        # Overlay visibility flags (toggled by checkboxes in the UI).
        self.show_image: bool = True
        self.show_boxes: bool = True
        self.show_labels: bool = True
        self.show_region: bool = True
        self.show_lines: bool = True
        self._line_boxes: list[BoundingBox] = []

    def update_frame(
        self,
        img_bytes: bytes,
        boxes: list[BoundingBox],
        line_boxes: list[BoundingBox],
        crop_rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        qimg = QImage.fromData(img_bytes)
        self._raw = QPixmap.fromImage(qimg)
        self._orig_w = qimg.width()
        self._orig_h = qimg.height()
        self._boxes = boxes
        self._line_boxes = line_boxes
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

        if self.show_image:
            scaled = self._raw.scaled(
                lw, lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            # Compute the same size as the scaled image would have, but
            # fill with a dark background instead of the game frame.
            tmp = self._raw.scaled(
                lw, lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            scaled = QPixmap(tmp.width(), tmp.height())
            scaled.fill(QColor(40, 40, 40))

        sx = scaled.width()  / self._orig_w
        sy = scaled.height() / self._orig_h
        ox = (lw - scaled.width())  // 2
        oy = (lh - scaled.height()) // 2

        if self._boxes and (self.show_boxes or self.show_labels):
            painter = QPainter(scaled)
            font = QFont("Consolas", 7)
            painter.setFont(font)
            for i, box in enumerate(self._boxes):
                color = _BBOX_COLORS[i % len(_BBOX_COLORS)]
                rx = int(box.x * sx)
                ry = int(box.y * sy)
                rw = max(1, int(box.w * sx))
                rh = max(1, int(box.h * sy))
                if self.show_boxes:
                    painter.setPen(QPen(color, 1))
                    painter.drawRect(rx, ry, rw, rh)
                if self.show_labels:
                    label_w = min(rw, 150)
                    painter.fillRect(rx, max(0, ry - 11), label_w, 11, QColor(0, 0, 0, 160))
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(rx + 1, max(9, ry - 1), box.text[:24])
            painter.end()

        if self._line_boxes and self.show_lines:
            plines = QPainter(scaled)
            pen_line = QPen(QColor(80, 255, 200), 1, Qt.PenStyle.DotLine)
            plines.setPen(pen_line)
            for lb in self._line_boxes:
                plines.drawRect(
                    int(lb.x * sx), int(lb.y * sy),
                    max(1, int(lb.w * sx)), max(1, int(lb.h * sy)),
                )
            plines.end()

        if self._crop_rect is not None and self.show_region:
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

    Panels
    ------
    Left:  Capture preview (scaled, with bbox overlay).
    Right: Windows OCR · Detected Region · Memory Scan · Translation.
    """

    _trigger_tick = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt — Debug")
        self.resize(1400, 820)

        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(primary.availableGeometry().center() - self.rect().center())

        self._target: GameTarget | None = None
        self._worker: _PipelineWorker | None = None
        self._worker_thread: QThread | None = None
        self._translator: Translator | None = None

        self._run_timer = QTimer(self)
        self._run_timer.timeout.connect(self._request_tick)

        self._picker: WindowPicker | None = None

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
        self._btn_pick.setToolTip(
            "Minimises this window; click the game window to select it"
        )
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

        # ── Restore persisted settings ───────────────────────────────
        saved_lang = _cfg.ocr_language
        saved_interval = _cfg.interval_ms
        self._spn_interval.setValue(saved_interval)
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # ── Install progress bar (hidden until capability install) ───
        self._install_bar = QWidget()
        _ibl = QHBoxLayout(self._install_bar)
        _ibl.setContentsMargins(6, 3, 6, 3)
        self._install_lbl = QLabel("Installing …")
        self._install_prog = QProgressBar()
        self._install_prog.setRange(0, 0)   # indeterminate
        self._install_prog.setFixedHeight(16)
        _ibl.addWidget(self._install_lbl)
        _ibl.addWidget(self._install_prog, 1)
        self._install_bar.setVisible(False)

        # ── Central splitter ───────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        central = QWidget(self)
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self._install_bar)
        central_lay.addWidget(splitter)
        self.setCentralWidget(central)

        # -- Left column: game preview + overlay toggles --
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(2)

        # Overlay-visibility checkboxes.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(4, 2, 4, 0)
        toggle_row.setSpacing(10)

        self._chk_image  = QCheckBox("画面")
        self._chk_lines  = QCheckBox("OCR行")
        self._chk_boxes  = QCheckBox("OCR框")
        self._chk_labels = QCheckBox("OCR结果")
        self._chk_region = QCheckBox("聚合范围")

        for chk in (self._chk_image, self._chk_lines, self._chk_boxes,
                    self._chk_labels, self._chk_region):
            chk.setChecked(True)
            toggle_row.addWidget(chk)
        toggle_row.addStretch()

        left_lay.addLayout(toggle_row)

        self._preview = _PreviewLabel(self)
        left_lay.addWidget(self._preview, 1)  # stretch=1 so preview fills space

        # Wire checkboxes → preview flags; re-render on toggle.
        self._chk_image.toggled.connect(self._on_toggle_image)
        self._chk_lines.toggled.connect(self._on_toggle_lines)
        self._chk_boxes.toggled.connect(self._on_toggle_boxes)
        self._chk_labels.toggled.connect(self._on_toggle_labels)
        self._chk_region.toggled.connect(self._on_toggle_region)

        splitter.addWidget(left)

        # -- Right column: text panels --
        right = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(right)

        grp_wocr,   self._te_wocr   = _make_panel("Windows OCR")
        self._grp_region, self._te_region = _make_panel("Detected Region")
        grp_region = self._grp_region
        grp_mem,    self._te_mem    = _make_panel("Memory Scan")
        grp_corr,   self._te_corr   = _make_panel("Levenshtein Corrected")
        grp_tl,     self._te_tl     = _make_panel("Translation")

        self._te_region.setPlaceholderText(
            "Region text will appear after range detection."
        )
        self._te_mem.setPlaceholderText(
            "Memory scan results appear here.\n"
            "ReadProcessMemory scans the game's heap for OCR text substrings."
        )
        self._te_corr.setPlaceholderText(
            "Corrected text (best OCR\u2194memory match) appears here.\n"
            "Falls back to OCR region text when no confident match is found."
        )
        self._te_tl.setPlaceholderText(
            "Configure a translator backend below and click \"Apply\" to enable."
        )

        right.addWidget(grp_wocr)
        right.addWidget(grp_region)
        right.addWidget(grp_mem)
        right.addWidget(grp_corr)
        right.addWidget(self._build_translator_settings_panel())
        right.addWidget(grp_tl)
        right.setSizes([220, 120, 100, 140, 180, 100])

        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 4)

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))

        # Connect AFTER populating to avoid spurious install prompt.
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)

    # ------------------------------------------------------------------
    # Overlay toggle handlers
    # ------------------------------------------------------------------

    def _on_toggle_image(self, checked: bool) -> None:
        self._preview.show_image = checked
        self._preview._render()

    def _on_toggle_lines(self, checked: bool) -> None:
        self._preview.show_lines = checked
        self._preview._render()

    def _on_toggle_boxes(self, checked: bool) -> None:
        self._preview.show_boxes = checked
        self._preview._render()

    def _on_toggle_labels(self, checked: bool) -> None:
        self._preview.show_labels = checked
        self._preview._render()

    def _on_toggle_region(self, checked: bool) -> None:
        self._preview.show_region = checked
        self._preview._render()

    # ------------------------------------------------------------------
    # Language helpers
    # ------------------------------------------------------------------

    def _populate_languages(self) -> None:
        """Fill lang combo with available Windows OCR languages."""
        try:
            import winrt.windows.media.ocr as wocr
            import winrt.windows.globalization as glob
            _ensure_apartment()

            installed_tags: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed_tags.add(tag)
                self._cmb_lang.addItem(
                    f"{tag}  ({lang.display_name})", userData=tag
                )

            for tag, capability in _LANG_CAPABILITIES.items():
                if tag in installed_tags:
                    continue
                try:
                    display = glob.Language(tag).display_name
                except Exception as exc:
                    _log.debug("Could not get display name for lang %r: %s", tag, exc)
                    display = tag
                self._cmb_lang.addItem(
                    f"{tag}  ({display})  ⬇ select to install via DISM (~6 MB)",
                    userData=tag,
                )

            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == "en-US":
                    self._cmb_lang.setCurrentIndex(i)
                    break
        except Exception as exc:
            self._cmb_lang.addItem(f"(error: {exc})", userData="en-US")

    @property
    def _selected_language(self) -> str:
        return self._cmb_lang.currentData() or "en-US"

    @Slot(int)
    def _on_lang_changed(self, index: int) -> None:
        tag = self._cmb_lang.itemData(index)
        if not tag:
            return

        if tag in _LANG_CAPABILITIES:
            try:
                import winrt.windows.media.ocr as wocr
                import winrt.windows.globalization as glob
                _ensure_apartment()
                if not wocr.OcrEngine.is_language_supported(glob.Language(tag)):
                    self._start_install(tag)
                    return
            except Exception as exc:
                _log.warning("WinRT OCR language check failed for %r: %s", tag, exc)

        if self._worker_thread is not None and self._worker_thread.isRunning():
            self.statusBar().showMessage(
                f"Restarting pipeline with lang={tag} …"
            )
            self._run()

        _cfg.ocr_language = tag

    # ------------------------------------------------------------------
    # Language pack installation
    # ------------------------------------------------------------------

    def _start_install(self, lang_tag: str) -> None:
        capability = _LANG_CAPABILITIES[lang_tag]
        reply = QMessageBox.question(
            self,
            "Install Windows OCR Language Pack",
            f"The OCR language pack for '{lang_tag}' is not installed.\n\n"
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
        sei.cbSize       = ctypes.sizeof(sei)
        sei.fMask        = _SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb       = "runas"
        sei.lpFile       = "powershell.exe"
        sei.lpParameters = args
        sei.nShow        = 1  # SW_SHOWNORMAL
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not ok or not sei.hProcess:
            self.statusBar().showMessage(
                "Could not launch installer — UAC denied or PowerShell not found.",
                8000,
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
        if self._install_proc_handle is None:
            self._install_timer.stop()
            return
        result = _kernel32_ui.WaitForSingleObject(
            ctypes.c_void_p(self._install_proc_handle), 0
        )
        if result != _WAIT_TIMEOUT:
            self._finish_install()

    def _finish_install(self) -> None:
        self._install_timer.stop()
        if self._install_proc_handle is not None:
            _kernel32_ui.CloseHandle(
                ctypes.c_void_p(self._install_proc_handle)
            )
            self._install_proc_handle = None
        self._install_bar.setVisible(False)

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
        self.statusBar().showMessage(
            "Click the game window to select it …  (right-click to cancel)"
        )
        self._btn_pick.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
        self.showMinimized()
        self._picker = WindowPicker(self)
        self._picker.picked.connect(self._on_window_picked)
        self._picker.cancelled.connect(self._on_pick_cancelled)
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
        self._run()

    # ------------------------------------------------------------------
    # Pipeline run / stop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        if self._target is None:
            self.statusBar().showMessage("Pick a window first.", 3000)
            return
        self._stop()

        lang = self._selected_language
        target_lang = _cfg.translator_target_lang
        self._worker = _PipelineWorker(
            self._target, lang, self._translator, target_lang
        )
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)

        self._worker.result_ready.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.ready.connect(self._on_worker_ready)
        self._trigger_tick.connect(self._worker.run_tick)
        self._worker_thread.started.connect(self._worker.setup)
        self._worker_thread.finished.connect(self._worker.teardown)

        self._worker_thread.start()
        self.statusBar().showMessage(
            f"Starting — lang={lang}  interval={self._spn_interval.value()} ms"
        )

    def _stop(self) -> None:
        self._run_timer.stop()
        if self._worker_thread is not None:
            try:
                self._trigger_tick.disconnect()
            except RuntimeError:
                pass
            self._worker_thread.quit()
            self._worker_thread.wait(3000)
            self._worker_thread = None
            self._worker = None

    @Slot()
    def _on_worker_ready(self) -> None:
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

    @Slot(bytes, list, list, object, str, str, str, str, str, str, float)
    def _on_result(
        self,
        img_bytes: bytes,
        boxes: list,
        line_boxes: list,
        crop_rect: object,
        win_ocr_text: str,
        region_text: str,
        detector_name: str,
        mem_text: str,
        corrected_text: str,
        translated_text: str,
        elapsed_ms: float,
    ) -> None:
        self._preview.update_frame(img_bytes, boxes, line_boxes, crop_rect)
        header = f"[ {len(boxes)} boxes  —  {elapsed_ms:.0f} ms ]\n\n"
        self._te_wocr.setPlainText(header + win_ocr_text)
        if detector_name:
            self._grp_region.setTitle(f"Detected Region  [{detector_name}]")
        self._te_region.setPlainText(region_text)
        self._te_mem.setPlainText(mem_text)
        self._te_corr.setPlainText(corrected_text)
        if translated_text:
            self._te_tl.setPlainText(translated_text)
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
        _cfg.interval_ms = self._spn_interval.value()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Translator settings panel
    # ------------------------------------------------------------------

    def _build_translator_settings_panel(self) -> QWidget:
        """Build the collapsible translator configuration group box."""
        grp = QGroupBox("Translation Settings")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Row 1: backend + target lang
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Backend:"))
        self._cmb_backend = QComboBox()
        for label, val in [
            ("\u2014 None \u2014", "none"),
            ("Google Translate (free, no key)", "google_free"),
            ("Google Cloud Translation (API key)", "cloud"),
            ("OpenAI", "openai"),
        ]:
            self._cmb_backend.addItem(label, userData=val)
        row1.addWidget(self._cmb_backend)
        row1.addSpacing(12)
        row1.addWidget(QLabel("Target lang:"))
        self._le_target_lang = QLineEdit()
        self._le_target_lang.setMaximumWidth(70)
        self._le_target_lang.setPlaceholderText("en")
        row1.addWidget(self._le_target_lang)
        row1.addStretch()
        lay.addLayout(row1)

        # Row 2: API key (used by both backends)
        self._row_api_key = QWidget()
        r2 = QHBoxLayout(self._row_api_key)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(QLabel("API Key:"))
        self._le_api_key = QLineEdit()
        self._le_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_api_key.setPlaceholderText("Paste API key here")
        r2.addWidget(self._le_api_key)
        lay.addWidget(self._row_api_key)

        # Rows 3-4: OpenAI-only fields
        self._openai_fields = QWidget()
        of_lay = QVBoxLayout(self._openai_fields)
        of_lay.setContentsMargins(0, 0, 0, 0)
        of_lay.setSpacing(4)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Model:"))
        self._le_model = QLineEdit()
        self._le_model.setPlaceholderText("gpt-4o-mini")
        self._le_model.setMaximumWidth(160)
        row3.addWidget(self._le_model)
        row3.addSpacing(12)
        row3.addWidget(QLabel("Base URL:"))
        self._le_base_url = QLineEdit()
        self._le_base_url.setPlaceholderText(
            "https://api.openai.com/v1  (leave blank for default)"
        )
        row3.addWidget(self._le_base_url)
        of_lay.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("System Prompt:"))
        prompt_col = QVBoxLayout()
        self._te_system_prompt = QTextEdit()
        self._te_system_prompt.setPlaceholderText(
            "Supports {source_lang} and {target_lang} placeholders."
        )
        self._te_system_prompt.setFixedHeight(72)
        self._btn_reset_prompt = QPushButton("Reset to default")
        self._btn_reset_prompt.setToolTip(
            "Restore the built-in default system prompt template."
        )
        self._btn_reset_prompt.setFlat(True)
        self._btn_reset_prompt.clicked.connect(self._on_reset_system_prompt)
        prompt_col.addWidget(self._te_system_prompt)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._btn_reset_prompt)
        prompt_col.addLayout(btn_row)
        row4.addLayout(prompt_col)
        of_lay.addLayout(row4)

        lay.addWidget(self._openai_fields)

        # Row 5: Apply / Test + status
        row5 = QHBoxLayout()
        self._btn_apply_tl = QPushButton("Apply")
        self._btn_apply_tl.setToolTip(
            "Save settings and (re-)initialise the translator.\n"
            "Missing packages are installed automatically."
        )
        self._btn_test_tl = QPushButton("Test")
        self._btn_test_tl.setToolTip("Send a short test string to verify the translator is working.")
        self._lbl_tl_status = QLabel("")
        self._lbl_tl_status.setWordWrap(True)
        row5.addWidget(self._btn_apply_tl)
        row5.addWidget(self._btn_test_tl)
        row5.addWidget(self._lbl_tl_status, 1)
        lay.addLayout(row5)

        # Wire up
        self._cmb_backend.currentIndexChanged.connect(self._on_backend_changed)
        self._btn_apply_tl.clicked.connect(self._on_apply_translator)
        self._btn_test_tl.clicked.connect(self._on_test_translator)

        self._restore_translator_settings()
        self._on_backend_changed(self._cmb_backend.currentIndex())
        # Auto-build translator from saved config (non-blocking: errors shown in label)
        if _cfg.translator_backend not in ("none", ""):
            self._build_translator_from_config()
        return grp

    def _restore_translator_settings(self) -> None:
        """Populate translator settings widgets from persisted config."""
        backend = _cfg.translator_backend
        for i in range(self._cmb_backend.count()):
            if self._cmb_backend.itemData(i) == backend:
                self._cmb_backend.setCurrentIndex(i)
                break
        self._le_target_lang.setText(_cfg.translator_target_lang)
        # API key: show whichever is set
        if backend == "openai":
            self._le_api_key.setText(_cfg.openai_api_key)
            self._le_model.setText(_cfg.openai_model)
            self._le_base_url.setText(_cfg.openai_base_url)
            self._te_system_prompt.setPlainText(_cfg.openai_system_prompt or DEFAULT_SYSTEM_PROMPT)
        else:
            self._le_api_key.setText(_cfg.cloud_api_key)

    @Slot(int)
    def _on_backend_changed(self, index: int) -> None:
        """Show/hide backend-specific fields based on the selected backend."""
        backend = self._cmb_backend.itemData(index) or "none"
        self._row_api_key.setVisible(backend == "cloud")
        self._openai_fields.setVisible(backend == "openai")
        # Repopulate API key field with the relevant stored value
        if backend == "openai":
            self._le_api_key.setText(_cfg.openai_api_key)
        elif backend == "cloud":
            self._le_api_key.setText(_cfg.cloud_api_key)

    @Slot()
    def _on_reset_system_prompt(self) -> None:
        """Restore the built-in default system prompt template."""
        self._te_system_prompt.setPlainText(DEFAULT_SYSTEM_PROMPT)

    @Slot()
    def _on_apply_translator(self) -> None:
        """Persist settings and (re-)build the translator.  Auto-installs deps."""
        backend = self._cmb_backend.currentData() or "none"
        target_lang = self._le_target_lang.text().strip() or "en"
        api_key = self._le_api_key.text().strip()

        # Persist ALL fields first, regardless of backend
        _cfg.translator_backend = backend
        _cfg.translator_target_lang = target_lang
        if backend == "cloud":
            _cfg.cloud_api_key = api_key
        elif backend == "openai":
            _cfg.openai_api_key = api_key
            _cfg.openai_model = self._le_model.text().strip() or "gpt-4o-mini"
            _cfg.openai_base_url = self._le_base_url.text().strip()
            _cfg.openai_system_prompt = self._te_system_prompt.toPlainText().strip()

        if backend == "none":
            self._translator = None
            self._lbl_tl_status.setText("Translator disabled.")
            self._restart_worker_with_translator()
            return

        self._build_translator_from_config()
        self._restart_worker_with_translator()

    def _build_translator_from_config(self) -> None:
        """(Re-)build ``self._translator`` from current config.  Updates status label."""
        backend = _cfg.translator_backend
        target_lang = _cfg.translator_target_lang or "en"
        self._lbl_tl_status.setText("Building translator\u2026")
        QApplication.processEvents()
        try:
            self._translator = build_translator(
                _cfg,
                progress=lambda msg: (
                    self._lbl_tl_status.setText(msg),
                    QApplication.processEvents(),
                ),
            )
            if self._translator is not None:
                self._lbl_tl_status.setText(
                    f"\u2713 {backend.title()} translator ready  \u2192  {target_lang}"
                )
            else:
                self._lbl_tl_status.setText("Translator disabled.")
        except RuntimeError as exc:
            self._translator = None
            self._lbl_tl_status.setText(f"\u26a0 {exc}")

    def _restart_worker_with_translator(self) -> None:
        """Restart the pipeline worker so it picks up the new translator."""
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._run()

    @Slot()
    def _on_test_translator(self) -> None:
        """Translate a short fixed string to verify the backend is working."""
        if self._translator is None:
            self._lbl_tl_status.setText("No active translator \u2014 click Apply first.")
            return
        target_lang = _cfg.translator_target_lang or "en"
        test_src = "\u3053\u3093\u306b\u3061\u306f\u3001\u4e16\u754c\uff01"  # "こんにちは、世界！"
        self._lbl_tl_status.setText("Testing\u2026")
        QApplication.processEvents()
        try:
            result = self._translator.translate(test_src, target_lang=target_lang)
            self._lbl_tl_status.setText(f"Test \u2713  {test_src!r} \u2192 {result!r}")
        except Exception as exc:
            self._lbl_tl_status.setText(f"Test failed: {exc}")
