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
    QSignalBlocker,
    QSize,
    QTimer,
    Signal,
    Slot,
    Qt,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.languages import display_name

_cfg = AppConfig()
_log = logging.getLogger(__name__)

from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QToolButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.controller import OcrOutput, PipelineResult, RangeOutput, StepResult
from src.target import GameTarget
from src.ocr.windows_ocr import _ensure_apartment
from src.ocr.range_detectors import BoundingBox
from ._config_model import ConfigModel
from ._translator_settings import TranslatorSettingsWidget
from .window_picker import WindowPicker
from src.knowledge.knowledge_manager import KnowledgeManagerDialog
from src.dataset.dataset_dialog import DatasetDialog


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
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_TIMEOUT = 0x00000102
_kernel32_ui = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------------------
# Capture preview with bbox overlay
# ---------------------------------------------------------------------------

_BBOX_COLORS = [
    QColor(255, 80, 80),
    QColor(80, 200, 80),
    QColor(80, 130, 255),
    QColor(255, 200, 50),
    QColor(200, 80, 255),
    QColor(80, 220, 220),
]


class _PreviewLabel(QLabel):
    """QLabel subclass that scales the captured frame and draws bbox overlays."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(400, 300))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("暂无画面。\n请选择游戏窗口。")
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
                lw,
                lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            # Compute the same size as the scaled image would have, but
            # fill with a dark background instead of the game frame.
            tmp = self._raw.scaled(
                lw,
                lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            scaled = QPixmap(tmp.width(), tmp.height())
            scaled.fill(QColor(40, 40, 40))

        sx = scaled.width() / self._orig_w
        sy = scaled.height() / self._orig_h
        ox = (lw - scaled.width()) // 2
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
                    painter.fillRect(
                        rx, max(0, ry - 11), label_w, 11, QColor(0, 0, 0, 160)
                    )
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(rx + 1, max(9, ry - 1), box.text[:24])
            painter.end()

        if self._line_boxes and self.show_lines:
            plines = QPainter(scaled)
            pen_line = QPen(QColor(80, 255, 200), 1, Qt.PenStyle.DotLine)
            plines.setPen(pen_line)
            for lb in self._line_boxes:
                plines.drawRect(
                    int(lb.x * sx),
                    int(lb.y * sy),
                    max(1, int(lb.w * sx)),
                    max(1, int(lb.h * sy)),
                )
            plines.end()

        if self._crop_rect is not None and self.show_region:
            cl, ct, cr, cb = self._crop_rect
            painter2 = QPainter(scaled)
            pen = QPen(QColor(255, 255, 100), 2, Qt.PenStyle.DashLine)
            painter2.setPen(pen)
            painter2.drawRect(
                int(cl * sx),
                int(ct * sy),
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

    _EMA_ALPHA: float = 0.2  # exponential moving-average smoothing factor

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
        self.te.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.te.setStyleSheet(
            "QScrollBar:vertical { background: #252525; width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: #4a4a4a; min-height: 20px;"
            " border-radius: 3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )
        frame_lay.addWidget(self.te, 1)

        outer.addWidget(frame, 1)

        # Reference kept for add_settings_row()
        self._frame_lay = frame_lay

    # ------------------------------------------------------------------

    def add_settings_row(self, widget: QWidget) -> None:
        """Insert a compact settings row between the proportion bar and text.

        The widget is inserted at index 2 (after header + bar, before te).
        """
        self._frame_lay.insertWidget(2, widget)

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
                self._avg_ms * (1.0 - self._EMA_ALPHA) + now_ms * self._EMA_ALPHA
            )
        self._lbl_latency.setText(f"avg {self._avg_ms:.0f} ms  ·  now {now_ms:.0f} ms")
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

    When opened from :class:`MainWindow`, caller passes *knowledge_base*,
    *translator*, and *target* so that both windows share a single backend
    instance and knowledge base.  Set *standalone=False* to prevent this
    window from calling ``QApplication.quit()`` on close.
    """

    #: Emitted just before the window closes, regardless of *standalone*.
    closed = Signal()

    def __init__(self, backend: AppBackend, *, standalone: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt")
        self.resize(1400, 820)

        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(primary.availableGeometry().center() - self.rect().center())

        self._standalone = standalone
        self._backend = backend
        self._picker: WindowPicker | None = None

        self._install_proc_handle: int | None = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(500)
        self._install_timer.timeout.connect(self._poll_install)

        # -- state for debug dump --
        self._last_result: PipelineResult | None = None

        self._build_ui()

        # Connect to backend signals — views own no backend resources.
        self._backend.translation_ready.connect(self._on_translation)
        self._backend.pipeline_debug.connect(self._on_result)
        self._backend.pipeline_progress.connect(self._on_pipeline_progress)
        self._backend.freeze_triggered.connect(self._on_freeze_triggered)
        self._backend.dump_triggered.connect(self._on_dump_triggered)
        self._backend.error.connect(self._on_error)
        self._backend.ready.connect(self._on_worker_ready)
        self._backend.freeze_overlay.dismissed.connect(self._on_freeze_dismissed)
        self._backend.paused_changed.connect(self._on_paused_changed)
        self._backend.recording_changed.connect(self._on_recording_changed)

        # If the backend is already running when this window is created,
        # the ready signal was emitted before our connection — catch up now.
        if self._backend.is_running:
            self._on_worker_ready()

        # Populate target label if backend already has a target.
        if backend.target is not None:
            t = backend.target
            self._lbl_target.setText(
                f"{t.process_name}  (PID {t.pid})"
                f"  [{t.window_rect.width}\u00d7{t.window_rect.height}]"
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self._btn_pick = QPushButton("⊕ 选择窗口")
        self._btn_pick.setToolTip("最小化本窗口，点击游戏窗口以选中目标")
        self._btn_pick.clicked.connect(self._start_picking)
        tb.addWidget(self._btn_pick)
        tb.addSeparator()

        tb.addWidget(QLabel("目标: "))
        self._lbl_target = QLabel("—")
        self._lbl_target.setMinimumWidth(260)
        tb.addWidget(self._lbl_target)
        tb.addSeparator()

        tb.addWidget(QLabel(" 悬停延迟: "))
        self._spn_interval = QSpinBox()
        self._spn_interval.setRange(200, 15000)
        self._spn_interval.setValue(1500)
        self._spn_interval.setSuffix(" ms")
        self._spn_interval.setToolTip(
            "鼠标悬停多久（毫秒）后触发翻译流水线。\n"
            "鼠标大幅移动（≥20 px）会重置计时器，\n"
            "即鼠标不停移动时不会重复触发。"
        )
        tb.addWidget(self._spn_interval)
        tb.addSeparator()

        tb.addWidget(QLabel(" 冻结键: "))
        self._cmb_freeze_key = QComboBox()
        _VK_FKEYS = [
            ("F1", 0x70),
            ("F2", 0x71),
            ("F3", 0x72),
            ("F4", 0x73),
            ("F5", 0x74),
            ("F6", 0x75),
            ("F7", 0x76),
            ("F8", 0x77),
            ("F9", 0x78),
            ("F10", 0x79),
            ("F11", 0x7A),
            ("F12", 0x7B),
        ]
        for label, vk in _VK_FKEYS:
            self._cmb_freeze_key.addItem(label, userData=vk)
        self._cmb_freeze_key.setToolTip("切换冻结模式的快捷键")
        tb.addWidget(self._cmb_freeze_key)
        tb.addSeparator()

        tb.addWidget(QLabel(" 快照键: "))
        self._cmb_dump_key = QComboBox()
        for label, vk in _VK_FKEYS:
            self._cmb_dump_key.addItem(label, userData=vk)
        self._cmb_dump_key.setToolTip("按下后将 OCR / 内存 / 校正文本快照复制到剪贴板")
        tb.addWidget(self._cmb_dump_key)
        tb.addSeparator()

        self._btn_pause = QPushButton("\u23f8 暂停")
        self._btn_pause.setToolTip("暂停 / 恢复翻译流水线")
        self._btn_pause.setFixedWidth(78)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        tb.addWidget(self._btn_pause)

        self._btn_record = QPushButton("\u23fa 记录")
        self._btn_record.setToolTip("开始 / 停止将流水线样本录入数据集（用于算法迭代）")
        self._btn_record.setFixedWidth(78)
        self._btn_record.setCheckable(True)
        self._btn_record.clicked.connect(self._on_record_clicked)
        tb.addWidget(self._btn_record)

        # ── Right-aligned tools menu ──────────────────────────────────
        _spacer = QWidget()
        _spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(_spacer)

        self._act_clear_cache = QAction("🗑 清除缓存", self)
        self._act_clear_cache.setToolTip(
            "清空内存缓存和持久化翻译缓存。\n"
            "更新应用后使用，强制以改进的逻辑重新翻译。"
        )
        self._act_clear_cache.triggered.connect(self._on_clear_cache)

        act_knowledge = QAction("📚 知识库管理", self)
        act_knowledge.setToolTip("浏览或删除知识库中的术语和事件。")
        act_knowledge.triggered.connect(self._on_open_knowledge_manager)

        act_dataset = QAction("📊 数据集标注", self)
        act_dataset.setToolTip("浏览、标注和删除流水线数据集样本。")
        act_dataset.triggered.connect(self._on_open_dataset)

        act_open_dir = QAction("📂 打开数据目录", self)
        act_open_dir.setToolTip(
            "在资源管理器中打开 %APPDATA%\\JustReadIt\\。\n"
            "可在此备份/替换 config.json、knowledge.db、translations.db 等文件。"
        )
        act_open_dir.triggered.connect(self._on_open_data_dir)

        _tools_menu = QMenu(self)
        _tools_menu.addAction(self._act_clear_cache)
        _tools_menu.addSeparator()
        _tools_menu.addAction(act_knowledge)
        _tools_menu.addAction(act_dataset)
        _tools_menu.addSeparator()
        _tools_menu.addAction(act_open_dir)

        self._btn_tools = QToolButton()
        self._btn_tools.setText("工具")
        self._btn_tools.setToolTip("缓存 / 知识库管理")
        self._btn_tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._btn_tools.setMenu(_tools_menu)
        tb.addWidget(self._btn_tools)

        # ── Install progress bar (hidden until capability install) ───
        self._install_bar = QWidget()
        _ibl = QHBoxLayout(self._install_bar)
        _ibl.setContentsMargins(6, 3, 6, 3)
        self._install_lbl = QLabel("正在安装…")
        self._install_prog = QProgressBar()
        self._install_prog.setRange(0, 0)  # indeterminate
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

        self._chk_image = QCheckBox("画面")
        self._chk_lines = QCheckBox("OCR行")
        self._chk_boxes = QCheckBox("OCR框")
        self._chk_labels = QCheckBox("OCR结果")
        self._chk_region = QCheckBox("聚合范围")

        for chk in (
            self._chk_image,
            self._chk_lines,
            self._chk_boxes,
            self._chk_labels,
            self._chk_region,
        ):
            chk.setChecked(True)
            toggle_row.addWidget(chk)

        toggle_row.addWidget(_vsep := QFrame())
        _vsep.setFrameShape(QFrame.Shape.VLine)
        _vsep.setFrameShadow(QFrame.Shadow.Sunken)
        _vsep.setFixedWidth(2)

        self._chk_mem_scan = QCheckBox("内存扫描")
        self._chk_mem_scan.setToolTip(
            "启用 ReadProcessMemory 内存扫描（提升文本精度，但会增加内存占用）\n"
            "内存较大的游戏建议关闭以使用纯 OCR 模式"
        )
        toggle_row.addWidget(self._chk_mem_scan)
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

        # -- Right column: text panels (in a scroll area for small screens) --
        right = QSplitter(Qt.Orientation.Vertical)
        right.setMinimumHeight(860)  # 220+120+100+140+180+100 = 860; scroll below this
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)

        self._panel_wocr = _StepPanel("Windows OCR", (80, 160, 255))
        self._panel_region = _StepPanel("检测区域", (80, 210, 120))
        self._panel_mem = _StepPanel("内存扫描", (255, 160, 50))
        self._panel_corr = _StepPanel("校正文本", (180, 100, 255))
        self._panel_tl = _StepPanel("翻译", (80, 220, 200))

        # ── OCR settings row (embedded inside the Windows OCR panel) ──
        ocr_settings = QWidget()
        ocr_sl = QHBoxLayout(ocr_settings)
        ocr_sl.setContentsMargins(0, 2, 0, 2)
        ocr_sl.setSpacing(6)
        ocr_sl.addWidget(QLabel("语言:"))
        self._cmb_lang = QComboBox()
        self._cmb_lang.setToolTip("Windows OCR 识别语言")
        self._populate_languages()
        ocr_sl.addWidget(self._cmb_lang)
        ocr_sl.addSpacing(12)
        ocr_sl.addWidget(QLabel("最大尺寸:"))
        self._spn_ocr_max = QSpinBox()
        self._spn_ocr_max.setRange(480, 7680)
        self._spn_ocr_max.setSingleStep(240)
        self._spn_ocr_max.setSuffix(" px")
        self._spn_ocr_max.setToolTip(
            "送入 Windows OCR 的图像最大长边（像素）。\n"
            "1920 对 1080p 无影响，4K 帧减半。\n"
            "下次启动流水线时生效。"
        )
        ocr_sl.addWidget(self._spn_ocr_max)
        ocr_sl.addStretch()
        self._panel_wocr.add_settings_row(ocr_settings)

        # Restore OCR language (not managed by the config-model mapper
        # because the combo has custom language-pack install logic).
        saved_lang = _cfg.ocr.language
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # Convenience aliases so the rest of the code keeps working unchanged.
        self._te_wocr = self._panel_wocr.te
        self._te_region = self._panel_region.te
        self._te_mem = self._panel_mem.te
        self._te_corr = self._panel_corr.te
        self._te_tl = self._panel_tl.te

        self._te_region.setPlaceholderText("区域文本将在范围检测后显示。")
        self._te_mem.setPlaceholderText(
            "内存扫描结果显示在此。\n"
            "通过 ReadProcessMemory 扫描游戏堆内存中的 OCR 文本子串。"
        )
        self._te_corr.setPlaceholderText(
            "校正文本（OCR ↔ 内存最佳匹配）显示在此。\n"
            "无高置信匹配时回退到 OCR 区域文本。"
        )
        self._te_tl.setPlaceholderText("请在下方配置翻译后端并点击「应用」以启用。")

        right.addWidget(self._panel_wocr)
        right.addWidget(self._panel_region)
        right.addWidget(self._panel_mem)
        right.addWidget(self._panel_corr)
        right.addWidget(self._build_translator_settings_panel())
        right.addWidget(self._panel_tl)
        right.setSizes([80, 120, 100, 140, 280, 140])

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 980])

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))
        # Right-aligned notification label for transient action feedback.
        self._notify_label = QLabel("")
        self._notify_label.setStyleSheet("padding-right: 8px; color: #ccc;")
        self.statusBar().addPermanentWidget(self._notify_label)
        self._notify_timer = QTimer(self)
        self._notify_timer.setSingleShot(True)
        self._notify_timer.timeout.connect(lambda: self._notify_label.setText(""))

        # Connect widget change handlers AFTER populating to avoid spurious
        # signals (e.g. the install prompt for missing OCR language packs).
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)

        # ── QDataWidgetMapper ────────────────────────────────────────
        # Two-way binding: widget edits → config, config changes → widget.
        # Replaces manual restore loops, widget→config handlers, and
        # blockSignals sync slots for all mapped widgets.
        self._mapper = ConfigModel.create_mapper(
            self,
            (self._spn_interval, ConfigModel.INTERVAL_MS),
            (self._spn_ocr_max, ConfigModel.OCR_MAX_SIZE),
            (self._cmb_freeze_key, ConfigModel.FREEZE_VK),
            (self._cmb_dump_key, ConfigModel.DUMP_VK),
            (self._chk_mem_scan, ConfigModel.MEMORY_SCAN_ENABLED),
        )

        # OCR language combo is NOT mapped (custom install logic in
        # _on_lang_changed); sync from config manually.
        _cfg.ocr.language_changed.connect(self._sync_lang_combo)

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

            _ensure_apartment()

            installed_tags: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed_tags.add(tag)
                self._cmb_lang.addItem(display_name(tag), userData=tag)

            for tag, capability in _LANG_CAPABILITIES.items():
                # WinRT tags are region-specific (e.g. "ja-JP"), while
                # _LANG_CAPABILITIES keys are bare subtags ("ja").  Accept
                # any installed tag that equals or starts with "<tag>-".
                if any(t == tag or t.startswith(tag + "-") for t in installed_tags):
                    continue
                self._cmb_lang.addItem(
                    f"{tag}  ⬇ 点击安装 (~6 MB)",
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

        _cfg.ocr.language = tag  # signal → backend restart if running
        if self._backend.is_running:
            self.statusBar().showMessage(f"正在以 lang={tag} 重启流水线…")

    # ------------------------------------------------------------------
    # Language pack installation
    # ------------------------------------------------------------------

    def _start_install(self, lang_tag: str) -> None:
        capability = _LANG_CAPABILITIES[lang_tag]
        reply = QMessageBox.question(
            self,
            "安装 Windows OCR 语言包",
            f"OCR 语言 '{lang_tag}' 的语言包尚未安装。\n\n"
            f"Capability:  {capability}\n\n"
            "是否立即安装？（约 6 MB，仅 OCR 数据 — 不会更改系统语言）\n"
            "安装时会出现管理员（UAC）提权提示。",
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
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = _SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = "powershell.exe"
        sei.lpParameters = args
        sei.nShow = 1  # SW_SHOWNORMAL
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not ok or not sei.hProcess:
            self.statusBar().showMessage(
                "无法启动安装程序 — UAC 被拒绝或未找到 PowerShell。",
                8000,
            )
            return

        self._install_proc_handle = sei.hProcess
        self._install_lbl.setText(
            f"正在安装 {capability}…  " "（可能需要一分钟 — 请勿关闭此窗口）"
        )
        self._install_bar.setVisible(True)
        self._install_timer.start()
        self.statusBar().showMessage(f"正在安装 {capability}…")

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
            _kernel32_ui.CloseHandle(ctypes.c_void_p(self._install_proc_handle))
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
        self.statusBar().showMessage("语言包安装完成 — 已自动生效。", 8000)

    # ------------------------------------------------------------------
    # Window picking
    # ------------------------------------------------------------------

    def _start_picking(self) -> None:
        self.statusBar().showMessage("请点击游戏窗口以选中目标…  （右键取消）")
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
        self.statusBar().showMessage("已取消选择。", 3000)

    def _set_target(self, target: GameTarget) -> None:
        w = target.window_rect.width
        h = target.window_rect.height
        self._lbl_target.setText(
            f"{target.process_name}  (PID {target.pid})  [{w}×{h}]"
        )
        self.statusBar().showMessage(
            f"目标: {target.process_name}  PID={target.pid}"
            f"  output_idx={target.dxcam_output_idx}",
            5000,
        )
        self._backend.set_target(target)

    # ------------------------------------------------------------------
    # Pipeline lifecycle (delegated to AppBackend)
    # ------------------------------------------------------------------

    @Slot()
    def _on_clear_cache(self) -> None:
        self._backend.clear_caches()
        self.statusBar().showMessage("翻译缓存已清除。", 3000)

    @Slot()
    def _on_worker_ready(self) -> None:
        self._btn_pause.setEnabled(True)
        lang = self._selected_language
        interval = self._spn_interval.value()
        self.statusBar().showMessage(f"运行中 — 语言={lang}  延迟={interval} ms")

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
        """Show a loading indicator in the status bar."""
        self.statusBar().showMessage(f"⏳ {step}")

    @Slot(str, object, object)
    def _on_translation(
        self,
        text: str,
        near_rect: object,
        screen_origin: object,
    ) -> None:
        """Update translation panel (overlay is handled by AppBackend)."""
        if self._te_tl is not None:
            self._te_tl.setPlainText(text)

    @Slot(object, int, int, int, int)
    def _on_freeze_triggered(
        self,
        screenshot: object,
        window_left: int,
        window_top: int,
        pid: int,
        hwnd: int,
    ) -> None:
        """Update status bar when freeze mode starts (overlay handled by AppBackend)."""
        freeze_key = self._cmb_freeze_key.currentText()
        self.statusBar().showMessage(
            f"❄ Freeze — 右键/Esc 退出  ({freeze_key} 再次切换)"
        )

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._notify(f"⚠  {message}", 10000)
        self._te_wocr.append(f"\n[worker error] {message}")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------

    @Slot()
    def _on_pause_clicked(self) -> None:
        self._backend.set_paused(not self._backend.is_paused)

    @Slot(bool)
    def _on_paused_changed(self, paused: bool) -> None:
        if paused:
            self._btn_pause.setText("\u25b6 恢复")
            self.statusBar().showMessage("已暂停")
        else:
            self._btn_pause.setText("\u23f8 暂停")
            lang = self._selected_language
            interval = self._spn_interval.value()
            self.statusBar().showMessage(f"运行中 — 语言={lang}  延迟={interval} ms")

    @Slot()
    def _on_record_clicked(self) -> None:
        self._backend.set_recording(self._btn_record.isChecked())

    @Slot(bool)
    def _on_recording_changed(self, recording: bool) -> None:
        with QSignalBlocker(self._btn_record):
            self._btn_record.setChecked(recording)
        self._btn_record.setText("⏹ 停止" if recording else "⏺ 记录")
        self._btn_record.setStyleSheet(
            "QPushButton { color: #e55; font-weight: bold; }" if recording else ""
        )
        self.statusBar().showMessage(
            "🔴 数据集记录中…" if recording else "⏹ 数据集记录已停止", 4000
        )

    @Slot()
    def _on_open_dataset(self) -> None:
        """Open the dataset annotation dialog, initialising the DB if needed."""
        from src.paths import dataset_db_path  # noqa: PLC0415
        from src.dataset import PipelineDataset  # noqa: PLC0415

        # Prefer the already-open backend dataset so recorded-but-not-committed
        # rows are immediately visible.  Fall back to a fresh read-only connection.
        ds = self._backend.dataset or PipelineDataset.open(dataset_db_path())
        dlg = DatasetDialog(ds, parent=self)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    @Slot()
    def _on_open_data_dir(self) -> None:
        """Open the JustReadIt AppData directory in Windows Explorer."""
        import os  # noqa: PLC0415
        from src.paths import app_data_dir  # noqa: PLC0415

        os.startfile(app_data_dir())

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        super().closeEvent(event)
        if self._standalone:
            QApplication.instance().quit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Freeze mode
    # ------------------------------------------------------------------

    @Slot()
    def _on_dump_triggered(self) -> None:
        """Show OCR / Memory / Corrected snapshot in a dialog and copy to clipboard."""
        r = self._last_result
        if r is None:
            QMessageBox.information(self, "调试快照", "暂无流水线结果可导出。")
            return
        ocr_text = r.range_det.value.region_text.strip()
        mem_text = r.scan.value.strip()
        corr_text = r.corr.value.strip()
        tl_text = r.translate.value.strip()
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
        self.statusBar().showMessage(f"运行中 — 语言={lang}  延迟={interval} ms")

    # ------------------------------------------------------------------
    # Interval change
    # ------------------------------------------------------------------

    def _notify(self, msg: str, ms: int = 4000) -> None:
        """Show a transient right-aligned message in the status bar."""
        self._notify_label.setText(msg)
        self._notify_timer.start(ms)

    # ------------------------------------------------------------------
    # Knowledge Manager
    # ------------------------------------------------------------------

    @Slot()
    def _on_open_knowledge_manager(self) -> None:
        """Open the Knowledge Manager dialog (non-modal)."""
        dlg = KnowledgeManagerDialog(self._backend.knowledge_base, parent=self)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    # ------------------------------------------------------------------
    # Reactive config → widget sync (unmapped widgets only)
    # ------------------------------------------------------------------
    # Widgets bound via QDataWidgetMapper auto-sync; only the OCR language
    # combo (which has custom install logic) needs a manual sync slot.

    @Slot(str)
    def _sync_lang_combo(self, tag: str) -> None:
        """Update OCR language combo from config (not managed by mapper)."""
        with QSignalBlocker(self._cmb_lang):
            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == tag:
                    self._cmb_lang.setCurrentIndex(i)
                    break

    # ------------------------------------------------------------------
    # Translator settings panel
    # ------------------------------------------------------------------

    def _build_translator_settings_panel(self) -> QWidget:
        """Translator configuration panel backed by the shared widget."""
        grp = QGroupBox("翻译设置")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(0)
        self._tl_settings = TranslatorSettingsWidget(
            self._backend,
            auto_build=True,
            parent=grp,
        )
        lay.addWidget(self._tl_settings)
        return grp
