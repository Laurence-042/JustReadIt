# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Persistent application configuration backed by a JSON file.

File location::

    %APPDATA%\\JustReadIt\\config.json

Structure::

    {
        "ocr": {
            "language": "ja",
            "max_size": 1920
        },
        "pipeline": {
            "interval_ms": 1500,
            "memory_scan_enabled": true
        },
        "translator": {
            "backend": "cloud",
            "target_lang": "en",
            "backends": {
                "google_free": {},
                "cloud":  { "api_key": "" },
                "openai": {
                    "api_key": "",
                    "model": "gpt-4o-mini",
                    "base_url": "",
                    "system_prompt": "",
                    "context_window": 10,
                    "summary_trigger": 20,
                    "tools_enabled": true,
                    "disable_thinking": true
                }
            }
        },
        "overlay": {
            "dump_vk": 119,
            "freeze_vk": 120
        }
    }

``AppConfig`` is a **singleton**.  Settings are accessed through typed
namespace sub-objects::

    cfg = AppConfig()
    cfg.ocr.language                 # -> str
    cfg.ocr.language = "ja"          # persists; emits cfg.ocr.language_changed
    cfg.pipeline.interval_ms         # -> int
    cfg.translator.backend           # -> str
    cfg.translator.backends.openai.model      # -> str
    cfg.translator.backends.cloud.api_key     # -> str
    cfg.overlay.freeze_vk            # -> int

Signals live on the namespace objects::

    cfg.ocr.language_changed.connect(slot)
    cfg.translator.target_lang_changed.connect(slot)
    cfg.translator.backends.openai.model_changed.connect(slot)
