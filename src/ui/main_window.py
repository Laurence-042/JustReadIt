# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""User-facing main window for JustReadIt.

A compact window (460 px wide) that exposes the core workflow:

  1. Click *Pick Game Window* to attach to the running game.
  2. The translation pipeline starts automatically; results appear in the
     floating overlay and in this window's translation panel.
  3. Press the Freeze hotkey (default F9) to enter screenshot-inspection mode.
  4. Click *Settings* to configure the translator, OCR language, and hotkeys.
  5. Minimising hides the window to the system tray — the pipeline keeps
     running.

Launch via ``python main.py`` (no flags).
The full debug view is available via ``python main.py --debug`` or the
*Debug view* button inside this window.
"""
from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import QEvent, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QFont, QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.ocr.windows_ocr import _ensure_apartment
from src.target import GameTarget
from ._translator_settings import TranslatorSettingsWidget
from .window_picker import WindowPicker

_cfg = AppConfig()
_log = logging.getLogger(__name__)

# ── Virtual-key codes F1–F12 ───────────────────────────────────────────────────
_VK_FKEYS: list[tuple[str, int]] = [
    ("F1", 0x70), ("F2", 0x71), ("F3", 0x72), ("F4", 0x73),
    ("F5", 0x74), ("F6", 0x75), ("F7", 0x76), ("F8", 0x77),
    ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A), ("F12", 0x7B),
]

# ── BCP-47 tag → DISM capability name ─────────────────────────────────────────
_LANG_CAPABILITIES: dict[str, str] = {
    "ja": "Language.OCR~~~ja-JP~0.0.1.0",
}

# ── Win32: ShellExecuteEx for OCR pack install (elevated) ─────────────────────
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
_kernel32_mw = ctypes.WinDLL("kernel32", use_last_error=True)


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
# Settings dialog  (Translation / OCR & Pipeline / Hotkeys)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _SettingsDialog(QDialog):
    """Three-tab settings dialog.  Writes to :class:`AppConfig` only on OK."""

    def __init__(self, backend: AppBackend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle("Settings — JustReadIt")
        self.setMinimumWidth(540)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        # OCR language-pack install
        self._install_proc_handle: int | None = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(500)
        self._install_timer.timeout.connect(self._poll_install)

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_translation_tab(), "Translation")
        self._tabs.addTab(self._build_ocr_tab(),         "OCR & Pipeline")
        self._tabs.addTab(self._build_hotkeys_tab(),     "Hotkeys")

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._restore_values()

    # ── Translation tab ───────────────────────────────────────────────────

    def _build_translation_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self._tl_settings = TranslatorSettingsWidget(
            self._backend, show_buttons=True, auto_build=False, parent=w
        )
        lay.addWidget(self._tl_settings)
        return w

    # ── OCR & Pipeline tab ────────────────────────────────────────────────

    def _build_ocr_tab(self) -> QWidget:
        w = QWidget()
        lay = QFormLayout(w)
        lay.setSpacing(12)
        lay.setContentsMargins(10, 12, 10, 10)

        self._cmb_lang = QComboBox()
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)
        self._populate_languages()
        lang_row = QHBoxLayout()
        lang_row.addWidget(self._cmb_lang, 1)
        self._lbl_install = QLabel("")
        lang_row.addWidget(self._lbl_install)
        lay.addRow("OCR Language:", lang_row)

        self._spn_max_size = QSpinBox()
        self._spn_max_size.setRange(480, 7680)
        self._spn_max_size.setSingleStep(240)
        self._spn_max_size.setSuffix(" px")
        self._spn_max_size.setToolTip(
            "Maximum long-edge (px) of the image fed to Windows OCR.\n"
            "1920 leaves 1080p images untouched and halves 4K frames.\n"
            "Lower values speed up OCR at the cost of accuracy."
        )
        lay.addRow("OCR max size:", self._spn_max_size)

        self._spn_interval = QSpinBox()
        self._spn_interval.setRange(200, 15000)
        self._spn_interval.setSuffix(" ms")
        self._spn_interval.setToolTip("Cursor poll + translation pipeline interval.")
        lay.addRow("Interval:", self._spn_interval)

        self._chk_mem = QCheckBox("Enable ReadProcessMemory scanning")
        self._chk_mem.setToolTip(
            "Extract cleaner text directly from game memory.\n"
            "Disable for games with very large heaps to reduce stutter."
        )
        lay.addRow("Memory scan:", self._chk_mem)
        return w

    # ── Hotkeys tab ───────────────────────────────────────────────────────

    def _build_hotkeys_tab(self) -> QWidget:
        w = QWidget()
        lay = QFormLayout(w)
        lay.setSpacing(12)
        lay.setContentsMargins(10, 12, 10, 10)

        self._cmb_freeze = QComboBox()
        self._cmb_dump = QComboBox()
        for label, vk in _VK_FKEYS:
            self._cmb_freeze.addItem(label, userData=vk)
            self._cmb_dump.addItem(label, userData=vk)
        self._cmb_freeze.setToolTip("Hotkey to enter / exit Freeze screenshot mode.")
        self._cmb_dump.setToolTip(
            "Hotkey to copy an OCR / memory / translation snapshot to the clipboard."
        )
        lay.addRow("Freeze mode:", self._cmb_freeze)
        lay.addRow("Debug dump:", self._cmb_dump)
        return w

    # ── Persist / restore ─────────────────────────────────────────────────

    def _restore_values(self) -> None:
        # OCR & Pipeline
        self._spn_max_size.setValue(_cfg.ocr_max_size)
        self._spn_interval.setValue(_cfg.interval_ms)
        self._chk_mem.setChecked(_cfg.memory_scan_enabled)
        saved_lang = _cfg.ocr_language
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # Hotkeys
        for cmb, saved_vk in [
            (self._cmb_freeze, _cfg.freeze_vk),
            (self._cmb_dump, _cfg.dump_vk),
        ]:
            for i in range(cmb.count()):
                if cmb.itemData(i) == saved_vk:
                    cmb.setCurrentIndex(i)
                    break

    def accept(self) -> None:  # noqa: N802
        """Persist all settings to :class:`AppConfig` and close."""
        # Translator settings are saved + applied via the embedded widget.
        self._tl_settings.apply()

        # OCR & Pipeline
        _cfg.ocr_language = self._cmb_lang.currentData() or "ja"
        _cfg.ocr_max_size = self._spn_max_size.value()
        _cfg.interval_ms = self._spn_interval.value()
        _cfg.memory_scan_enabled = self._chk_mem.isChecked()
        _cfg.freeze_vk = self._cmb_freeze.currentData() or 0x78
        _cfg.dump_vk = self._cmb_dump.currentData() or 0x77

        super().accept()

    # ── OCR language (for OCR & Pipeline tab) ────────────────────────────

    def _populate_languages(self) -> None:
        try:
            import winrt.windows.media.ocr as wocr
            _ensure_apartment()
            installed: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed.add(tag)
                self._cmb_lang.addItem(tag, userData=tag)
            for tag in _LANG_CAPABILITIES:
                if any(t == tag or t.startswith(tag + "-") for t in installed):
                    continue
                self._cmb_lang.addItem(f"{tag}  ⬇ click Install", userData=tag)
        except Exception as exc:
            self._cmb_lang.addItem(f"(error: {exc})", userData="ja")

    @Slot(int)
    def _on_lang_changed(self, index: int) -> None:
        tag = self._cmb_lang.itemData(index)
        if not tag or tag not in _LANG_CAPABILITIES:
            self._lbl_install.setText("")
            return
        # Check whether the pack is already installed
        try:
            import winrt.windows.media.ocr as wocr
            import winrt.windows.globalization as glob
            _ensure_apartment()
            if wocr.OcrEngine.is_language_supported(glob.Language(tag)):
                self._lbl_install.setText("")
                return
        except Exception:
            pass
        # Offer to install
        self._lbl_install.setText("")
        if QMessageBox.question(
            self,
            "Install OCR Language Pack",
            f"The OCR pack for '{tag}' is not installed.\n\n"
            "Install now? (~6 MB, requires UAC elevation)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._start_install(tag)

    def _start_install(self, lang_tag: str) -> None:
        capability = _LANG_CAPABILITIES[lang_tag]
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
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
            self._lbl_install.setText("UAC denied.")
            return
        self._install_proc_handle = sei.hProcess
        self._lbl_install.setText("Installing…")
        self._install_timer.start()

    @Slot()
    def _poll_install(self) -> None:
        if self._install_proc_handle is None:
            self._install_timer.stop()
            return
        if (
            _kernel32_mw.WaitForSingleObject(
                ctypes.c_void_p(self._install_proc_handle), 0
            )
            != _WAIT_TIMEOUT
        ):
            self._finish_install()

    def _finish_install(self) -> None:
        self._install_timer.stop()
        if self._install_proc_handle is not None:
            _kernel32_mw.CloseHandle(ctypes.c_void_p(self._install_proc_handle))
            self._install_proc_handle = None
        current_tag = self._cmb_lang.currentData()
        self._cmb_lang.currentIndexChanged.disconnect(self._on_lang_changed)
        self._cmb_lang.clear()
        self._populate_languages()
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == current_tag:
                self._cmb_lang.setCurrentIndex(i)
                break
        self._lbl_install.setText("✓ Installed")


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

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # ── Game picker row ───────────────────────────────────────────
        pick_row = QHBoxLayout()
        self._btn_pick = QPushButton("⊕  Pick Game Window")
        self._btn_pick.setFixedHeight(32)
        self._btn_pick.clicked.connect(self._start_picking)
        self._lbl_target = QLabel("No game selected")
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
        self._lbl_status_text = QLabel("Idle \u2014 pick a game window to start")
        self._lbl_status_text.setStyleSheet("color: #888; font-size: 9pt;")
        self._lbl_status_text.setWordWrap(True)
        card_lay.addWidget(self._lbl_dot)
        card_lay.addWidget(self._lbl_status_text, 1)
        lay.addWidget(self._status_card)

        # ── Language quick-select ─────────────────────────────────────
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(0, 0, 0, 0)
        _lbl_ocr = QLabel("OCR:")
        _lbl_ocr.setStyleSheet("color: #777; font-size: 9pt;")
        self._cmb_src_lang = QComboBox()
        self._cmb_src_lang.setMaximumWidth(120)
        self._cmb_src_lang.setToolTip(
            "OCR source language — changes take effect immediately."
        )
        self._populate_src_languages()
        _saved_src = _cfg.ocr_language
        for _i in range(self._cmb_src_lang.count()):
            if self._cmb_src_lang.itemData(_i) == _saved_src:
                self._cmb_src_lang.setCurrentIndex(_i)
                break
        self._cmb_src_lang.currentIndexChanged.connect(self._on_src_lang_changed)
        _lbl_arr = QLabel("\u2192")
        _lbl_arr.setStyleSheet("color: #555; font-size: 9pt;")
        self._lbl_tgt_lang = QLabel(_cfg.translator_target_lang or "en")
        self._lbl_tgt_lang.setStyleSheet("color: #777; font-size: 9pt;")
        lang_row.addWidget(_lbl_ocr)
        lang_row.addSpacing(4)
        lang_row.addWidget(self._cmb_src_lang)
        lang_row.addSpacing(6)
        lang_row.addWidget(_lbl_arr)
        lang_row.addSpacing(4)
        lang_row.addWidget(self._lbl_tgt_lang)
        lang_row.addStretch()
        lay.addLayout(lang_row)

        # ── Translation display ───────────────────────────────────────
        lay.addWidget(_hsep())

        tl_header = QHBoxLayout()
        tl_header.addWidget(QLabel("Last translation:"))
        tl_header.addStretch()
        self._btn_copy = QPushButton("Copy")
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
            "Hover over game text to translate.\n\n"
            "If this area stays blank, make sure a translator backend is "
            "configured in Settings."
        )
        lay.addWidget(self._te_translation, 1)

        lay.addWidget(_hsep())

        # ── Bottom buttons ────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_settings = QPushButton("⚙  Settings")
        self._btn_settings.clicked.connect(self._open_settings)
        self._btn_knowledge = QPushButton("📚  Knowledge")
        self._btn_knowledge.clicked.connect(self._open_knowledge)
        self._btn_debug = QPushButton("🔧  Debug view")
        self._btn_debug.setFlat(True)
        self._btn_debug.setStyleSheet("color: #777;")
        self._btn_debug.clicked.connect(self._open_debug)
        btn_row.addWidget(self._btn_settings)
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
        act_show     = QAction("Show", self)
        act_settings = QAction("⚙ Settings", self)
        act_exit     = QAction("Exit", self)
        act_show.triggered.connect(self._show_from_tray)
        act_settings.triggered.connect(self._open_settings)
        act_exit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_settings)
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
        self._set_status(f"Running  ·  {info}", "#4ec94e")

    # ── Language helpers ─────────────────────────────────────────────────

    def _populate_src_languages(self) -> None:
        """Fill :attr:`_cmb_src_lang` with available Windows OCR languages."""
        try:
            import winrt.windows.media.ocr as wocr  # noqa: PLC0415
            _ensure_apartment()
            installed: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed.add(tag)
                self._cmb_src_lang.addItem(tag, userData=tag)
            for tag in _LANG_CAPABILITIES:
                if any(t == tag or t.startswith(tag + "-") for t in installed):
                    continue
                self._cmb_src_lang.addItem(f"{tag}  \u2b07 Install", userData=tag)
        except Exception as exc:
            self._cmb_src_lang.addItem(f"(error: {exc})", userData="ja")

    @Slot(int)
    def _on_src_lang_changed(self, index: int) -> None:
        """Persist the selected OCR language and restart the controller."""
        tag = self._cmb_src_lang.itemData(index)
        if not tag:
            return
        _cfg.ocr_language = tag
        if self._backend.is_running:
            self._backend.start()

    def _sync_lang_display(self) -> None:
        """Sync the source-lang combo and target-lang label from :class:`AppConfig`."""
        saved = _cfg.ocr_language
        for i in range(self._cmb_src_lang.count()):
            if self._cmb_src_lang.itemData(i) == saved:
                if self._cmb_src_lang.currentIndex() != i:
                    self._cmb_src_lang.blockSignals(True)
                    self._cmb_src_lang.setCurrentIndex(i)
                    self._cmb_src_lang.blockSignals(False)
                break
        self._lbl_tgt_lang.setText(_cfg.translator_target_lang or "en")

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
        self._stop()
        self._target = target
        w = target.window_rect.width
        h = target.window_rect.height
        self._lbl_target.setText(
            f"{target.process_name}  (PID {target.pid})  [{w}×{h}]"
        )
        self._lbl_target.setStyleSheet("color: #ddd;")
        self._run()

    # ── Controller lifecycle ──────────────────────────────────────────────

    def _run(self) -> None:
        """Delegate pipeline start to :class:`AppBackend`."""
        self._backend.start()

    def _stop(self) -> None:
        self._backend.stop()

    def _rebuild_translator(self, *, silent: bool = False) -> None:
        """Delegate translator rebuild to :class:`AppBackend`."""
        err = self._backend.rebuild_translator(silent=silent)
        if err and not silent:
            QMessageBox.warning(self, "Translator error", err)

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
        self._set_status(f"Translating…  ({step})", "#e0c840")

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
        freeze_key_name = ""
        for label, vk in _VK_FKEYS:
            if vk == _cfg.freeze_vk:
                freeze_key_name = label
                break
        self._set_status(
            f"❄ Freeze mode — right-click or Esc to exit  ({freeze_key_name})",
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
    def _open_settings(self) -> None:
        dlg = _SettingsDialog(self._backend, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._sync_lang_display()
        # Restart picks up all new config values (language, interval, hotkeys).
        if self._backend.target is not None:
            self._backend.start()

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
                "Translation running in background.  Click tray icon to restore.",
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
