# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared translator-settings panel used by both MainWindow and DebugWindow.

Extracted to avoid duplicate code and ensure feature parity between the
compact settings dialog and the inline debug panel.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal, Slot
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
from src.translators.base import PROVIDERS, PROVIDERS_BY_KEY
from src.translators.factory import build_translator
from src.translators.openai_translator import DEFAULT_SYSTEM_PROMPT, OPENAI_PRESETS

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.translators.base import Translator

_cfg = AppConfig()

# ---------------------------------------------------------------------------
# BCP-47 target-language presets
# ---------------------------------------------------------------------------

_BCP47_TARGETS: list[tuple[str, str]] = [
    ("zh-CN", "zh-CN — 简体中文"),
    ("zh-TW", "zh-TW — 繁體中文"),
    ("en",    "en — English"),
    ("ko",    "ko — 한국어"),
    ("fr",    "fr — Français"),
    ("de",    "de — Deutsch"),
    ("ja",    "ja — 日本語"),
    ("es",    "es — Español"),
    ("pt",    "pt — Português"),
    ("ru",    "ru — Русский"),
    ("ar",    "ar — العربية"),
    ("it",    "it — Italiano"),
]


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
    show_buttons:
        When *True* (default), renders Apply / Test / Clear-cache buttons and
        a status label inline.  Set *False* when the host dialog provides its
        own OK/Cancel flow and will call :meth:`apply` explicitly.
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
        show_buttons: bool = True,
        auto_build: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._build_ui(show_buttons=show_buttons)
        self.load_from_config()
        self._on_backend_changed(self._cmb_backend.currentIndex())

        # Show existing translator status or attempt initial build.
        if backend.translator is not None:
            self._set_status(
                f"✓ {_cfg.translator_backend.title()} ready"
                f" → {_cfg.translator_target_lang}"
            )
        elif auto_build and _cfg.translator_backend not in ("none", ""):
            self._build_from_config()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, *, show_buttons: bool) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ── Row 1: backend + target language ──────────────────────────
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Backend:"))
        self._cmb_backend = QComboBox()
        self._cmb_backend.addItem("— None —", userData="none")
        for p in PROVIDERS:
            self._cmb_backend.addItem(p.display_name, userData=p.key)
        row1.addWidget(self._cmb_backend)

        row1.addSpacing(12)
        row1.addWidget(QLabel("目标语言:"))
        self._cmb_target_lang = QComboBox()
        self._cmb_target_lang.setEditable(True)
        self._cmb_target_lang.setMinimumWidth(140)
        for code, label in _BCP47_TARGETS:
            self._cmb_target_lang.addItem(label, userData=code)
        self._cmb_target_lang.setToolTip(
            "BCP-47 target language tag (e.g. zh-CN, en, ko).\n"
            "Select from the list or type a custom tag directly."
        )
        row1.addWidget(self._cmb_target_lang)
        row1.addStretch()
        lay.addLayout(row1)

        # ── Row 2: API key ─────────────────────────────────────────────
        self._row_api_key = QWidget()
        r2 = QHBoxLayout(self._row_api_key)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(QLabel("API Key:"))
        self._le_api_key = QLineEdit()
        self._le_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_api_key.setPlaceholderText(
            "Paste API key here  (local models: leave blank)"
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
            "Auto-fills fields below — click Apply to save."
        )
        preset_row.addWidget(self._cmb_preset, 1)
        of_lay.addLayout(preset_row)

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
            "https://api.openai.com/v1  (blank = default)"
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
        self._btn_reset_prompt = QPushButton("Reset")
        self._btn_reset_prompt.setFlat(True)
        self._btn_reset_prompt.setToolTip("Restore the built-in default system prompt.")
        self._btn_reset_prompt.clicked.connect(self._on_reset_prompt)
        prompt_col.addWidget(self._te_system_prompt)
        _btn_row = QHBoxLayout()
        _btn_row.addStretch()
        _btn_row.addWidget(self._btn_reset_prompt)
        prompt_col.addLayout(_btn_row)
        row4.addLayout(prompt_col)
        of_lay.addLayout(row4)

        row_ctx = QHBoxLayout()
        row_ctx.addWidget(QLabel("Context:"))
        self._spn_context_window = QSpinBox()
        self._spn_context_window.setRange(0, 100)
        self._spn_context_window.setMaximumWidth(72)
        self._spn_context_window.setToolTip(
            "Number of recent translation pairs included as context."
        )
        row_ctx.addWidget(self._spn_context_window)
        row_ctx.addSpacing(12)
        row_ctx.addWidget(QLabel("Summary:"))
        self._spn_summary_trigger = QSpinBox()
        self._spn_summary_trigger.setRange(0, 200)
        self._spn_summary_trigger.setMaximumWidth(72)
        self._spn_summary_trigger.setToolTip(
            "History length that triggers summarisation of the oldest chunk."
        )
        row_ctx.addWidget(self._spn_summary_trigger)
        row_ctx.addSpacing(16)
        self._chk_tools_enabled = QCheckBox("KB工具调用")
        self._chk_tools_enabled.setToolTip(
            "Allow the model to read/write the knowledge base via function-calling.\n"
            "Disable for small models that struggle with tool prompts."
        )
        row_ctx.addWidget(self._chk_tools_enabled)
        row_ctx.addSpacing(8)
        self._chk_disable_thinking = QCheckBox("禁止thinking")
        self._chk_disable_thinking.setToolTip(
            "Prepend empty <think></think> to suppress chain-of-thought.\n"
            "Only for local thinking models (DeepSeek-R1-Distill, QwQ…).\n"
            "Never enable for the standard OpenAI API."
        )
        row_ctx.addWidget(self._chk_disable_thinking)
        row_ctx.addStretch()
        of_lay.addLayout(row_ctx)

        lay.addWidget(self._openai_fields)

        # ── Status label (always present) ──────────────────────────────
        self._lbl_status = QLabel("")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 8pt;")

        # ── Buttons row (optional) ─────────────────────────────────────
        if show_buttons:
            btn_row = QHBoxLayout()
            self._btn_apply = QPushButton("Apply")
            self._btn_apply.setToolTip(
                "Save settings and (re-)initialise the translator.\n"
                "Missing packages are installed automatically."
            )
            self._btn_test = QPushButton("Test")
            self._btn_test.setToolTip(
                "Send a short test string to verify the translator is working."
            )
            btn_row.addWidget(self._btn_apply)
            btn_row.addWidget(self._btn_test)
            btn_row.addWidget(self._lbl_status, 1)
            lay.addLayout(btn_row)

            self._btn_apply.clicked.connect(self._on_apply)
            self._btn_test.clicked.connect(self._on_test)
        else:
            lay.addWidget(self._lbl_status)

        # Wire backend/preset selectors
        self._cmb_backend.currentIndexChanged.connect(self._on_backend_changed)
        self._cmb_preset.currentIndexChanged.connect(self._on_preset_selected)

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
        # Editable combo: strip display suffix like " — 简体中文"
        text = self._cmb_target_lang.currentText().strip()
        code = text.split(" ")[0] if text else ""
        return code or "zh-CN"

    def load_from_config(self) -> None:
        """Populate all fields from :class:`AppConfig`."""
        backend = _cfg.translator_backend
        for i in range(self._cmb_backend.count()):
            if self._cmb_backend.itemData(i) == backend:
                self._cmb_backend.setCurrentIndex(i)
                break

        self._set_target_lang(_cfg.translator_target_lang or "zh-CN")

        self._spn_context_window.setValue(_cfg.openai_context_window)
        self._spn_summary_trigger.setValue(_cfg.openai_summary_trigger)
        self._chk_tools_enabled.setChecked(_cfg.openai_tools_enabled)
        self._chk_disable_thinking.setChecked(_cfg.openai_disable_thinking)

        if backend == "openai":
            self._le_api_key.setText(_cfg.openai_api_key)
            self._le_model.setText(_cfg.openai_model)
            self._le_base_url.setText(_cfg.openai_base_url)
            self._te_system_prompt.setPlainText(
                _cfg.openai_system_prompt or DEFAULT_SYSTEM_PROMPT
            )
        else:
            self._le_api_key.setText(_cfg.cloud_api_key)
            self._te_system_prompt.setPlainText(DEFAULT_SYSTEM_PROMPT)

    def save_to_config(self) -> None:
        """Persist all translator settings to :class:`AppConfig` without building."""
        backend = self._cmb_backend.currentData() or "none"
        api_key = self._le_api_key.text().strip()
        _cfg.translator_backend = backend
        _cfg.translator_target_lang = self.target_lang_value()
        if backend == "cloud":
            _cfg.cloud_api_key = api_key
        elif backend == "openai":
            _cfg.openai_api_key = api_key
            _cfg.openai_model = self._le_model.text().strip() or "gpt-4o-mini"
            _cfg.openai_base_url = self._le_base_url.text().strip()
            _cfg.openai_system_prompt = (
                self._te_system_prompt.toPlainText().strip()
            )
            _cfg.openai_context_window = self._spn_context_window.value()
            _cfg.openai_summary_trigger = self._spn_summary_trigger.value()
            _cfg.openai_tools_enabled = self._chk_tools_enabled.isChecked()
            _cfg.openai_disable_thinking = self._chk_disable_thinking.isChecked()

    def apply(self) -> None:
        """Save settings to config and rebuild the translator via AppBackend."""
        self.save_to_config()
        if _cfg.translator_backend == "none":
            self._backend.set_translator(None)
            self._set_status("Translator disabled.")
            self.translator_applied.emit()
            return
        self._build_from_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._lbl_status.setText(text)

    def _set_target_lang(self, tag: str) -> None:
        """Set the target-lang combo to the given BCP-47 tag."""
        for i in range(self._cmb_target_lang.count()):
            if self._cmb_target_lang.itemData(i) == tag:
                self._cmb_target_lang.setCurrentIndex(i)
                return
        # Not in preset list — type it directly (editable combo)
        self._cmb_target_lang.setCurrentText(tag)

    def _build_from_config(self) -> None:
        """Build translator from saved AppConfig and inject into AppBackend."""
        backend_key = _cfg.translator_backend
        target_lang = _cfg.translator_target_lang or "zh-CN"
        self._set_status("Building…")
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
                    f"✓ {backend_key.title()} ready → {target_lang}"
                )
            else:
                self._set_status("Translator disabled.")
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
            from src.translators.cloud_translation import CloudTranslationTranslator  # noqa: PLC0415
            return CloudTranslationTranslator(api_key=api_key or None, progress=progress)
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
        self._set_status("Building…")
        QApplication.processEvents()
        try:
            translator = self._build_temp_translator()
        except RuntimeError as exc:
            self._set_status(f"⚠ {exc}")
            return
        if translator is None:
            self._set_status("No backend selected.")
            return
        self._set_status("Testing…")
        QApplication.processEvents()
        try:
            result = translator.translate(
                "こんにちは、世界！", target_lang=target_lang
            )
            self._set_status(f"✓  {result!r}")
        except Exception as exc:
            self._set_status(f"⚠ {exc}")

    @Slot(int)
    def _on_backend_changed(self, index: int) -> None:
        backend = self._cmb_backend.itemData(index) or "none"
        info = PROVIDERS_BY_KEY.get(backend)
        self._row_api_key.setVisible(bool(info and info.needs_api_key))
        self._openai_fields.setVisible(backend == "openai")
        if backend == "openai":
            self._le_api_key.setText(_cfg.openai_api_key)
        elif backend == "cloud":
            self._le_api_key.setText(_cfg.cloud_api_key)

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

    @Slot()
    def _on_reset_prompt(self) -> None:
        self._te_system_prompt.setPlainText(DEFAULT_SYSTEM_PROMPT)