"""
from __future__ import annotations

import json
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from src.paths import config_path

# ---------------------------------------------------------------------------
# Default configuration tree
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "ocr": {
        "language": "ja",
        "max_size": 1920,
    },
    "pipeline": {
        "interval_ms": 1500,
        "memory_scan_enabled": True,
    },
    "translator": {
        "backend": "google_free",
        "target_lang": "en",
        "backends": {
            "google_free": {},
            "cloud": {
                "api_key": "",
            },
            "openai": {
                "api_key": "",
                "model": "gpt-4o-mini",
                "base_url": "",
                "system_prompt": "",
                "context_window": 10,
                "summary_trigger": 20,
                "tools_enabled": True,
                "disable_thinking": True,
            },
        },
    },
    "overlay": {
        "dump_vk": 0x77,
        "freeze_vk": 0x78,
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict that is *base* recursively updated by *override*."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load() -> dict:
    """Load config from disk, falling back to defaults on first run or corrupt file."""
    path = config_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return _deep_merge(_DEFAULTS, data)
        except Exception:
            pass  # corrupt file — start fresh
    return _deep_merge(_DEFAULTS, {})


# ---------------------------------------------------------------------------
# Namespace section objects.
# PySide6 requires Signal descriptors to live on QObject subclasses, so each
# section is a lightweight QObject that owns its own change signals.
# ---------------------------------------------------------------------------

class _OcrConfig(QObject):
    """cfg.ocr.*"""

    language_changed = Signal(str)
    max_size_changed = Signal(int)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root

    @property
    def language(self) -> str:
        return str(self._r._get("ocr", "language", default="ja"))

    @language.setter
    def language(self, value: str) -> None:
        if self._r._set("ocr", "language", value=value):
            self.language_changed.emit(value)

    @property
    def max_size(self) -> int:
        """Maximum long-edge (px) for Windows OCR (default 1920).

        Halves 4-K frames to reduce OCR latency; leaves 1080p frames untouched.
        """
        return int(self._r._get("ocr", "max_size", default=1920))

    @max_size.setter
    def max_size(self, value: int) -> None:
        if self._r._set("ocr", "max_size", value=value):
            self.max_size_changed.emit(value)


class _PipelineConfig(QObject):
    """cfg.pipeline.*"""

    interval_ms_changed = Signal(int)
    memory_scan_enabled_changed = Signal(bool)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root

    @property
    def interval_ms(self) -> int:
        return int(self._r._get("pipeline", "interval_ms", default=1500))

    @interval_ms.setter
    def interval_ms(self, value: int) -> None:
        if self._r._set("pipeline", "interval_ms", value=value):
            self.interval_ms_changed.emit(value)

    @property
    def memory_scan_enabled(self) -> bool:
        """Whether to run ReadProcessMemory scanning (default True)."""
        v = self._r._get("pipeline", "memory_scan_enabled", default=True)
        return bool(v) if not isinstance(v, str) else v.lower() not in ("false", "0", "no")

    @memory_scan_enabled.setter
    def memory_scan_enabled(self, value: bool) -> None:  # noqa: FBT001
        if self._r._set("pipeline", "memory_scan_enabled", value=value):
            self.memory_scan_enabled_changed.emit(value)


class _CloudBackendConfig(QObject):
    """cfg.translator.backends.cloud.*"""

    api_key_changed = Signal(str)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root

    @property
    def api_key(self) -> str:
        """Google Cloud Translation API key (empty = use ADC)."""
        return str(self._r._get("translator", "backends", "cloud", "api_key", default=""))

    @api_key.setter
    def api_key(self, value: str) -> None:
        if self._r._set("translator", "backends", "cloud", "api_key", value=value):
            self.api_key_changed.emit(value)


class _OpenAIBackendConfig(QObject):
    """cfg.translator.backends.openai.*"""

    api_key_changed = Signal(str)
    model_changed = Signal(str)
    base_url_changed = Signal(str)
    system_prompt_changed = Signal(str)
    context_window_changed = Signal(int)
    summary_trigger_changed = Signal(int)
    tools_enabled_changed = Signal(bool)
    disable_thinking_changed = Signal(bool)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root

    def _kv(self, *keys: str, default: Any) -> Any:
        return self._r._get("translator", "backends", "openai", *keys, default=default)

    def _sv(self, *keys: str, value: Any) -> bool:
        return self._r._set("translator", "backends", "openai", *keys, value=value)

    @property
    def api_key(self) -> str:
        return str(self._kv("api_key", default=""))

    @api_key.setter
    def api_key(self, value: str) -> None:
        if self._sv("api_key", value=value):
            self.api_key_changed.emit(value)

    @property
    def model(self) -> str:
        return str(self._kv("model", default="gpt-4o-mini"))

    @model.setter
    def model(self, value: str) -> None:
        if self._sv("model", value=value):
            self.model_changed.emit(value)

    @property
    def base_url(self) -> str:
        return str(self._kv("base_url", default=""))

    @base_url.setter
    def base_url(self, value: str) -> None:
        if self._sv("base_url", value=value):
            self.base_url_changed.emit(value)

    @property
    def system_prompt(self) -> str:
        return str(self._kv("system_prompt", default=""))

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        if self._sv("system_prompt", value=value):
            self.system_prompt_changed.emit(value)

    @property
    def context_window(self) -> int:
        return int(self._kv("context_window", default=10))

    @context_window.setter
    def context_window(self, value: int) -> None:
        if self._sv("context_window", value=value):
            self.context_window_changed.emit(value)

    @property
    def summary_trigger(self) -> int:
        return int(self._kv("summary_trigger", default=20))

    @summary_trigger.setter
    def summary_trigger(self, value: int) -> None:
        if self._sv("summary_trigger", value=value):
            self.summary_trigger_changed.emit(value)

    @property
    def tools_enabled(self) -> bool:
        """Whether to expose KB tool-calling to the model (default True)."""
        v = self._kv("tools_enabled", default=True)
        return bool(v) if not isinstance(v, str) else v.lower() not in ("false", "0", "no")

    @tools_enabled.setter
    def tools_enabled(self, value: bool) -> None:  # noqa: FBT001
        if self._sv("tools_enabled", value=value):
            self.tools_enabled_changed.emit(value)

    @property
    def disable_thinking(self) -> bool:
        """Prepend empty <think></think> to suppress reasoning (local models only)."""
        v = self._kv("disable_thinking", default=True)
        return bool(v) if not isinstance(v, str) else v.lower() not in ("false", "0", "no")

    @disable_thinking.setter
    def disable_thinking(self, value: bool) -> None:  # noqa: FBT001
        if self._sv("disable_thinking", value=value):
            self.disable_thinking_changed.emit(value)


class _BackendsConfig:
    """cfg.translator.backends.*  — grouping namespace (not a QObject)."""

    def __init__(self, root: "_AppConfigCore") -> None:
        self.cloud = _CloudBackendConfig(root)
        self.openai = _OpenAIBackendConfig(root)


class _TranslatorConfig(QObject):
    """cfg.translator.*

    Sub-namespaces: ``.backends.cloud``, ``.backends.openai``.
    """

    backend_changed = Signal(str)
    target_lang_changed = Signal(str)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root
        self.backends = _BackendsConfig(root)

    @property
    def backend(self) -> str:
        """Active backend key: ``"cloud"``, ``"openai"``, ``"google_free"``, or ``"none"``."""
        return str(self._r._get("translator", "backend", default="cloud"))

    @backend.setter
    def backend(self, value: str) -> None:
        if self._r._set("translator", "backend", value=value):
            self.backend_changed.emit(value)

    @property
    def target_lang(self) -> str:
        """BCP-47 target language code (default ``"en"``)."""
        return str(self._r._get("translator", "target_lang", default="en"))

    @target_lang.setter
    def target_lang(self, value: str) -> None:
        if self._r._set("translator", "target_lang", value=value):
            self.target_lang_changed.emit(value)

    def backend_config(self, key: str) -> dict:
        """Return a shallow copy of the stored settings dict for backend *key*."""
        node = self._r._get("translator", "backends", key, default={})
        return dict(node) if isinstance(node, dict) else {}

    def set_backend_config(self, key: str, data: dict) -> None:
        """Atomically replace the full settings dict for backend *key*."""
        with self._r._lock:
            backends = (
                self._r._data
                .setdefault("translator", {})
                .setdefault("backends", {})
            )
            if backends.get(key) == data:
                return
            backends[key] = data
            self._r._save()


class _OverlayConfig(QObject):
    """cfg.overlay.*"""

    dump_vk_changed = Signal(int)
    freeze_vk_changed = Signal(int)

    def __init__(self, root: "_AppConfigCore") -> None:
        super().__init__()
        self._r = root

    @property
    def dump_vk(self) -> int:
        """Virtual-key code for the debug-dump hotkey (default 0x77 = F8)."""
        return int(self._r._get("overlay", "dump_vk", default=0x77))

    @dump_vk.setter
    def dump_vk(self, value: int) -> None:
        if self._r._set("overlay", "dump_vk", value=value):
            self.dump_vk_changed.emit(value)

    @property
    def freeze_vk(self) -> int:
        """Virtual-key code for the Freeze hotkey (default 0x78 = F9)."""
        return int(self._r._get("overlay", "freeze_vk", default=0x78))

    @freeze_vk.setter
    def freeze_vk(self, value: int) -> None:
        if self._r._set("overlay", "freeze_vk", value=value):
            self.freeze_vk_changed.emit(value)


# ---------------------------------------------------------------------------
# Core -- owns JSON data and the write lock
# ---------------------------------------------------------------------------

class _AppConfigCore:
    """Non-QObject base that owns the raw data dict and disk I/O."""

    def __init__(self) -> None:
        self._data: dict = _load()
        self._lock = threading.Lock()

    def _get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def _set(self, *keys: str, value: Any) -> bool:
        with self._lock:
            node = self._data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            leaf = keys[-1]
            if node.get(leaf) == value:
                return False
            node[leaf] = value
            self._save()
        return True

    def _save(self) -> None:
        path = config_path()
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

class AppConfig(_AppConfigCore):
    """Structured application configuration singleton.

    Every ``AppConfig()`` call returns the same instance.

    ============================================ ======================================
    Namespace                                    Properties
    ============================================ ======================================
    ``cfg.ocr``                                  ``language``, ``max_size``
    ``cfg.pipeline``                             ``interval_ms``, ``memory_scan_enabled``
    ``cfg.translator``                           ``backend``, ``target_lang``
    ``cfg.translator.backends.cloud``            ``api_key``
    ``cfg.translator.backends.openai``           ``api_key``, ``model``, ``base_url``,
                                                 ``system_prompt``, ``context_window``,
                                                 ``summary_trigger``, ``tools_enabled``,
                                                 ``disable_thinking``
    ``cfg.overlay``                              ``dump_vk``, ``freeze_vk``
    ============================================ ======================================
    """

    _instance: "AppConfig | None" = None

    def __new__(cls) -> "AppConfig":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self.ocr = _OcrConfig(self)
        self.pipeline = _PipelineConfig(self)
        self.translator = _TranslatorConfig(self)
        self.overlay = _OverlayConfig(self)
        self._initialized = True


# -- removed flat properties (replaced by namespace sub-objects above) --
# Old usage:  cfg.ocr_language          → cfg.ocr.language
#             cfg.interval_ms           → cfg.pipeline.interval_ms
#             cfg.translator_backend    → cfg.translator.backend
#             cfg.translator_target_lang → cfg.translator.target_lang
#             cfg.cloud_api_key         → cfg.translator.backends.cloud.api_key
#             cfg.openai_*              → cfg.translator.backends.openai.*
#             cfg.dump_vk / freeze_vk   → cfg.overlay.dump_vk / .freeze_vk


