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
import logging

from PySide6.QtCore import (
    QSize, QThread, QTimer,
    Slot, Qt,
)

from src.config import AppConfig

_cfg = AppConfig()
_log = logging.getLogger(__name__)

from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QToolBar, QToolButton,
    QVBoxLayout, QWidget,
)

from src.controller import HoverController, OcrOutput, PipelineResult, RangeOutput, StepResult
from src.target import GameTarget
from src.ocr.windows_ocr import _ensure_apartment
from src.ocr.range_detectors import BoundingBox
from src.translators.base import PROVIDERS, PROVIDERS_BY_KEY, Translator
from src.translators.factory import build_translator
from src.translators.openai_translator import DEFAULT_SYSTEM_PROMPT, OPENAI_PRESETS
from src.knowledge import KnowledgeBase
from src.paths import knowledge_db_path
from src.overlay import FreezeOverlay, TranslationOverlay
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


# ---------------------------------------------------------------------------
# Knowledge Manager dialog
# ---------------------------------------------------------------------------

class _KnowledgeManagerDialog(QDialog):
    """Modal dialog for browsing and deleting knowledge-base entries."""

    def __init__(self, knowledge_base: "KnowledgeBase", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kb = knowledge_base
        self.setWindowTitle("📚 Knowledge Manager")
        self.setMinimumSize(800, 500)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        # ── Terms tab ──
        self._terms_table = QTableWidget()
        self._terms_table.setColumnCount(4)
        self._terms_table.setHorizontalHeaderLabels(["Category", "Original", "Translation", "Description"])
        self._terms_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._terms_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._terms_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._terms_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._terms_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._terms_table.setSortingEnabled(True)
        terms_widget = QWidget()
        terms_lay = QVBoxLayout(terms_widget)
        terms_lay.setContentsMargins(4, 4, 4, 4)
        terms_btn_row = QHBoxLayout()
        self._btn_del_term = QPushButton("🗑  Delete Selected")
        self._btn_del_term.clicked.connect(self._on_delete_term)
        terms_btn_row.addWidget(self._btn_del_term)
        terms_btn_row.addStretch()
        self._lbl_terms_count = QLabel()
        terms_btn_row.addWidget(self._lbl_terms_count)
        terms_lay.addLayout(terms_btn_row)
        terms_lay.addWidget(self._terms_table)
        self._tabs.addTab(terms_widget, "Terms")

        # ── Events tab ──
        self._events_table = QTableWidget()
        self._events_table.setColumnCount(2)
        self._events_table.setHorizontalHeaderLabels(["ID", "Summary"])
        self._events_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._events_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._events_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._events_table.setSortingEnabled(True)
        events_widget = QWidget()
        events_lay = QVBoxLayout(events_widget)
        events_lay.setContentsMargins(4, 4, 4, 4)
        events_btn_row = QHBoxLayout()
        self._btn_del_event = QPushButton("🗑  Delete Selected")
        self._btn_del_event.clicked.connect(self._on_delete_event)
        events_btn_row.addWidget(self._btn_del_event)
        events_btn_row.addStretch()
        self._lbl_events_count = QLabel()
        events_btn_row.addWidget(self._lbl_events_count)
        events_lay.addLayout(events_btn_row)
        events_lay.addWidget(self._events_table)
        self._tabs.addTab(events_widget, "Events")

        # ── Bottom buttons ──
        btn_box = QHBoxLayout()
        self._btn_refresh = QPushButton("🔄  Refresh")
        self._btn_refresh.clicked.connect(self._load_data)
        btn_box.addWidget(self._btn_refresh)
        btn_box.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_box.addWidget(close_btn)
        layout.addLayout(btn_box)

        self._load_data()

    def _load_data(self) -> None:
        """Reload both tables from the knowledge base."""
        self._load_terms()
        self._load_events()

    def _load_terms(self) -> None:
        self._terms_table.setSortingEnabled(False)
        self._terms_table.setRowCount(0)
        terms = self._kb.get_all_terms()
        self._terms_table.setRowCount(len(terms))
        for row, entry in enumerate(terms):
            self._terms_table.setItem(row, 0, QTableWidgetItem(entry.category))
            item_orig = QTableWidgetItem(entry.original)
            self._terms_table.setItem(row, 1, item_orig)
            self._terms_table.setItem(row, 2, QTableWidgetItem(entry.translation))
            self._terms_table.setItem(row, 3, QTableWidgetItem(entry.description))
        self._terms_table.setSortingEnabled(True)
        self._lbl_terms_count.setText(f"{len(terms)} term(s)")

    def _load_events(self) -> None:
        self._events_table.setSortingEnabled(False)
        self._events_table.setRowCount(0)
        rows = self._kb.get_all_events_rows()
        self._events_table.setRowCount(len(rows))
        for row, (eid, summary) in enumerate(rows):
            id_item = QTableWidgetItem()
            id_item.setData(Qt.ItemDataRole.DisplayRole, eid)
            self._events_table.setItem(row, 0, id_item)
            self._events_table.setItem(row, 1, QTableWidgetItem(summary))
        self._events_table.setSortingEnabled(True)
        self._lbl_events_count.setText(f"{len(rows)} event(s)")

    def _on_delete_term(self) -> None:
        selected = self._terms_table.selectedItems()
        if not selected:
            return
        rows = sorted({item.row() for item in selected}, reverse=True)
        originals = [self._terms_table.item(r, 1).text() for r in rows]
        if QMessageBox.question(
            self,
            "Delete Terms",
            f"Delete {len(originals)} term(s)?\n" + "\n".join(f"  • {o}" for o in originals[:10]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for orig in originals:
            self._kb.delete_term(orig)
        self._load_terms()

    def _on_delete_event(self) -> None:
        selected = self._events_table.selectedItems()
        if not selected:
            return
        row_indices = sorted({item.row() for item in selected}, reverse=True)
        event_ids = [
            self._events_table.item(r, 0).data(Qt.ItemDataRole.DisplayRole)
            for r in row_indices
        ]
        if QMessageBox.question(
            self,
            "Delete Events",
            f"Delete {len(event_ids)} event(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for eid in event_ids:
            self._kb.delete_event(int(eid))
        self._load_events()




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
# Step panel with per-step latency label + proportion bar
# ---------------------------------------------------------------------------

class _StepPanel(QWidget):
    """Pipeline step panel showing title, rolling-average latency, and a
    proportion bar that grows with the step's share of total pipeline time.

    Layout (top to bottom inside a styled frame):
      header row  ·  <title>  ────────  avg: X ms  ·  now: Y ms
      proportion  ·  ████████░░░░░░░░░░░░░░░  (fraction of total elapsed)
      text area   ·  read-only QTextEdit
    """

    _EMA_ALPHA: float = 0.2   # exponential moving-average smoothing factor

    def __init__(
        self,
        title: str,
        color: tuple[int, int, int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._base_title = title
        self._color = color
        self._avg_ms: float = 0.0
        self._n: int = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)

        # Styled frame for the visual border
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(4, 5, 4, 4)
        frame_lay.setSpacing(3)

        # Header row: bold title on the left, latency info on the right
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._lbl_title = QLabel(f"<b>{title}</b>")
        self._lbl_latency = QLabel("avg: —    now: —")
        self._lbl_latency.setStyleSheet("color: #999; font-size: 8pt;")
        self._lbl_latency.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self._lbl_title)
        header.addStretch()
        header.addWidget(self._lbl_latency)
        frame_lay.addLayout(header)

        # Proportion bar
        r, g, b = color
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(5)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: #2e2e2e; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: rgb({r},{g},{b}); border-radius: 2px; }}"
        )
        frame_lay.addWidget(self._bar)

        # Read-only text area
        self.te = QTextEdit()
        self.te.setReadOnly(True)
        self.te.setFont(QFont("Consolas", 9))
        frame_lay.addWidget(self.te, 1)

        outer.addWidget(frame, 1)

    # ------------------------------------------------------------------

    def set_subtitle(self, subtitle: str) -> None:
        """Update the optional subtitle appended to the title label."""
        if subtitle:
            self._lbl_title.setText(f"<b>{self._base_title}</b>  [{subtitle}]")
        else:
            self._lbl_title.setText(f"<b>{self._base_title}</b>")

    def update_timing(self, now_ms: float, total_ms: float) -> None:
        """Update EMA average, latency label text, and proportion bar."""
        self._n += 1
        if self._n == 1:
            self._avg_ms = now_ms
        else:
            self._avg_ms = (
                self._avg_ms * (1.0 - self._EMA_ALPHA)
                + now_ms * self._EMA_ALPHA
            )
        self._lbl_latency.setText(
            f"avg {self._avg_ms:.0f} ms  ·  now {now_ms:.0f} ms"
        )
        ratio = now_ms / total_ms if total_ms > 0 else 0.0
        self._bar.setValue(int(min(ratio, 1.0) * 1000))


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

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt")
        self.resize(1400, 820)

        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(primary.availableGeometry().center() - self.rect().center())

        self._target: GameTarget | None = None
        self._controller: HoverController | None = None
        self._worker_thread: QThread | None = None
        self._translator: Translator | None = None
        # Shared knowledge base — persists across translator rebuilds.
        self._knowledge_base: KnowledgeBase = KnowledgeBase.open(knowledge_db_path())

        self._picker: WindowPicker | None = None

        self._install_proc_handle: int | None = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(500)
        self._install_timer.timeout.connect(self._poll_install)

        # Overlays — translation popup + freeze screenshot.
        self._translation_overlay = TranslationOverlay()
        self._freeze_overlay = FreezeOverlay()
        self._freeze_overlay.dismissed.connect(self._on_freeze_dismissed)

        # -- state for debug dump --
        self._last_result: PipelineResult | None = None

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

        tb.addWidget(QLabel(" Freeze key: "))
        self._cmb_freeze_key = QComboBox()
        _VK_FKEYS = [
            ("F1", 0x70), ("F2", 0x71), ("F3", 0x72), ("F4", 0x73),
            ("F5", 0x74), ("F6", 0x75), ("F7", 0x76), ("F8", 0x77),
            ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A), ("F12", 0x7B),
        ]
        for label, vk in _VK_FKEYS:
            self._cmb_freeze_key.addItem(label, userData=vk)
        self._cmb_freeze_key.setToolTip("Hotkey to toggle freeze mode")
        tb.addWidget(self._cmb_freeze_key)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dump key: "))
        self._cmb_dump_key = QComboBox()
        for label, vk in _VK_FKEYS:
            self._cmb_dump_key.addItem(label, userData=vk)
        self._cmb_dump_key.setToolTip("Press to copy OCR / Memory / Corrected snapshot to clipboard")
        tb.addWidget(self._cmb_dump_key)
        tb.addSeparator()

        # ── Right-aligned tools menu ──────────────────────────────────
        _spacer = QWidget()
        _spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(_spacer)

        self._act_clear_cache = QAction("🗑  清除缓存", self)
        self._act_clear_cache.setToolTip(
            "Flush the in-memory and persistent translation caches.\n"
            "Use this after updating the app to force re-translation with improved logic."
        )
        self._act_clear_cache.setEnabled(False)
        self._act_clear_cache.triggered.connect(self._on_clear_cache)

        act_knowledge = QAction("📚  Knowledge Manager", self)
        act_knowledge.setToolTip("Browse or delete knowledge-base terms and events.")
        act_knowledge.triggered.connect(self._on_open_knowledge_manager)

        _tools_menu = QMenu(self)
        _tools_menu.addAction(self._act_clear_cache)
        _tools_menu.addSeparator()
        _tools_menu.addAction(act_knowledge)

        self._btn_tools = QToolButton()
        self._btn_tools.setText("工具")
        self._btn_tools.setToolTip("缓存 / Knowledge 管理")
        self._btn_tools.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._btn_tools.setMenu(_tools_menu)
        tb.addWidget(self._btn_tools)

        # ── Restore persisted settings ───────────────────────────────
        saved_lang = _cfg.ocr_language
        saved_interval = _cfg.interval_ms
        self._spn_interval.setValue(saved_interval)
        saved_vk = _cfg.freeze_vk
        for i in range(self._cmb_freeze_key.count()):
            if self._cmb_freeze_key.itemData(i) == saved_vk:
                self._cmb_freeze_key.setCurrentIndex(i)
                break
        saved_dump_vk = _cfg.dump_vk
        for i in range(self._cmb_dump_key.count()):
            if self._cmb_dump_key.itemData(i) == saved_dump_vk:
                self._cmb_dump_key.setCurrentIndex(i)
                break
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # Live-update freeze hotkey when combo changes.
        self._cmb_freeze_key.currentIndexChanged.connect(self._on_freeze_key_changed)
        self._cmb_dump_key.currentIndexChanged.connect(self._on_dump_key_changed)

        # Live-update poll interval when spinbox changes.
        self._spn_interval.valueChanged.connect(self._on_interval_changed)

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

        self._panel_wocr   = _StepPanel("Windows OCR",          (80,  160, 255))
        self._panel_region = _StepPanel("Detected Region",       (80,  210, 120))
        self._panel_mem    = _StepPanel("Memory Scan",           (255, 160,  50))
        self._panel_corr   = _StepPanel("Levenshtein Corrected", (180, 100, 255))
        self._panel_tl     = _StepPanel("Translation",           ( 80, 220, 200))

        # Convenience aliases so the rest of the code keeps working unchanged.
        self._te_wocr   = self._panel_wocr.te
        self._te_region = self._panel_region.te
        self._te_mem    = self._panel_mem.te
        self._te_corr   = self._panel_corr.te
        self._te_tl     = self._panel_tl.te

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

        right.addWidget(self._panel_wocr)
        right.addWidget(self._panel_region)
        right.addWidget(self._panel_mem)
        right.addWidget(self._panel_corr)
        right.addWidget(self._build_translator_settings_panel())
        right.addWidget(self._panel_tl)
        right.setSizes([220, 120, 100, 140, 180, 100])

        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 4)

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))
        # Right-aligned notification label for transient action feedback.
        self._notify_label = QLabel("")
        self._notify_label.setStyleSheet("padding-right: 8px; color: #ccc;")
        self.statusBar().addPermanentWidget(self._notify_label)
        self._notify_timer = QTimer(self)
        self._notify_timer.setSingleShot(True)
        self._notify_timer.timeout.connect(lambda: self._notify_label.setText(""))

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
        interval = self._spn_interval.value()
        freeze_vk = self._cmb_freeze_key.currentData() or 0x78
        dump_vk = self._cmb_dump_key.currentData() or 0x77

        self._controller = HoverController(
            self._target,
            language_tag=lang,
            translator=self._translator,
            source_lang=lang,
            target_lang=target_lang,
            freeze_vk=freeze_vk,
            dump_vk=dump_vk,
            poll_ms=interval,
            continuous=True,
        )
        self._worker_thread = QThread(self)
        self._controller.moveToThread(self._worker_thread)

        # Controller → debug panels + overlays
        self._controller.pipeline_debug.connect(self._on_result)
        self._controller.translation_ready.connect(self._on_translation)
        self._controller.pipeline_progress.connect(self._on_pipeline_progress)
        self._controller.freeze_triggered.connect(self._on_freeze_triggered)
        self._controller.dump_triggered.connect(self._on_dump_triggered)
        self._controller.cursor_moved.connect(self._translation_overlay.hide)
        self._controller.error.connect(self._on_error)
        self._controller.ready.connect(self._on_worker_ready)

        # Overlay → controller (freeze mode interaction)
        self._freeze_overlay.hover_requested.connect(
            self._controller.on_freeze_hover,
        )
        self._freeze_overlay.dismissed.connect(
            self._controller.on_freeze_dismissed,
        )

        self._worker_thread.started.connect(self._controller.setup)
        self._worker_thread.finished.connect(self._controller.teardown)

        self._worker_thread.start()
        self.statusBar().showMessage(
            f"Starting — lang={lang}  interval={interval} ms"
        )

    def _stop(self) -> None:
        self._act_clear_cache.setEnabled(False)
        if self._worker_thread is not None:
            self._worker_thread.quit()
            if not self._worker_thread.wait(3000):
                _log.warning("Worker thread did not stop in 3 s — terminating.")
                self._worker_thread.terminate()
                self._worker_thread.wait(1000)
            self._worker_thread = None
            self._controller = None

    @Slot()
    def _on_clear_cache(self) -> None:
        if self._controller is not None:
            self._controller.clear_caches()
            self.statusBar().showMessage("Translation caches cleared.", 3000)

    @Slot()
    def _on_worker_ready(self) -> None:
        self._act_clear_cache.setEnabled(True)
        lang = self._selected_language
        interval = self._spn_interval.value()
        self.statusBar().showMessage(
            f"Running — lang={lang}  interval={interval} ms"
        )

    # ------------------------------------------------------------------
    # Result / error handlers
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_result(self, result: PipelineResult) -> None:
        """Update debug panels with intermediate pipeline data."""
        self._last_result = result
        ocr = result.ocr.value
        rng = result.range_det.value
        self._preview.update_frame(
            result.img_bytes, ocr.boxes, ocr.line_boxes, rng.crop_rect
        )
        header = f"[ {len(ocr.boxes)} boxes  \u2014  {result.elapsed_ms:.0f} ms ]\n\n"
        self._te_wocr.setPlainText(header + ocr.text)
        self._panel_region.set_subtitle(rng.detector_name)
        self._te_region.setPlainText(rng.region_text)
        self._te_mem.setPlainText(result.scan.value)
        self._te_corr.setPlainText(result.corr.value)
        if result.translate.value:
            self._te_tl.setPlainText(result.translate.value)
        total = max(result.elapsed_ms, 1.0)
        self._panel_wocr.update_timing(result.ocr.ms, total)
        self._panel_region.update_timing(result.range_det.ms, total)
        self._panel_mem.update_timing(result.scan.ms, total)
        self._panel_corr.update_timing(result.corr.ms, total)
        self._panel_tl.update_timing(result.translate.ms, total)

    @Slot(str, object, object)
    def _on_pipeline_progress(
        self,
        step: str,
        near_rect: object,
        screen_origin: object,
    ) -> None:
        """Show a loading indicator in the appropriate overlay."""
        if self._freeze_overlay.is_active:
            self._freeze_overlay.show_translation(f"\u23f3 {step}")
        elif near_rect is not None and screen_origin is not None:
            self._translation_overlay.show_progress(step, near_rect, screen_origin)
        else:
            self._translation_overlay.show_progress(step, None, screen_origin or (0, 0))

    @Slot(str, object, object)
    def _on_translation(
        self,
        text: str,
        near_rect: object,
        screen_origin: object,
    ) -> None:
        """Route translation to the appropriate overlay."""
        if self._freeze_overlay.is_active:
            self._freeze_overlay.show_translation(text)
        elif near_rect is not None and screen_origin is not None:
            self._translation_overlay.show_translation(
                text, near_rect, screen_origin,
            )

    @Slot(object, int, int, int, int)
    def _on_freeze_triggered(
        self,
        screenshot: object,
        window_left: int,
        window_top: int,
        pid: int,
        hwnd: int,
    ) -> None:
        """Enter freeze mode when the controller detects the hotkey."""
        self._freeze_overlay.freeze(
            screenshot, window_left, window_top, pid, hwnd,
        )
        freeze_key = self._cmb_freeze_key.currentText()
        self.statusBar().showMessage(f"❄ Freeze — 右键/Esc 退出  ({freeze_key} 再次切换)")

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._notify(f"⚠  {message}", 10000)
        self._te_wocr.append(f"\n[worker error] {message}")

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Disconnect freeze_dismissed first so _on_freeze_dismissed cannot restart
        # timers after we've already stopped everything.
        try:
            self._freeze_overlay.dismissed.disconnect(self._on_freeze_dismissed)
        except RuntimeError:
            pass
        # Stop all timers and the worker thread.
        self._stop()
        # Persist toolbar settings.
        _cfg.interval_ms = self._spn_interval.value()
        _cfg.freeze_vk = self._cmb_freeze_key.currentData() or 0x78
        _cfg.dump_vk = self._cmb_dump_key.currentData() or 0x77
        # Close overlays and knowledge base.
        self._translation_overlay.close()
        self._freeze_overlay.close()
        try:
            self._knowledge_base.close()
        except Exception:
            pass
        super().closeEvent(event)
        # Ensure the process exits even if dangling threads/resources remain.
        QApplication.instance().quit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Freeze mode
    # ------------------------------------------------------------------

    @Slot(int)
    def _on_freeze_key_changed(self, index: int) -> None:
        """Update the live freeze hotkey VK code and persist it."""
        vk = self._cmb_freeze_key.itemData(index)
        if vk is not None:
            _cfg.freeze_vk = vk
            if self._controller is not None:
                self._controller.set_freeze_vk(vk)

    @Slot(int)
    def _on_dump_key_changed(self, index: int) -> None:
        """Update the live dump hotkey VK code and persist it."""
        vk = self._cmb_dump_key.itemData(index)
        if vk is not None:
            _cfg.dump_vk = vk
            if self._controller is not None:
                self._controller.set_dump_vk(vk)

    @Slot()
    def _on_dump_triggered(self) -> None:
        """Show OCR / Memory / Corrected snapshot in a dialog and copy to clipboard."""
        r = self._last_result
        if r is None:
            QMessageBox.information(self, "调试快照", "暂无流水线结果可导出。")
            return
        ocr_text  = r.range_det.value.region_text.strip()
        mem_text  = r.scan.value.strip()
        corr_text = r.corr.value.strip()
        tl_text   = r.translate.value.strip()
        lines = [
            "=== JustReadIt Debug Snapshot ===",
            f"[OCR]\n{ocr_text}",
            f"[Memory]\n{mem_text}" if mem_text else "[Memory]\n无结果",
            f"[Corrected]\n{corr_text}" if corr_text else "[Corrected]\n无结果",
        ]
        if tl_text:
            lines.append(f"[Translation]\n{tl_text}")
        text = "\n\n".join(lines)
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("📋 调试快照已复制到剪贴板", 4000)

    @Slot()
    def _on_freeze_dismissed(self) -> None:
        lang = self._selected_language
        interval = self._spn_interval.value()
        self.statusBar().showMessage(
            f"Running — lang={lang}  interval={interval} ms"
        )

    # ------------------------------------------------------------------
    # Interval change
    # ------------------------------------------------------------------

    def _notify(self, msg: str, ms: int = 4000) -> None:
        """Show a transient right-aligned message in the status bar."""
        self._notify_label.setText(msg)
        self._notify_timer.start(ms)

    @Slot(int)
    def _on_interval_changed(self, ms: int) -> None:
        """Push the new poll interval to the running controller."""
        if self._controller is not None:
            self._controller.set_poll_interval(ms)

    # ------------------------------------------------------------------
    # Knowledge Manager
    # ------------------------------------------------------------------

    @Slot()
    def _on_open_knowledge_manager(self) -> None:
        """Open the Knowledge Manager dialog."""
        dlg = _KnowledgeManagerDialog(self._knowledge_base, parent=self)
        dlg.exec()


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
        self._cmb_backend.addItem("\u2014 None \u2014", userData="none")
        for p in PROVIDERS:
            self._cmb_backend.addItem(p.display_name, userData=p.key)
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
        self._le_api_key.setPlaceholderText("Paste API key here  (local models: leave blank)")
        r2.addWidget(self._le_api_key)
        lay.addWidget(self._row_api_key)

        # Rows 3-4: OpenAI-only fields
        self._openai_fields = QWidget()
        of_lay = QVBoxLayout(self._openai_fields)
        of_lay.setContentsMargins(0, 0, 0, 0)
        of_lay.setSpacing(4)

        # Preset row: quick-fill button
        row_preset = QHBoxLayout()
        row_preset.addWidget(QLabel("快速预设:"))
        self._cmb_preset = QComboBox()
        self._cmb_preset.addItem("— 选择预设自动填充—", userData=None)
        for _p in OPENAI_PRESETS:
            self._cmb_preset.addItem(_p.label, userData=_p)
        self._cmb_preset.setToolTip(
            "选择后自动填充下方相应参数（不会立即保存，仍需点击 Apply）"
        )
        row_preset.addWidget(self._cmb_preset, 1)
        of_lay.addLayout(row_preset)

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

        row_ctx = QHBoxLayout()
        row_ctx.addWidget(QLabel("Context window:"))
        self._spn_context_window = QSpinBox()
        self._spn_context_window.setRange(0, 100)
        self._spn_context_window.setToolTip(
            "Number of recent translation pairs included as context"
        )
        self._spn_context_window.setMaximumWidth(80)
        row_ctx.addWidget(self._spn_context_window)
        row_ctx.addSpacing(12)
        row_ctx.addWidget(QLabel("Summary trigger:"))
        self._spn_summary_trigger = QSpinBox()
        self._spn_summary_trigger.setRange(0, 200)
        self._spn_summary_trigger.setToolTip(
            "History length that triggers summarisation of the oldest chunk"
        )
        self._spn_summary_trigger.setMaximumWidth(80)
        row_ctx.addWidget(self._spn_summary_trigger)
        row_ctx.addSpacing(16)
        self._chk_tools_enabled = QCheckBox("启用 KB 工具调用")
        self._chk_tools_enabled.setToolTip(
            "允许模型通过 function calling 读写知识库。\n"
            "对于小模型（如 Qwen-7B、Gemma 等）建议关闭，\n"
            "避免复杂 tool prompt 引起注意力消耗和格式混乱。"
        )
        self._chk_tools_enabled.setChecked(True)
        row_ctx.addWidget(self._chk_tools_enabled)
        row_ctx.addSpacing(16)
        self._chk_disable_thinking = QCheckBox("禁止 thinking")
        self._chk_disable_thinking.setToolTip(
            "预填空 <think></think> 强制跳过推理阶段。\n"
            "仅适用于 Ollama/LM Studio 上运行的思维链模型\n"
            "（DeepSeek-R1-Distill、QwQ 等）。\n"
            "请勿对 OpenAI 官方 API 启用，会导致 API 报错。"
        )
        self._chk_disable_thinking.setChecked(True)
        row_ctx.addWidget(self._chk_disable_thinking)
        row_ctx.addStretch()
        of_lay.addLayout(row_ctx)

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
        self._cmb_preset.currentIndexChanged.connect(self._on_preset_selected)

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
        self._spn_context_window.setValue(_cfg.openai_context_window)
        self._spn_summary_trigger.setValue(_cfg.openai_summary_trigger)
        self._chk_tools_enabled.setChecked(_cfg.openai_tools_enabled)
        self._chk_disable_thinking.setChecked(_cfg.openai_disable_thinking)
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
        info = PROVIDERS_BY_KEY.get(backend)
        self._row_api_key.setVisible(bool(info and info.needs_api_key))
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

    @Slot(int)
    def _on_preset_selected(self, index: int) -> None:
        """Apply the selected preset to the OpenAI fields (no save)."""
        preset = self._cmb_preset.itemData(index)
        if preset is None:
            return  # placeholder item
        self._le_model.setPlaceholderText(preset.model_placeholder)
        self._le_base_url.setPlaceholderText(preset.base_url_placeholder)
        self._chk_tools_enabled.setChecked(preset.tools_enabled)
        self._chk_disable_thinking.setChecked(preset.disable_thinking)
        self._spn_context_window.setValue(preset.context_window)
        self._spn_summary_trigger.setValue(preset.summary_trigger)
        self._te_system_prompt.setPlainText(preset.system_prompt)
        # Reset the combo back to placeholder so re-selecting same preset works
        self._cmb_preset.blockSignals(True)
        self._cmb_preset.setCurrentIndex(0)
        self._cmb_preset.blockSignals(False)

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
            _cfg.openai_context_window = self._spn_context_window.value()
            _cfg.openai_summary_trigger = self._spn_summary_trigger.value()
            _cfg.openai_tools_enabled = self._chk_tools_enabled.isChecked()
            _cfg.openai_disable_thinking = self._chk_disable_thinking.isChecked()

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
                knowledge_base=self._knowledge_base,
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
        """Push the new translator to the running controller."""
        if self._controller is not None:
            self._controller.set_translator(self._translator)

    def _build_translator_from_ui(self) -> "Translator | None":
        """Instantiate a translator from current UI fields without persisting to config."""
        backend = self._cmb_backend.currentData() or "none"
        if backend in ("none", ""):
            return None
        progress = lambda msg: (  # noqa: E731
            self._lbl_tl_status.setText(msg),
            QApplication.processEvents(),
        )
        api_key = self._le_api_key.text().strip()
        if backend == "cloud":
            from src.translators.cloud_translation import CloudTranslationTranslator
            return CloudTranslationTranslator(api_key=api_key or None, progress=progress)
        if backend == "google_free":
            from src.translators.google_free import GoogleFreeTranslator
            return GoogleFreeTranslator(progress=progress)
        if backend == "openai":
            from src.translators.openai_translator import OpenAICompatTranslator
            return OpenAICompatTranslator(
                api_key=api_key,
                model=self._le_model.text().strip() or "gpt-4o-mini",
                system_prompt=self._te_system_prompt.toPlainText().strip(),
                context_window=self._spn_context_window.value(),
                base_url=self._le_base_url.text().strip() or None,
                knowledge_base=self._knowledge_base,
                tools_enabled=self._chk_tools_enabled.isChecked(),
                progress=progress,
            )
        raise RuntimeError(f"Unknown backend: {backend!r}")

    @Slot()
    def _on_test_translator(self) -> None:
        """Build a temporary translator from current UI fields and run a test translation.

        Does *not* persist settings or replace the active translator — use Apply for that.
        """
        target_lang = self._le_target_lang.text().strip() or "en"
        self._lbl_tl_status.setText("Building\u2026")
        QApplication.processEvents()
        try:
            translator = self._build_translator_from_ui()
        except RuntimeError as exc:
            self._lbl_tl_status.setText(f"\u26a0 {exc}")
            return
        if translator is None:
            self._lbl_tl_status.setText("No backend selected.")
            return
        test_src = "\u3053\u3093\u306b\u3061\u306f\u3001\u4e16\u754c\uff01"  # "こんにちは、世界！"
        self._lbl_tl_status.setText("Testing\u2026")
        QApplication.processEvents()
        try:
            result = translator.translate(test_src, target_lang=target_lang)
            self._lbl_tl_status.setText(f"Test \u2713  {test_src!r} \u2192 {result!r}")
        except Exception as exc:
            self._lbl_tl_status.setText(f"Test failed: {exc}")
