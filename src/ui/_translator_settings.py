# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared translator-settings panel used by both MainWindow and DebugWindow.

Extracted to avoid duplicate code and ensure feature parity between the
compact settings dialog and the inline debug panel.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.languages import TARGET_PRESETS, display_name
from src.translators.base import PROVIDERS, PROVIDERS_BY_KEY
from src.translators.factory import build_translator
from src.translators.openai_translator import DEFAULT_SYSTEM_PROMPT, OPENAI_PRESETS

if TYPE_CHECKING:
    from collections.abc import Callable
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
        self._build_ui()
        self.load_from_config()
        self._on_backend_changed(self._cmb_backend.currentIndex())

        # Show existing translator status or attempt initial build.
        if backend.translator is not None:
            self._set_status(
                f"✓ {_cfg.translator.backend.title()} 就绪"
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

        # ── Row 1: backend + target language ──────────────────────────
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

        # ── Row 2: API key ─────────────────────────────────────────────
        self._row_api_key = QWidget()
        r2 = QHBoxLayout(self._row_api_key)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(QLabel("API 密钥:"))
        self._le_api_key = QLineEdit()
        self._le_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_api_key.setPlaceholderText(
            "在此粘贴 API 密钥（本地模型留空）"
        )
        r2.addWidget(self._le_api_key)
        lay.addWidget(self._row_api_key)

        # ── OpenAI / compatible-endpoint fields ────────────────────────
        self._openai_fields = QWidget()
        of_lay = QVBoxLayout(self._openai_fields)
        of_lay.setContentsMargins(0, 0, 0, 0)
        of_lay.setSpacing(4)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("快速预设:"))
        self._cmb_preset = QComboBox()
        self._cmb_preset.addItem("— 选择预设自动填充 —", userData=None)
        for _p in OPENAI_PRESETS:
            self._cmb_preset.addItem(_p.label, userData=_p)
        self._cmb_preset.setToolTip(
            "自动填充下方字段 — 点击「应用」保存。"
        )
        preset_row.addWidget(self._cmb_preset, 1)
        of_lay.addLayout(preset_row)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("模型:"))
        self._le_model = QLineEdit()
        self._le_model.setPlaceholderText("gpt-4o-mini")
        self._le_model.setMaximumWidth(160)
        row3.addWidget(self._le_model)
        row3.addSpacing(12)
        row3.addWidget(QLabel("基础 URL:"))
        self._le_base_url = QLineEdit()
        self._le_base_url.setPlaceholderText(
            "https://api.openai.com/v1  (留空 = 默认)"
        )
        row3.addWidget(self._le_base_url)
        of_lay.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("系统提示词:"))
        self._te_system_prompt = QTextEdit()
        self._te_system_prompt.setPlaceholderText(
            "支持 {source_lang} 和 {target_lang} 占位符。"
        )
        self._te_system_prompt.setFixedHeight(72)
        row4.addWidget(self._te_system_prompt)
        of_lay.addLayout(row4)

        row_ctx = QHBoxLayout()
        row_ctx.addWidget(QLabel("上下文:"))
        self._spn_context_window = QSpinBox()
        self._spn_context_window.setRange(0, 100)
        self._spn_context_window.setMaximumWidth(72)
        self._spn_context_window.setToolTip(
            "作为上下文包含的最近翻译对数量。"
        )
        row_ctx.addWidget(self._spn_context_window)
        row_ctx.addSpacing(12)
        row_ctx.addWidget(QLabel("摘要:"))
        self._spn_summary_trigger = QSpinBox()
        self._spn_summary_trigger.setRange(0, 200)
        self._spn_summary_trigger.setMaximumWidth(72)
        self._spn_summary_trigger.setToolTip(
            "触发最旧片段摘要的历史长度。"
        )
        row_ctx.addWidget(self._spn_summary_trigger)
        row_ctx.addSpacing(16)
        self._chk_tools_enabled = QCheckBox("KB 工具调用")
        self._chk_tools_enabled.setToolTip(
            "允许模型通过 function-calling 读写知识库。\n"
            "对于不擅长工具提示的小模型请禁用。"
        )
        row_ctx.addWidget(self._chk_tools_enabled)
        row_ctx.addSpacing(8)
        self._chk_disable_thinking = QCheckBox("禁止推理")
        self._chk_disable_thinking.setToolTip(
            "在开头添加空 <think></think> 以抑制思维链。\n"
            "仅用于本地思维模型（DeepSeek-R1-Distill、QwQ 等）。\n"
            "切勿对标准 OpenAI API 启用。"
        )
        row_ctx.addWidget(self._chk_disable_thinking)
        row_ctx.addStretch()
        of_lay.addLayout(row_ctx)

        lay.addWidget(self._openai_fields)

        # ── Status label (always present) ──────────────────────────────
        self._lbl_status = QLabel("")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 8pt;")

        # ── Buttons row ────────────────────────────────────────────────
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

        # Wire backend/preset selectors
        self._cmb_backend.currentIndexChanged.connect(self._on_backend_changed)
        self._cmb_preset.currentIndexChanged.connect(self._on_preset_selected)

        # Reactive config → widget sync: keep target-lang combo current when
        # another view (e.g. main window) changes the setting.
        _cfg.translator.target_lang_changed.connect(self._sync_target_lang)

        # Dirty-state tracking: any field change → remind user to apply.
        self._cmb_backend.currentIndexChanged.connect(self._mark_dirty)
        self._cmb_target_lang.currentIndexChanged.connect(self._mark_dirty)
        self._le_api_key.textChanged.connect(self._mark_dirty)
        self._le_model.textChanged.connect(self._mark_dirty)
        self._le_base_url.textChanged.connect(self._mark_dirty)
        self._te_system_prompt.textChanged.connect(self._mark_dirty)
        self._spn_context_window.valueChanged.connect(self._mark_dirty)
        self._spn_summary_trigger.valueChanged.connect(self._mark_dirty)
        self._chk_tools_enabled.toggled.connect(self._mark_dirty)
        self._chk_disable_thinking.toggled.connect(self._mark_dirty)

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

        All backend-specific fields are always loaded from their respective
        stored values, so the UI is ready immediately when the user switches
        the backend combo without needing an intermediate «apply».
        """
        backend = _cfg.translator.backend
        for i in range(self._cmb_backend.count()):
            if self._cmb_backend.itemData(i) == backend:
                self._cmb_backend.setCurrentIndex(i)
                break

        self.set_target_lang(_cfg.translator.target_lang or "zh-Hans-CN")

        # Always load the API key for the currently active backend.
        if backend == "openai":
            self._le_api_key.setText(_cfg.translator.backends.openai.api_key)
        elif backend == "cloud":
            self._le_api_key.setText(_cfg.translator.backends.cloud.api_key)
        else:
            self._le_api_key.setText("")

        # Always load all OpenAI-specific fields so they are ready when the
        # user switches the backend combo to "openai".
        self._le_model.setText(_cfg.translator.backends.openai.model)
        self._le_base_url.setText(_cfg.translator.backends.openai.base_url)
        self._te_system_prompt.setPlainText(
            _cfg.translator.backends.openai.system_prompt or DEFAULT_SYSTEM_PROMPT
        )
        self._spn_context_window.setValue(_cfg.translator.backends.openai.context_window)
        self._spn_summary_trigger.setValue(_cfg.translator.backends.openai.summary_trigger)
        self._chk_tools_enabled.setChecked(_cfg.translator.backends.openai.tools_enabled)
        self._chk_disable_thinking.setChecked(_cfg.translator.backends.openai.disable_thinking)

        # Reset dirty flag after loading — field changes above were not user edits.
        self._dirty = False

    def save_to_config(self) -> None:
        """Persist all translator settings to :class:`AppConfig` without building.

        All backend-specific settings are always persisted regardless of the
        currently active backend, so switching between backends never discards
        settings that were already filled in.
        """
        backend = self._cmb_backend.currentData() or "none"
        api_key = self._le_api_key.text().strip()
        _cfg.translator.backend = backend
        _cfg.translator.target_lang = self.target_lang_value()
        # Always persist the API key for the active backend so that unsaved
        # edits survive an accidental backend switch.
        if backend == "cloud":
            _cfg.translator.backends.cloud.api_key = api_key
        elif backend == "openai":
            _cfg.translator.backends.openai.api_key = api_key
        # Always persist all OpenAI-specific fields — they are stored under
        # dedicated config keys and are invisible to the cloud / google_free
        # backends, so saving them unconditionally is safe and ensures they
        # survive a temporary switch to another backend.
        _cfg.translator.backends.openai.model = self._le_model.text().strip() or "gpt-4o-mini"
        _cfg.translator.backends.openai.base_url = self._le_base_url.text().strip()
        _cfg.translator.backends.openai.system_prompt = (
            self._te_system_prompt.toPlainText().strip()
        )
        _cfg.translator.backends.openai.context_window = self._spn_context_window.value()
        _cfg.translator.backends.openai.summary_trigger = self._spn_summary_trigger.value()
        _cfg.translator.backends.openai.tools_enabled = self._chk_tools_enabled.isChecked()
        _cfg.translator.backends.openai.disable_thinking = self._chk_disable_thinking.isChecked()

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
        self._set_status("⚠ 设置已修改，点击「应用」保存并重新构建翻译器。")

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
                    f"✓ {backend_key.title()} 就绪 → {target_lang}"
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
        api_key = self._le_api_key.text().strip()
        progress: Callable[[str], None] = lambda _: None  # noqa: E731
        if backend == "google_free":
            from src.translators.google_free import GoogleFreeTranslator  # noqa: PLC0415
            return GoogleFreeTranslator(progress=progress)
        if backend == "cloud":
            from src.translators.google_cloud_translation import GoogleCloudTranslator  # noqa: PLC0415
            return GoogleCloudTranslator(api_key=api_key or None, progress=progress)
        if backend == "openai":
            from src.translators.openai_translator import OpenAICompatTranslator  # noqa: PLC0415
            return OpenAICompatTranslator(
                api_key=api_key,
                model=self._le_model.text().strip() or "gpt-4o-mini",
                system_prompt=self._te_system_prompt.toPlainText().strip(),
                context_window=self._spn_context_window.value(),
                base_url=self._le_base_url.text().strip() or None,
                knowledge_base=self._backend.knowledge_base,
                tools_enabled=self._chk_tools_enabled.isChecked(),
                disable_thinking=self._chk_disable_thinking.isChecked(),
                progress=progress,
            )
        return None

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
            self._set_status(f"✓  {result!r}")
        except Exception as exc:
            self._set_status(f"⚠ {exc}")

    def _save_current_api_key(self) -> None:
        """Persist the API key currently shown in the input field.

        Called before switching backends so that unsaved keystrokes are not
        silently discarded when ``_on_backend_changed`` overwrites the field.
        """
        current_backend = self._cmb_backend.currentData() or "none"
        api_key = self._le_api_key.text().strip()
        if current_backend == "cloud":
            _cfg.translator.backends.cloud.api_key = api_key
        elif current_backend == "openai":
            _cfg.translator.backends.openai.api_key = api_key

    @Slot(int)
    def _on_backend_changed(self, index: int) -> None:
        # Persist the outgoing backend's API key before overwriting the field.
        self._save_current_api_key()
        backend = self._cmb_backend.itemData(index) or "none"
        info = PROVIDERS_BY_KEY.get(backend)
        self._row_api_key.setVisible(bool(info and info.needs_api_key))
        self._openai_fields.setVisible(backend == "openai")
        if backend == "openai":
            self._le_api_key.setText(_cfg.translator.backends.openai.api_key)
        elif backend == "cloud":
            self._le_api_key.setText(_cfg.translator.backends.cloud.api_key)

    @Slot(int)
    def _on_preset_selected(self, index: int) -> None:
        preset = self._cmb_preset.itemData(index)
        if preset is None:
            return
        self._le_model.setPlaceholderText(preset.model_placeholder)
        self._le_base_url.setPlaceholderText(preset.base_url_placeholder)
        self._chk_tools_enabled.setChecked(preset.tools_enabled)
        self._chk_disable_thinking.setChecked(preset.disable_thinking)
        self._spn_context_window.setValue(preset.context_window)
        self._spn_summary_trigger.setValue(preset.summary_trigger)
        self._te_system_prompt.setPlainText(preset.system_prompt)
        self._cmb_preset.blockSignals(True)
        self._cmb_preset.setCurrentIndex(0)
        self._cmb_preset.blockSignals(False)
