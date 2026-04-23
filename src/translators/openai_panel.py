# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Settings panel for the OpenAI-compatible API backend.

Contains all backend-specific UI fields (API key, model, base URL, system
prompt, context window, etc.) and delegates config persistence and translator
construction.  Satisfies the panel contract defined in
:mod:`src.translators._panel_base`.

The :data:`OPENAI_PRESETS` list and :data:`DEFAULT_SYSTEM_PROMPT` constant are
imported directly from :mod:`src.translators.openai_translator` so that both
the UI and the translator share a single source of truth.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.translators.openai_translator import DEFAULT_SYSTEM_PROMPT, OPENAI_PRESETS

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.config import AppConfig
    from src.translators.base import Translator


class Panel(QWidget):
    """Settings panel for any OpenAI-compatible endpoint.

    Fields provided:
    - API key (password-masked)
    - Quick preset selector (auto-fills model / URL / system-prompt)
    - Model name
    - Base URL
    - System prompt (multi-line)
    - Context window / summary trigger
    - KB tool-calling toggle
    - Disable-thinking toggle
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ── API key ────────────────────────────────────────────────────
        row_key = QHBoxLayout()
        row_key.addWidget(QLabel("API 密钥:"))
        self._le_api_key = QLineEdit()
        self._le_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_api_key.setPlaceholderText(
            "在此粘贴 API 密钥（本地模型留空）"
        )
        row_key.addWidget(self._le_api_key)
        lay.addLayout(row_key)

        # ── Quick presets ──────────────────────────────────────────────
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("快速预设:"))
        self._cmb_preset = QComboBox()
        self._cmb_preset.addItem("— 选择预设自动填充 —", userData=None)
        for _p in OPENAI_PRESETS:
            self._cmb_preset.addItem(_p.label, userData=_p)
        self._cmb_preset.setToolTip("自动填充下方字段 — 点击「应用」保存。")
        preset_row.addWidget(self._cmb_preset, 1)
        lay.addLayout(preset_row)

        # ── Model + base URL ───────────────────────────────────────────
        row_model = QHBoxLayout()
        row_model.addWidget(QLabel("模型:"))
        self._le_model = QLineEdit()
        self._le_model.setPlaceholderText("gpt-4o-mini")
        self._le_model.setMaximumWidth(160)
        row_model.addWidget(self._le_model)
        row_model.addSpacing(12)
        row_model.addWidget(QLabel("基础 URL:"))
        self._le_base_url = QLineEdit()
        self._le_base_url.setPlaceholderText(
            "https://api.openai.com/v1  (留空 = 默认)"
        )
        row_model.addWidget(self._le_base_url)
        lay.addLayout(row_model)

        # ── System prompt ──────────────────────────────────────────────
        row_prompt = QHBoxLayout()
        row_prompt.addWidget(QLabel("系统提示词:"))
        self._te_system_prompt = QTextEdit()
        self._te_system_prompt.setPlaceholderText(
            "支持 {source_lang} 和 {target_lang} 占位符。"
        )
        self._te_system_prompt.setFixedHeight(72)
        row_prompt.addWidget(self._te_system_prompt)
        lay.addLayout(row_prompt)

        # ── Context / summary / flags ──────────────────────────────────
        row_ctx = QHBoxLayout()
        row_ctx.addWidget(QLabel("上下文:"))
        self._spn_context_window = QSpinBox()
        self._spn_context_window.setRange(0, 100)
        self._spn_context_window.setMaximumWidth(72)
        self._spn_context_window.setToolTip("作为上下文包含的最近翻译对数量。")
        row_ctx.addWidget(self._spn_context_window)
        row_ctx.addSpacing(12)
        row_ctx.addWidget(QLabel("摘要:"))
        self._spn_summary_trigger = QSpinBox()
        self._spn_summary_trigger.setRange(0, 200)
        self._spn_summary_trigger.setMaximumWidth(72)
        self._spn_summary_trigger.setToolTip("触发最旧片段摘要的历史长度。")
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
        lay.addLayout(row_ctx)

        # Wire preset selector
        self._cmb_preset.currentIndexChanged.connect(self._on_preset_selected)

    # ------------------------------------------------------------------
    # Panel contract
    # ------------------------------------------------------------------

    def load_from_config(self, cfg: "AppConfig") -> None:
        oa = cfg.translator.backends.openai
        self._le_api_key.setText(oa.api_key)
        self._le_model.setText(oa.model)
        self._le_base_url.setText(oa.base_url)
        self._te_system_prompt.setPlainText(oa.system_prompt or DEFAULT_SYSTEM_PROMPT)
        self._spn_context_window.setValue(oa.context_window)
        self._spn_summary_trigger.setValue(oa.summary_trigger)
        self._chk_tools_enabled.setChecked(oa.tools_enabled)
        self._chk_disable_thinking.setChecked(oa.disable_thinking)

    def save_to_config(self, cfg: "AppConfig") -> None:
        oa = cfg.translator.backends.openai
        oa.api_key = self._le_api_key.text().strip()
        oa.model = self._le_model.text().strip() or "gpt-4o-mini"
        oa.base_url = self._le_base_url.text().strip()
        oa.system_prompt = self._te_system_prompt.toPlainText().strip()
        oa.context_window = self._spn_context_window.value()
        oa.summary_trigger = self._spn_summary_trigger.value()
        oa.tools_enabled = self._chk_tools_enabled.isChecked()
        oa.disable_thinking = self._chk_disable_thinking.isChecked()

    def build_translator(
        self,
        cfg: "AppConfig",  # noqa: ARG002
        *,
        progress: "Callable[[str], None] | None" = None,
        knowledge_base: object = None,
    ) -> "Translator":
        from src.translators.openai_translator import OpenAICompatTranslator  # noqa: PLC0415
        return OpenAICompatTranslator(
            api_key=self._le_api_key.text().strip(),
            model=self._le_model.text().strip() or "gpt-4o-mini",
            system_prompt=self._te_system_prompt.toPlainText().strip(),
            context_window=self._spn_context_window.value(),
            base_url=self._le_base_url.text().strip() or None,
            knowledge_base=knowledge_base,  # type: ignore[arg-type]
            tools_enabled=self._chk_tools_enabled.isChecked(),
            disable_thinking=self._chk_disable_thinking.isChecked(),
            progress=progress,
        )

    def connect_dirty(self, slot: "Callable[[], None]") -> None:
        self._le_api_key.textChanged.connect(slot)
        self._le_model.textChanged.connect(slot)
        self._le_base_url.textChanged.connect(slot)
        self._te_system_prompt.textChanged.connect(slot)
        self._spn_context_window.valueChanged.connect(slot)
        self._spn_summary_trigger.valueChanged.connect(slot)
        self._chk_tools_enabled.toggled.connect(slot)
        self._chk_disable_thinking.toggled.connect(slot)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

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
