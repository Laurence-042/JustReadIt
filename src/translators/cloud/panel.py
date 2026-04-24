# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Settings panel for the Google Cloud Translation backend.

Provides an API-key input field and delegates config persistence and
translator construction.  Satisfies the panel contract defined in
:mod:`src.translators._panel_base`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.config import AppConfig
    from src.translators.base import Translator


class Panel(QWidget):
    """Settings panel for the Google Cloud Translation (v2) backend.

    Exposes a single API-key field; falls back to Application Default
    Credentials when the field is left empty.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel("API 密钥:"))
        self._le_api_key = QLineEdit()
        self._le_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._le_api_key.setPlaceholderText(
            "在此粘贴 API 密钥（留空使用 Application Default Credentials）"
        )
        row.addWidget(self._le_api_key)
        lay.addLayout(row)

    # ------------------------------------------------------------------
    # Panel contract
    # ------------------------------------------------------------------

    def load_from_config(self, cfg: "AppConfig") -> None:
        self._le_api_key.setText(cfg.translator.backends.cloud.api_key)

    def save_to_config(self, cfg: "AppConfig") -> None:
        cfg.translator.backends.cloud.api_key = self._le_api_key.text().strip()

    def build_translator(
        self,
        cfg: "AppConfig",  # noqa: ARG002
        *,
        progress: "Callable[[str], None] | None" = None,
        knowledge_base: object = None,  # noqa: ARG002
    ) -> "Translator":
        from src.translators.cloud.translator import GoogleCloudTranslator  # noqa: PLC0415
        api_key = self._le_api_key.text().strip() or None
        return GoogleCloudTranslator(api_key=api_key, progress=progress)

    def connect_dirty(self, slot: "Callable[[], None]") -> None:
        self._le_api_key.textChanged.connect(slot)
