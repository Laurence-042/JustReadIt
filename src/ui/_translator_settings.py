# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared translator-settings panel used by both MainWindow and DebugWindow.

Extracted to avoid duplicate code and ensure feature parity between the
compact settings dialog and the inline debug panel.

Architecture
------------
Translator-specific configuration (API keys, model settings, …) lives in
*panel* modules co-located with each translator backend.  Every panel module
exports a ``Panel`` class that satisfies the contract described in
:mod:`src.translators._panel_base` — ``load_from_config``, ``save_to_config``,
``build_translator``, and ``connect_dirty``.

:class:`TranslatorSettingsWidget` is backend-agnostic: it owns a
:class:`~PySide6.QtWidgets.QStackedWidget` of panel instances and delegates
all per-backend operations to whichever panel is currently visible.  Adding a
new translator backend only requires shipping a companion ``*_panel.py``
module — no changes to this file are needed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.languages import TARGET_PRESETS, display_name
from src.translators._panel_base import PANEL_REGISTRY, get_panel_class
from src.translators.base import PROVIDERS
from src.translators.factory import build_translator

if TYPE_CHECKING:
    from src.translators.base import Translator

_cfg = AppConfig()


# ---------------------------------------------------------------------------
# Shared widget
# ---------------------------------------------------------------------------

