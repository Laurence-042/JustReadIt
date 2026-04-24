# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Settings panel for the Google Translate (free) backend.

This backend requires no API key and has no per-backend configuration, so
the panel is intentionally empty.  It satisfies the panel contract defined in
:mod:`src.translators._panel_base`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.config import AppConfig
    from src.translators.base import Translator


class Panel(QWidget):
    """Empty settings panel — Google Translate (free) has no per-backend settings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # No fields needed; layout stays empty.

    # ------------------------------------------------------------------
    # Panel contract
    # ------------------------------------------------------------------

    def load_from_config(self, cfg: "AppConfig") -> None:  # noqa: ARG002
        """No-op — this backend has no configurable fields."""

    def save_to_config(self, cfg: "AppConfig") -> None:  # noqa: ARG002
        """No-op — this backend has no configurable fields."""

    def build_translator(
        self,
        cfg: "AppConfig",  # noqa: ARG002
        *,
        progress: "Callable[[str], None] | None" = None,
        knowledge_base: object = None,  # noqa: ARG002
    ) -> "Translator":
        from src.translators.google_free.translator import GoogleFreeTranslator  # noqa: PLC0415
        return GoogleFreeTranslator(progress=progress)

    def connect_dirty(self, slot: "Callable[[], None]") -> None:  # noqa: ARG002
        """No-op — no fields to observe."""
