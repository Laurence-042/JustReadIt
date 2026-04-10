# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""User-facing main window for JustReadIt.

A compact window (460 px wide) that exposes the core workflow:

  1. Click *Pick Game Window* to attach to the running game.
  2. The translation pipeline starts automatically; results appear in the
     floating overlay and in this window's translation panel.
  3. Press the Freeze hotkey (default F9) to enter screenshot-inspection mode.
  4. Open *Debug view* to configure the translator, OCR, and hotkeys.
  5. Minimising hides the window to the system tray — the pipeline keeps
     running.

Launch via ``python main.py`` (no flags).
The full debug view is available via ``python main.py --debug`` or the
*Debug view* button inside this window.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QFont, QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.languages import TARGET_PRESETS, display_name
from src.ocr.windows_ocr import _ensure_apartment
from src.target import GameTarget
from ._config_model import ConfigModel
from .window_picker import WindowPicker

_cfg = AppConfig()
_log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tray icon — generated from code; no external image file required
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_tray_icon() -> QIcon:
    """Return a 32×32 tray icon: dark square with a cyan 'J'."""
    pm = QPixmap(32, 32)
    pm.fill(QColor(28, 28, 40))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(100, 200, 255))
    p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "J")
    p.end()
    return QIcon(pm)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main user-facing window
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MainWindow(QMainWindow):
    """Compact user-facing window.

    Contains only the elements a non-developer needs: game picker, status
    indicator, last-translation panel, and access to Settings / Knowledge /
    Debug.  The translation overlay and freeze-mode overlay are shared with
    the debug window when both are open.
    """

    def __init__(self, backend: AppBackend) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt")
        self.setMinimumWidth(440)
        self.setMaximumWidth(660)

        self._backend = backend
        self._picker: WindowPicker | None = None
        self._debug_window: QMainWindow | None = None  # lazily created

        self._build_ui()
        self._setup_tray()

        # Connect backend signals to this view's slots.
        self._backend.translation_ready.connect(self._on_translation)
        self._backend.pipeline_progress.connect(self._on_pipeline_progress)
        self._backend.freeze_triggered.connect(self._on_freeze_triggered)
        self._backend.error.connect(self._on_error)
        self._backend.ready.connect(self._on_worker_ready)
        self._backend.freeze_overlay.dismissed.connect(self._on_freeze_dismissed)

        # Reactive config → widget sync: target-lang combo has custom
        # parsing logic, so it is not managed by QDataWidgetMapper.
        _cfg.translator_target_lang_changed.connect(self._sync_tgt_lang_combo)

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # ── Game picker row ───────────────────────────────────────────
        pick_row = QHBoxLayout()
        self._btn_pick = QPushButton("⊚ 选择游戏窗口")
        self._btn_pick.setFixedHeight(32)
        self._btn_pick.clicked.connect(self._start_picking)
        self._lbl_target = QLabel("未选择游戏")
        self._lbl_target.setStyleSheet("color: #999;")
        self._lbl_target.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._lbl_target.setWordWrap(True)
        pick_row.addWidget(self._btn_pick)
        pick_row.addWidget(self._lbl_target, 1)
        lay.addLayout(pick_row)

        # ── Status card ───────────────────────────────────────────────
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.NoFrame)
        self._status_card.setStyleSheet(
            "QFrame { border-top: 1px solid #2a2a3a; border-bottom: 1px solid #2a2a3a; }"
        )
        card_lay = QHBoxLayout(self._status_card)
        card_lay.setContentsMargins(2, 4, 2, 4)
        card_lay.setSpacing(6)
        self._lbl_dot = QLabel("\u25cf")
        self._lbl_dot.setStyleSheet("color: #444; font-size: 11px;")
        self._lbl_dot.setFixedWidth(14)
        self._lbl_status_text = QLabel("空闲 \u2014 选择游戏窗口以开始")
        self._lbl_status_text.setStyleSheet("color: #888; font-size: 9pt;")
        self._lbl_status_text.setWordWrap(True)
        card_lay.addWidget(self._lbl_dot)
        card_lay.addWidget(self._lbl_status_text, 1)
        lay.addWidget(self._status_card)

        # ── Language quick-select ─────────────────────────────────────
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(0, 0, 0, 0)
        _lbl_src = QLabel("源语言:")
        _lbl_src.setStyleSheet("color: #777; font-size: 9pt;")
        self._cmb_src_lang = QComboBox()
        self._cmb_src_lang.setMaximumWidth(140)
        self._cmb_src_lang.setToolTip(
            "翻译源语言（OCR 识别语言）\u2014 更改后立即生效。"
        )
        self._populate_src_languages()
        # Two-way bind: combo ↔ AppConfig.ocr_language via QDataWidgetMapper.
        self._mapper = ConfigModel.create_mapper(
            self,
            (self._cmb_src_lang, ConfigModel.OCR_LANGUAGE),
        )
        lang_row.addWidget(_lbl_src)
        lang_row.addSpacing(4)
        lang_row.addWidget(self._cmb_src_lang)

        lang_row.addSpacing(16)
        _lbl_tgt = QLabel("目标语言:")
        _lbl_tgt.setStyleSheet("color: #777; font-size: 9pt;")
        self._cmb_tgt_lang = QComboBox()
        self._cmb_tgt_lang.setEditable(True)
        self._cmb_tgt_lang.setMaximumWidth(180)
        self._cmb_tgt_lang.setToolTip(
            "翻译目标语言（BCP-47 标签）\u2014 更改后立即生效。\n"
            "从列表中选择或直接输入自定义标签。"
        )
        for _code in TARGET_PRESETS:
            self._cmb_tgt_lang.addItem(display_name(_code), userData=_code)
        _saved_tgt = _cfg.translator_target_lang or "zh-CN"
        _tgt_found = False
        for _i in range(self._cmb_tgt_lang.count()):
            if self._cmb_tgt_lang.itemData(_i) == _saved_tgt:
                self._cmb_tgt_lang.setCurrentIndex(_i)
                _tgt_found = True
                break
        if not _tgt_found:
            self._cmb_tgt_lang.setCurrentText(_saved_tgt)
        self._cmb_tgt_lang.currentIndexChanged.connect(self._on_tgt_lang_changed)
        lang_row.addWidget(_lbl_tgt)
        lang_row.addSpacing(4)
        lang_row.addWidget(self._cmb_tgt_lang)
        lang_row.addStretch()
        lay.addLayout(lang_row)

        # ── Translation display ───────────────────────────────────────
        lay.addWidget(_hsep())

        tl_header = QHBoxLayout()
        tl_header.addWidget(QLabel("最近翻译:"))
        tl_header.addStretch()
        self._btn_copy = QPushButton("复制")
        self._btn_copy.setFlat(True)
        self._btn_copy.setFixedHeight(22)
        self._btn_copy.setStyleSheet("color: #777;")
        self._btn_copy.clicked.connect(self._on_copy_translation)
        tl_header.addWidget(self._btn_copy)
        lay.addLayout(tl_header)

        self._te_translation = QTextEdit()
        self._te_translation.setReadOnly(True)
        self._te_translation.setFont(QFont("Segoe UI", 12))
        self._te_translation.setMinimumHeight(110)
        self._te_translation.setPlaceholderText(
            "将鼠标悬停在游戏文本上即可翻译。\n\n"
            "打开调试视图以配置翻译后端。"
        )
        lay.addWidget(self._te_translation, 1)

        lay.addWidget(_hsep())

        # ── Bottom buttons ────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_knowledge = QPushButton("\U0001f4da 知识库")
        self._btn_knowledge.clicked.connect(self._open_knowledge)
        self._btn_debug = QPushButton("\U0001f527 调试 / 设置")
        self._btn_debug.clicked.connect(self._open_debug)
        btn_row.addWidget(self._btn_knowledge)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_debug)
        lay.addLayout(btn_row)

        self.resize(460, 420)
        # Centre on primary screen
        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(
                primary.availableGeometry().center() - self.rect().center()
            )

    # ── System tray ───────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(_make_tray_icon(), self)
        self._tray.setToolTip("JustReadIt")

        menu = QMenu()
        act_show     = QAction("显示主窗口", self)
        act_debug    = QAction("\U0001f527 调试 / 设置", self)
        act_exit     = QAction("退出", self)
        act_show.triggered.connect(self._show_from_tray)
        act_debug.triggered.connect(self._open_debug)
        act_exit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_debug)
        menu.addSeparator()
        menu.addAction(act_exit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(
        self, reason: QSystemTrayIcon.ActivationReason
    ) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    @Slot()
    def _show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    # ── Status helpers ────────────────────────────────────────────────────

    def _set_status(self, text: str, dot_color: str = "#555") -> None:
        self._lbl_status_text.setText(text)
        self._lbl_dot.setStyleSheet(f"color: {dot_color}; font-size: 13px;")

    def _status_running(self) -> None:
        backend    = _cfg.translator_backend
        lang       = _cfg.ocr_language
        target_lang = _cfg.translator_target_lang
        info = f"{lang} → {target_lang}"
        if backend and backend != "none":
            info += f"  ·  {backend}"
        self._set_status(f"运行中  ·  {info}", "#4ec94e")

    # ── Language helpers ─────────────────────────────────────────────────

    def _populate_src_languages(self) -> None:
        """Fill :attr:`_cmb_src_lang` with available Windows OCR languages."""
        try:
            import winrt.windows.media.ocr as wocr  # noqa: PLC0415
            _ensure_apartment()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                self._cmb_src_lang.addItem(display_name(tag), userData=tag)
        except Exception as exc:
            self._cmb_src_lang.addItem(f"(error: {exc})", userData="ja")

    @Slot(int)
    def _on_tgt_lang_changed(self, index: int) -> None:
        """Persist the selected target language (signal auto-restarts pipeline)."""
        idx = self._cmb_tgt_lang.currentIndex()
        tag: str = ""
        if idx >= 0:
            data = self._cmb_tgt_lang.itemData(idx)
            if data:
                tag = str(data)
        if not tag:
            # Editable combo — raw text
            text = self._cmb_tgt_lang.currentText().strip()
            tag = text.split(" ")[0] if text else ""
        if not tag:
            return
        _cfg.translator_target_lang = tag

    # ── Reactive config → widget sync (unmapped widgets only) ─────────

    @Slot(str)
    def _sync_tgt_lang_combo(self, tag: str) -> None:
        with QSignalBlocker(self._cmb_tgt_lang):
            found = False
            for i in range(self._cmb_tgt_lang.count()):
                if self._cmb_tgt_lang.itemData(i) == tag:
                    self._cmb_tgt_lang.setCurrentIndex(i)
                    found = True
                    break
            if not found:
                self._cmb_tgt_lang.setCurrentText(tag)

    # ── Window picking ────────────────────────────────────────────────────

    def _start_picking(self) -> None:
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
        self.activateWindow()
        self._btn_pick.setEnabled(True)
        try:
            target = GameTarget.from_pid(pid)
        except Exception as exc:
            self._set_status(f"⚠ {exc}", "#e06060")
            return
        self._set_target(target)

    @Slot()
    def _on_pick_cancelled(self) -> None:
        QApplication.restoreOverrideCursor()
        self.showNormal()
        self._btn_pick.setEnabled(True)

    def _set_target(self, target: GameTarget) -> None:
        w = target.window_rect.width
        h = target.window_rect.height
        self._lbl_target.setText(
            f"{target.process_name}  (PID {target.pid})  [{w}×{h}]"
        )
        self._lbl_target.setStyleSheet("color: #ddd;")
        self._backend.set_target(target)  # stops old, stores target, starts pipeline

    # ── Controller → UI signals ───────────────────────────────────────────

    @Slot()
    def _on_worker_ready(self) -> None:
        self._status_running()

    @Slot(str, object, object)
    def _on_translation(
        self, text: str, near_rect: object, screen_origin: object
    ) -> None:
        self._te_translation.setPlainText(text)
        self._status_running()

    @Slot(str, object, object)
    def _on_pipeline_progress(
        self, step: str, near_rect: object, screen_origin: object
    ) -> None:
        self._set_status(f"翻译中…  ({step})", "#e0c840")

    @Slot(object, int, int, int, int)
    def _on_freeze_triggered(
        self,
        screenshot: object,
        window_left: int,
        window_top: int,
        pid: int,
        hwnd: int,
    ) -> None:
        # The freeze overlay is managed by AppBackend; we only update status.
        vk = _cfg.freeze_vk
        key_name = next(
            (f"F{i}" for i in range(1, 13) if 0x6F + i == vk), f"0x{vk:02X}"
        )
        self._set_status(
            f"\u2744 冻结模式 \u2014 右键或 Esc 退出  ({key_name})",
            "#60b0ff",
        )

    @Slot()
    def _on_freeze_dismissed(self) -> None:
        if self._backend.target is not None:
            self._status_running()

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._set_status(f"⚠ {message}", "#e06060")
        _log.warning("Controller error: %s", message)

    # ── Button handlers ───────────────────────────────────────────────────

    @Slot()
    def _on_copy_translation(self) -> None:
        text = self._te_translation.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    @Slot()
    def _open_knowledge(self) -> None:
        # Lazy import to avoid loading the debug window module eagerly.
        from src.ui.debug_window import _KnowledgeManagerDialog  # noqa: PLC0415
        dlg = _KnowledgeManagerDialog(self._backend.knowledge_base, parent=self)
        dlg.exec()

    @Slot()
    def _open_debug(self) -> None:
        if self._debug_window is not None and self._debug_window.isVisible():
            self._debug_window.activateWindow()
            self._debug_window.raise_()
            return
        from src.ui.debug_window import DebugWindow  # noqa: PLC0415
        self._debug_window = DebugWindow(self._backend, standalone=False)
        self._debug_window.closed.connect(
            lambda: setattr(self, "_debug_window", None)
        )
        self._debug_window.show()
        self._debug_window.activateWindow()
        self._debug_window.raise_()

    @Slot()
    def _quit(self) -> None:
        self.close()

    # ── Qt overrides ──────────────────────────────────────────────────────

    def changeEvent(self, event) -> None:  # noqa: N802
        """Minimise to tray instead of taskbar."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
            self._tray.showMessage(
                "JustReadIt",
                "翻译在后台继续运行。点击托盘图标恢复窗口。",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._backend.freeze_overlay.dismissed.disconnect(self._on_freeze_dismissed)
        except RuntimeError:
            pass
        if self._debug_window is not None:
            try:
                self._debug_window.close()
            except Exception:
                pass
        self._backend.close()
        self._tray.hide()
        super().closeEvent(event)
        QApplication.instance().quit()  # type: ignore[union-attr]


# ── Shared widget helpers ──────────────────────────────────────────────────────

def _hsep() -> QFrame:
    """Return a horizontal separator line."""
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFrameShadow(QFrame.Shadow.Sunken)
    return sep
