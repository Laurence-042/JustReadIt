"""Persistent application configuration.

Backed by a QSettings INI file at::

    %APPDATA%\\JustReadIt\\config.ini

Typed properties replace raw ``QSettings.value()`` calls so key names and
default values are defined in one place.  Every setter emits a
``<property>_changed`` signal when the persisted value actually changes,
enabling reactive UI updates without manual push synchronisation.

``AppConfig`` is a **singleton** — every ``AppConfig()`` call returns the
same instance so that signal connections are shared across all modules.

Usage::

    cfg = AppConfig()
    cfg.ocr_language          # -> str
    cfg.ocr_language = "ja"   # emits ocr_language_changed("ja")
    cfg.interval_ms           # -> int
    cfg.interval_ms = 1500    # emits interval_ms_changed(1500)
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal


def _make_qsettings() -> QSettings:
    return QSettings(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        "JustReadIt",
        "config",
    )


class AppConfig(QObject):
    """Reactive typed wrapper around ``QSettings``.

    Singleton — every ``AppConfig()`` call returns the same instance.

    Each settable property emits a ``<name>_changed`` signal when the
    persisted value actually changes.  Views and backends connect to these
    signals to react; no manual push synchronisation required.
    """

    _instance: "AppConfig | None" = None

    # ── Change signals ─────────────────────────────────────────────────
    ocr_language_changed = Signal(str)
    ocr_max_size_changed = Signal(int)
    interval_ms_changed = Signal(int)
    memory_scan_enabled_changed = Signal(bool)
    translator_backend_changed = Signal(str)
    translator_target_lang_changed = Signal(str)
    cloud_api_key_changed = Signal(str)
    openai_api_key_changed = Signal(str)
    openai_model_changed = Signal(str)
    openai_base_url_changed = Signal(str)
    openai_system_prompt_changed = Signal(str)
    openai_context_window_changed = Signal(int)
    openai_summary_trigger_changed = Signal(int)
    openai_tools_enabled_changed = Signal(bool)
    openai_disable_thinking_changed = Signal(bool)
    dump_vk_changed = Signal(int)
    freeze_vk_changed = Signal(int)

    def __new__(cls) -> "AppConfig":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self._initialized = True

    # ── Setter helpers ─────────────────────────────────────────────────

    def _set_str(
        self, key: str, value: str, default: str, sig: Signal
    ) -> None:
        s = _make_qsettings()
        if str(s.value(key, default)) == value:
            return
        s.setValue(key, value)
        s.sync()
        sig.emit(value)

    def _set_int(
        self, key: str, value: int, default: int, sig: Signal
    ) -> None:
        s = _make_qsettings()
        if int(s.value(key, default)) == value:
            return
        s.setValue(key, value)
        s.sync()
        sig.emit(value)

    def _set_bool(
        self, key: str, value: bool, default: bool, sig: Signal  # noqa: FBT001
    ) -> None:
        s = _make_qsettings()
        raw = s.value(key, default)
        if isinstance(raw, str):
            old = raw.lower() not in ("false", "0", "no")
        else:
            old = bool(raw)
        if old == value:
            return
        s.setValue(key, value)
        s.sync()
        sig.emit(value)

    # ── OCR ────────────────────────────────────────────────────────────

    @property
    def ocr_language(self) -> str:
        return str(_make_qsettings().value("ocr/language", "ja"))

    @ocr_language.setter
    def ocr_language(self, value: str) -> None:
        self._set_str("ocr/language", value, "ja", self.ocr_language_changed)

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
        self._set_int("ocr/max_size", value, 1920, self.ocr_max_size_changed)

    # ── Pipeline ───────────────────────────────────────────────────────

    @property
    def interval_ms(self) -> int:
        return int(_make_qsettings().value("pipeline/interval_ms", 1500))

    @interval_ms.setter
    def interval_ms(self, value: int) -> None:
        self._set_int("pipeline/interval_ms", value, 1500, self.interval_ms_changed)

    @property
    def memory_scan_enabled(self) -> bool:
        """Whether to run ReadProcessMemory scanning (default ``True``).

        Disable for games with very large memory footprints where scanning
        causes noticeable stutter; the pipeline falls back to pure OCR text.
        """
        v = _make_qsettings().value("pipeline/memory_scan_enabled", True)
        if isinstance(v, str):
            return v.lower() not in ("false", "0", "no")
        return bool(v)

    @memory_scan_enabled.setter
    def memory_scan_enabled(self, value: bool) -> None:
        self._set_bool(
            "pipeline/memory_scan_enabled", value, True,
            self.memory_scan_enabled_changed,
        )

    # ── Translation backend ────────────────────────────────────────────

    @property
    def translator_backend(self) -> str:
        """Active translation backend: ``"cloud"`` or ``"openai"``."""
        return str(_make_qsettings().value("translator/backend", "cloud"))

    @translator_backend.setter
    def translator_backend(self, value: str) -> None:
        self._set_str(
            "translator/backend", value, "cloud",
            self.translator_backend_changed,
        )

    @property
    def translator_target_lang(self) -> str:
        """BCP-47 target language code (default ``"en"``)."""
        return str(_make_qsettings().value("translator/target_lang", "en"))

    @translator_target_lang.setter
    def translator_target_lang(self, value: str) -> None:
        self._set_str(
            "translator/target_lang", value, "en",
            self.translator_target_lang_changed,
        )

    # ── Cloud Translation API ──────────────────────────────────────────

    @property
    def cloud_api_key(self) -> str:
        """Google Cloud Translation restricted API key (empty = use ADC)."""
        return str(_make_qsettings().value("cloud/api_key", ""))

    @cloud_api_key.setter
    def cloud_api_key(self, value: str) -> None:
        self._set_str("cloud/api_key", value, "", self.cloud_api_key_changed)

    # ── OpenAI ────────────────────────────────────────────────────────

    @property
    def openai_api_key(self) -> str:
        """OpenAI API key (``sk-...")."""
        return str(_make_qsettings().value("openai/api_key", ""))

    @openai_api_key.setter
    def openai_api_key(self, value: str) -> None:
        self._set_str("openai/api_key", value, "", self.openai_api_key_changed)

    @property
    def openai_model(self) -> str:
        """OpenAI chat model name (default ``"gpt-4o-mini"``)."""
        return str(_make_qsettings().value("openai/model", "gpt-4o-mini"))

    @openai_model.setter
    def openai_model(self, value: str) -> None:
        self._set_str(
            "openai/model", value, "gpt-4o-mini", self.openai_model_changed,
        )

    @property
    def openai_base_url(self) -> str:
        """OpenAI-compatible base URL override (empty = default OpenAI endpoint)."""
        return str(_make_qsettings().value("openai/base_url", ""))

    @openai_base_url.setter
    def openai_base_url(self, value: str) -> None:
        self._set_str("openai/base_url", value, "", self.openai_base_url_changed)

    @property
    def openai_system_prompt(self) -> str:
        """User-configured system prompt for the OpenAI translator."""
        return str(_make_qsettings().value("openai/system_prompt", ""))

    @openai_system_prompt.setter
    def openai_system_prompt(self, value: str) -> None:
        self._set_str(
            "openai/system_prompt", value, "",
            self.openai_system_prompt_changed,
        )

    @property
    def openai_context_window(self) -> int:
        """Number of recent dialogue pairs included as context (default 10)."""
        return int(_make_qsettings().value("openai/context_window", 10))

    @openai_context_window.setter
    def openai_context_window(self, value: int) -> None:
        self._set_int(
            "openai/context_window", value, 10,
            self.openai_context_window_changed,
        )

    @property
    def openai_summary_trigger(self) -> int:
        """History length that triggers summarisation of the oldest chunk (default 20)."""
        return int(_make_qsettings().value("openai/summary_trigger", 20))

    @openai_summary_trigger.setter
    def openai_summary_trigger(self, value: int) -> None:
        self._set_int(
            "openai/summary_trigger", value, 20,
            self.openai_summary_trigger_changed,
        )

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
        self._set_bool(
            "openai/tools_enabled", value, True,
            self.openai_tools_enabled_changed,
        )

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
        self._set_bool(
            "openai/disable_thinking", value, True,
            self.openai_disable_thinking_changed,
        )

    # ── Hover overlay ──────────────────────────────────────────────────

    @property
    def dump_vk(self) -> int:
        """Virtual-key code for the debug-dump hotkey (default 0x77 = F8)."""
        return int(_make_qsettings().value("overlay/dump_vk", 0x77))

    @dump_vk.setter
    def dump_vk(self, value: int) -> None:
        self._set_int("overlay/dump_vk", value, 0x77, self.dump_vk_changed)

    @property
    def freeze_vk(self) -> int:
        """Virtual-key code for the Freeze hotkey (default 0x78 = F9)."""
        return int(_make_qsettings().value("overlay/freeze_vk", 0x78))

    @freeze_vk.setter
    def freeze_vk(self, value: int) -> None:
        self._set_int("overlay/freeze_vk", value, 0x78, self.freeze_vk_changed)