class TranslatorSettingsWidget(QWidget):
    """Self-contained translator-configuration panel.

    Reads/writes :class:`AppConfig` directly.  Propagates a built translator
    to :class:`AppBackend` via :meth:`~AppBackend.set_translator`.

    Parameters
    ----------
    backend:
        The shared application backend.
    auto_build:
        When *True* (default), attempt to build the translator on construction
        if a backend is configured in :class:`AppConfig`.  Set *False* in
        modal dialogs where building should only happen on OK / Apply.
    """

    #: Emitted after the translator is successfully applied (or disabled).
    translator_applied = Signal()

    def __init__(
        self,
        backend: AppBackend,
        *,
        auto_build: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._dirty = False
        self._panels: dict[str, QWidget] = {}
        self._build_ui()
        self.load_from_config()

        # Show existing translator status or attempt initial build.
        if backend.translator is not None:
            self._set_status(
                f"✔ {_cfg.translator.backend.title()} 就绪"
                f" → {_cfg.translator.target_lang}"
            )
        elif auto_build and _cfg.translator.backend not in ("none", ""):
            self._build_from_config()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ── Row 1: backend + target language ──────────────────────────────
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("翻译后端:"))
        self._cmb_backend = QComboBox()
        self._cmb_backend.addItem("— 无 —", userData="none")
        for p in PROVIDERS:
            self._cmb_backend.addItem(p.display_name, userData=p.key)
        row1.addWidget(self._cmb_backend)

        row1.addSpacing(12)
        row1.addWidget(QLabel("目标语言:"))
        self._cmb_target_lang = QComboBox()
        self._cmb_target_lang.setMinimumWidth(140)
        for code in TARGET_PRESETS:
            self._cmb_target_lang.addItem(display_name(code), userData=code)
        self._cmb_target_lang.setToolTip(
            "BCP-47 目标语言标签（如 zh-Hans-CN、en、ko）。"
        )
        row1.addWidget(self._cmb_target_lang)
        row1.addStretch()
        lay.addLayout(row1)

        # ── Stacked panel area (backend-specific fields) ────────────────
        self._stack = QStackedWidget()

        # "none" slot: empty placeholder
        self._empty_panel = QWidget()
        self._stack.addWidget(self._empty_panel)

        # Pre-create a panel for every registered backend key so that each
        # panel's fields are initialised before the user switches backends.
        for key in PANEL_REGISTRY:
            panel_cls = get_panel_class(key)
            if panel_cls is not None:
                panel = panel_cls(self)  # type: ignore[call-arg]
                panel.connect_dirty(self._mark_dirty)  # type: ignore[attr-defined]
                self._panels[key] = panel
                self._stack.addWidget(panel)

        lay.addWidget(self._stack)

        # ── Status label (always present) ──────────────────────────────────
        self._lbl_status = QLabel("")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 8pt;")

        # ── Buttons row ──────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_apply = QPushButton("应用")
        self._btn_apply.setToolTip(
            "保存设置并（重新）初始化翻译器。\n"
            "缺少的依赖包会自动安装。"
        )
        self._btn_test = QPushButton("测试")
        self._btn_test.setToolTip(
            "发送一段测试文本以验证翻译器是否正常工作。"
        )
        btn_row.addWidget(self._btn_apply)
        btn_row.addWidget(self._btn_test)
        btn_row.addWidget(self._lbl_status, 1)
        lay.addLayout(btn_row)

        self._btn_apply.clicked.connect(self._on_apply)
        self._btn_test.clicked.connect(self._on_test)

        # Wire backend selector and reactive target-lang sync.
        self._cmb_backend.currentIndexChanged.connect(self._on_backend_changed)
        _cfg.translator.target_lang_changed.connect(self._sync_target_lang)

        # Dirty-state tracking for the common fields.
        self._cmb_backend.currentIndexChanged.connect(self._mark_dirty)
        self._cmb_target_lang.currentIndexChanged.connect(self._mark_dirty)

    # ------------------------------------------------------------------
    # Helpers: current panel
    # ------------------------------------------------------------------

    def _current_panel(self) -> "QWidget | None":
        """Return the panel widget for the currently selected backend, or None."""
        backend = self._cmb_backend.currentData() or "none"
        return self._panels.get(backend)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def target_lang_value(self) -> str:
        """Return the current BCP-47 target language code."""
        idx = self._cmb_target_lang.currentIndex()
        if idx >= 0:
            data = self._cmb_target_lang.itemData(idx)
            if data:
                return str(data)
        return "zh-Hans-CN"

    def load_from_config(self) -> None:
        """Populate all fields from :class:`AppConfig`.

        All backend panels are loaded from their respective stored values so
        the UI is ready immediately when the user switches the backend combo.
        """
        backend = _cfg.translator.backend
        for i in range(self._cmb_backend.count()):
            if self._cmb_backend.itemData(i) == backend:
                with QSignalBlocker(self._cmb_backend):
                    self._cmb_backend.setCurrentIndex(i)
                break

        self.set_target_lang(_cfg.translator.target_lang or "zh-Hans-CN")

        # Load every panel so that fields are initialised before the user
        # switches backends.
        for panel in self._panels.values():
            panel.load_from_config(_cfg)  # type: ignore[attr-defined]

        # Show the panel that matches the stored backend.
        self._show_panel_for(backend)

        # Reset dirty flag after loading 鈥?field population above was not a
        # user edit.
        self._dirty = False

    def save_to_config(self) -> None:
        """Persist all translator settings to :class:`AppConfig` without building.

        Only the **active** panel's settings are saved.  Other panels keep
        their own persisted values from the last time they were applied.
        """
        backend = self._cmb_backend.currentData() or "none"
        _cfg.translator.backend = backend
        _cfg.translator.target_lang = self.target_lang_value()
        panel = self._current_panel()
        if panel is not None:
            panel.save_to_config(_cfg)  # type: ignore[attr-defined]

    def apply(self) -> None:
        """Save settings to config and rebuild the translator via AppBackend."""
        self._dirty = False
        self.save_to_config()
        if _cfg.translator.backend == "none":
            self._backend.set_translator(None)
            self._set_status("翻译器已禁用。")
            self.translator_applied.emit()
            return
        self._build_from_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._lbl_status.setText(text)

    @Slot()
    def _mark_dirty(self) -> None:
        """Called whenever any settings field changes; reminds user to apply."""
        if self._dirty:
            return
        self._dirty = True
        self._set_status("⚠ 设置已修改，点击『应用』保存并重新构建翻译器。")

    def set_target_lang(self, tag: str) -> None:
        """Set the target-lang combo to the given BCP-47 tag."""
        for i in range(self._cmb_target_lang.count()):
            if self._cmb_target_lang.itemData(i) == tag:
                self._cmb_target_lang.setCurrentIndex(i)
                return

    @Slot(str)
    def _sync_target_lang(self, tag: str) -> None:
        """React to config change: update target-lang combo without feedback."""
        with QSignalBlocker(self._cmb_target_lang):
            self.set_target_lang(tag)

    def _show_panel_for(self, backend: str) -> None:
        """Switch the stacked widget to the panel for *backend*."""
        panel = self._panels.get(backend)
        if panel is not None:
            self._stack.setCurrentWidget(panel)
        else:
            self._stack.setCurrentWidget(self._empty_panel)

    def _build_from_config(self) -> None:
        """Build translator from saved AppConfig and inject into AppBackend."""
        backend_key = _cfg.translator.backend
        target_lang = _cfg.translator.target_lang or "zh-Hans-CN"
        self._set_status("构建中…")
        QApplication.processEvents()
        try:
            translator = build_translator(
                _cfg,
                knowledge_base=self._backend.knowledge_base,
                progress=lambda msg: (
                    self._set_status(msg),
                    QApplication.processEvents(),
                ),
            )
            self._backend.set_translator(translator)
            if translator is not None:
                self._set_status(
                    f"✔ {backend_key.title()} 就绪 → {target_lang}"
                )
            else:
                self._set_status("翻译器已禁用。")
            self.translator_applied.emit()
        except RuntimeError as exc:
            self._backend.set_translator(None)
            self._set_status(f"⚠ {exc}")

    def _build_temp_translator(self) -> "Translator | None":
        """Build a one-shot translator from current UI fields (no config save)."""
        backend = self._cmb_backend.currentData() or "none"
        if backend in ("none", ""):
            return None
        panel = self._current_panel()
        if panel is None:
            return None
        return panel.build_translator(  # type: ignore[attr-defined]
            _cfg,
            progress=lambda _: None,
            knowledge_base=self._backend.knowledge_base,
        )

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    @Slot()
    def _on_apply(self) -> None:
        self.apply()

    @Slot()
    def _on_test(self) -> None:
        target_lang = self.target_lang_value()
        self._set_status("构建中…")
        QApplication.processEvents()
        try:
            translator = self._build_temp_translator()
        except RuntimeError as exc:
            self._set_status(f"⚠ {exc}")
            return
        if translator is None:
            self._set_status("未选择翻译后端。")
            return
        self._set_status("测试中…")
        QApplication.processEvents()
        try:
            result = translator.translate(
                "こんにちは、世界！", target_lang=target_lang
            )
            self._set_status(f"✔ {result!r}")
        except Exception as exc:
            self._set_status(f"⚠ {exc}")

    @Slot(int)
    def _on_backend_changed(self, index: int) -> None:
        backend = self._cmb_backend.itemData(index) or "none"
        self._show_panel_for(backend)

