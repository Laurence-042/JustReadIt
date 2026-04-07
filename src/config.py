"""Persistent application configuration.

Backed by a QSettings INI file at::

    %APPDATA%\\JustReadIt\\config.ini

Typed properties replace raw ``QSettings.value()`` calls so key names and
default values are defined in one place.

Usage::

    cfg = AppConfig()
    cfg.ocr_language          # -> str
    cfg.ocr_language = "ja"
    cfg.interval_ms           # -> int
    cfg.interval_ms = 1500
"""
from __future__ import annotations

from PySide6.QtCore import QSettings


def _make_qsettings() -> QSettings:
    return QSettings(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        "JustReadIt",
        "config",
    )


class AppConfig:
    """Thin typed wrapper around ``QSettings``.

    Each property handles its own key name, type coercion, and default value.
    A new ``QSettings`` handle is opened on every access, which is cheap and
    avoids holding a stale handle across long-lived objects.
    """

    # ── OCR ────────────────────────────────────────────────────────────

    @property
    def ocr_language(self) -> str:
        return str(_make_qsettings().value("ocr/language", "ja"))

    @ocr_language.setter
    def ocr_language(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("ocr/language", value)
        s.sync()

    @property
    def ocr_max_size(self) -> int:
        """Maximum long-edge (px) of the image fed to Windows OCR.

        Acts as a soft cap: the effective scale is
        ``min(upscale_factor, ocr_max_size / max_dim)``.
        Small probe crops (< ocr_max_size/2 px) still receive the full
        upscale boost; full-resolution captures are downsampled to this
        limit, which dramatically reduces OCR latency on high-DPI screens.

        Default 1920 — leaves 1080p frames untouched and halves 4K frames.
        """
        return int(_make_qsettings().value("ocr/max_size", 1920))

    @ocr_max_size.setter
    def ocr_max_size(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("ocr/max_size", value)
        s.sync()

    # ── Pipeline ───────────────────────────────────────────────────────

    @property
    def interval_ms(self) -> int:
        return int(_make_qsettings().value("pipeline/interval_ms", 1500))

    @interval_ms.setter
    def interval_ms(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("pipeline/interval_ms", value)
        s.sync()

    # ── Translation backend ────────────────────────────────────────────

    @property
    def translator_backend(self) -> str:
        """Active translation backend: ``"cloud"`` or ``"openai"``."""
        return str(_make_qsettings().value("translator/backend", "cloud"))

    @translator_backend.setter
    def translator_backend(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("translator/backend", value)
        s.sync()

    @property
    def translator_target_lang(self) -> str:
        """BCP-47 target language code (default ``"en"``)."""
        return str(_make_qsettings().value("translator/target_lang", "en"))

    @translator_target_lang.setter
    def translator_target_lang(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("translator/target_lang", value)
        s.sync()

    # ── Cloud Translation API ──────────────────────────────────────────

    @property
    def cloud_api_key(self) -> str:
        """Google Cloud Translation restricted API key (empty = use ADC)."""
        return str(_make_qsettings().value("cloud/api_key", ""))

    @cloud_api_key.setter
    def cloud_api_key(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("cloud/api_key", value)
        s.sync()

    # ── OpenAI ────────────────────────────────────────────────────────

    @property
    def openai_api_key(self) -> str:
        """OpenAI API key (``sk-...")."""
        return str(_make_qsettings().value("openai/api_key", ""))

    @openai_api_key.setter
    def openai_api_key(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("openai/api_key", value)
        s.sync()

    @property
    def openai_model(self) -> str:
        """OpenAI chat model name (default ``"gpt-4o-mini"``)."""
        return str(_make_qsettings().value("openai/model", "gpt-4o-mini"))

    @openai_model.setter
    def openai_model(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("openai/model", value)
        s.sync()

    @property
    def openai_base_url(self) -> str:
        """OpenAI-compatible base URL override (empty = default OpenAI endpoint)."""
        return str(_make_qsettings().value("openai/base_url", ""))

    @openai_base_url.setter
    def openai_base_url(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("openai/base_url", value)
        s.sync()

    @property
    def openai_system_prompt(self) -> str:
        """User-configured system prompt for the OpenAI translator."""
        return str(_make_qsettings().value("openai/system_prompt", ""))

    @openai_system_prompt.setter
    def openai_system_prompt(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("openai/system_prompt", value)
        s.sync()

    @property
    def openai_context_window(self) -> int:
        """Number of recent dialogue pairs included as context (default 10)."""
        return int(_make_qsettings().value("openai/context_window", 10))

    @openai_context_window.setter
    def openai_context_window(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("openai/context_window", value)
        s.sync()

    @property
    def openai_summary_trigger(self) -> int:
        """History length that triggers summarisation of the oldest chunk (default 20)."""
        return int(_make_qsettings().value("openai/summary_trigger", 20))

    @openai_summary_trigger.setter
    def openai_summary_trigger(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("openai/summary_trigger", value)
        s.sync()

    @property
    def openai_tools_enabled(self) -> bool:
        """Whether to expose KB tool-calling functions to the model (default ``True``).

        Disable for small or instruction-tuned models that struggle with
        function-calling prompts.
        """
        v = _make_qsettings().value("openai/tools_enabled", True)
        # QSettings may return str "true"/"false" when read back from INI.
        if isinstance(v, str):
            return v.lower() not in ("false", "0", "no")
        return bool(v)

    @openai_tools_enabled.setter
    def openai_tools_enabled(self, value: bool) -> None:
        s = _make_qsettings()
        s.setValue("openai/tools_enabled", value)
        s.sync()

    @property
    def openai_disable_thinking(self) -> bool:
        """Prepend empty ``<think></think>`` prefill to suppress reasoning.

        Only effective on local endpoints (Ollama / LM Studio) running
        thinking-capable models (DeepSeek-R1-Distill, QwQ, …).
        **Must not be enabled for the standard OpenAI endpoint.**
        """
        v = _make_qsettings().value("openai/disable_thinking", True)
        if isinstance(v, str):
            return v.lower() not in ("false", "0", "no")
        return bool(v)

    @openai_disable_thinking.setter
    def openai_disable_thinking(self, value: bool) -> None:
        s = _make_qsettings()
        s.setValue("openai/disable_thinking", value)
        s.sync()

    # ── Hover overlay ──────────────────────────────────────────────────

    @property
    def dump_vk(self) -> int:
        """Virtual-key code for the debug-dump hotkey (default 0x77 = F8)."""
        return int(_make_qsettings().value("overlay/dump_vk", 0x77))

    @dump_vk.setter
    def dump_vk(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("overlay/dump_vk", value)
        s.sync()

    @property
    def freeze_vk(self) -> int:
        """Virtual-key code for the Freeze hotkey (default 0x78 = F9)."""
        return int(_make_qsettings().value("overlay/freeze_vk", 0x78))

    @freeze_vk.setter
    def freeze_vk(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("overlay/freeze_vk", value)
        s.sync()
