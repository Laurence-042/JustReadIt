# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Free (no-API-key) Google Translate backend via ``deep-translator``.

Uses the same unofficial endpoint as the Google Translate web page \u2014 no
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
from src.translators.base import TranslationError, Translator

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# BCP-47 → deep-translator code
# ---------------------------------------------------------------------------
# deep-translator silently strips region subtags and then rejects bare
# ``"zh"`` ("No support for the provided language").  Chinese tags must
# be kept intact.  A few BCP-47 codes map to legacy Google identifiers.

# Tags whose region subtag must NOT be stripped.
_KEEP_REGION: frozenset[str] = frozenset({"zh-CN", "zh-TW"})

# BCP-47 → legacy Google code (pure downgrades).
_LEGACY: dict[str, str] = {
    "he":  "iw",
    "fil": "tl",
    "nb":  "no",
    "nn":  "no",
}


def _to_deep_translator(bcp47: str) -> str:
    """Convert a BCP-47 tag to the code ``deep-translator`` accepts."""
    if bcp47 in _KEEP_REGION:
        return bcp47
    if bcp47 in _LEGACY:
        return _LEGACY[bcp47]
    # Strip region: "ja-JP" → "ja", "en-US" → "en"
    bare = bcp47.split("-")[0].lower()
    return bare if bare != bcp47.lower() else bcp47


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
        """Translate *text* without an API key.

        Args:
            text: Source text.
            source_lang: BCP-47 / ISO 639-1 source language code
                (e.g. ``"ja"``).  Pass ``"auto"`` or ``""`` to auto-detect.
            target_lang: BCP-47 / ISO 639-1 target language code
                (e.g. ``"en"``, ``"zh-CN"``, ``"zh-TW"``).

        Returns:
            Translated string.

        Raises:
            RuntimeError: If the translation request fails.
        """
        if not text.strip():
            return text

        src = _to_deep_translator(source_lang) if source_lang else "auto"
        tgt = _to_deep_translator(target_lang)
        try:
            result: str = self._gt_cls(source=src, target=tgt).translate(text)
        except Exception as exc:
            raise TranslationError(
                f"Google Translate (free) request failed: {exc}"
            ) from exc

        return result or text
