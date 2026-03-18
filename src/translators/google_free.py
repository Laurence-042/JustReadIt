# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Free (no-API-key) Google Translate backend via ``deep-translator``.

Uses the same unofficial endpoint as the Google Translate web page — no
billing account or API key required.  Suitable for testing and light personal
use; not recommended for production throughput.

Requirements::

    pip install deep-translator

Usage::

    from src.translators.google_free import GoogleFreeTranslator

    translator = GoogleFreeTranslator()
    result = translator.translate("おはようございます", target_lang="zh-CN")
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.translators._installer import ensure_package
from src.translators.base import Translator

if TYPE_CHECKING:
    from collections.abc import Callable


class GoogleFreeTranslator(Translator):
    """Translation backend backed by the free (unofficial) Google Translate API.

    Wraps :class:`deep_translator.GoogleTranslator`.  No API key is needed.

    Args:
        progress: Optional status callback used during automatic package
            installation.
    """

    def __init__(
        self,
        *,
        progress: "Callable[[str], None] | None" = None,
    ) -> None:
        ensure_package("deep-translator>=1.11", "deep_translator", progress=progress)
        # Eagerly import to surface InstallationError early.
        import deep_translator as _dt  # type: ignore[import-untyped]
        self._gt_cls = _dt.GoogleTranslator

    # ── Translator ────────────────────────────────────────────────────

    def translate(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "en",
    ) -> str:
        """Translate *text* via the free Google Translate endpoint.

        Args:
            text: Source text.
            source_lang: BCP-47 / ISO 639-1 source language code
                (e.g. ``"ja"``).  Pass ``"auto"`` to auto-detect.
            target_lang: BCP-47 / ISO 639-1 target language code
                (e.g. ``"en"``, ``"zh-CN"``).

        Returns:
            Translated string.

        Raises:
            RuntimeError: If the translation request fails.
        """
        if not text.strip():
            return text

        src = source_lang if source_lang else "auto"
        try:
            result: str = self._gt_cls(source=src, target=target_lang).translate(text)
        except Exception as exc:
            raise RuntimeError(
                f"Google Translate (free) request failed: {exc}"
            ) from exc

        return result or text
