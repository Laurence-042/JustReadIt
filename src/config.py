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

    # ── Pipeline ───────────────────────────────────────────────────────

    @property
    def interval_ms(self) -> int:
        return int(_make_qsettings().value("pipeline/interval_ms", 1500))

    @interval_ms.setter
    def interval_ms(self, value: int) -> None:
        s = _make_qsettings()
        s.setValue("pipeline/interval_ms", value)
        s.sync()

    # ── Hook ────────────────────────────────────────────────

    @property
    def hook_code(self) -> str:
        """Serialised :class:`~src.hook.hook_search.HookCode` string, or empty.

        Stored as ``"<module>!<rva_hex>:<access_pattern>:<encoding>"``.
        See :meth:`~src.hook.hook_search.HookCode.to_str` for pattern syntax.
        Empty string means no engine-specific hook has been configured.
        """
        return str(_make_qsettings().value("hook/code", ""))

    @hook_code.setter
    def hook_code(self, value: str) -> None:
        s = _make_qsettings()
        s.setValue("hook/code", value)
        s.sync()


