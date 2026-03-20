# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Free (no-API-key) Google Translate backend.

Uses the internal endpoint (``translate.googleapis.com``) that Google's own
mobile apps use — ``client=gtx``, no billing account or API key required.

Uses only the Python standard library (``urllib``); no third-party packages
are installed.

Usage::

    from src.translators.google_free import GoogleFreeTranslator

    translator = GoogleFreeTranslator()
    result = translator.translate("おはようございます", target_lang="zh-CN")
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from src.translators.base import Translator

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Endpoint constants
# ---------------------------------------------------------------------------

_GTX_URL = "https://translate.googleapis.com/translate_a/single"

# Request headers that mimic a browser to avoid bot-detection 429s.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_TIMEOUT = 10  # seconds


class GoogleFreeTranslator(Translator):
    """Translation backend using the unofficial Google Translate internal API.

    No API key or account needed.

    Args:
        progress: Optional status callback (currently unused, kept for API
            parity with other backends).
    """

    def __init__(
        self,
        *,
        progress: "Callable[[str], None] | None" = None,
    ) -> None:
        pass  # No package installation required.

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

        src = source_lang if source_lang not in ("auto", "") else "auto"
        try:
            return _gtx_translate(text, src, target_lang)
        except Exception as exc:
            raise RuntimeError(
                f"Google Translate (free) request failed: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _gtx_translate(text: str, src: str, tgt: str) -> str:
    """Call the unofficial Google Translate internal endpoint."""
    params = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": src,
            "tl": tgt,
            "dt": "t",
            "q": text,
        }
    )
    url = f"{_GTX_URL}?{params}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Response structure: [ [[translated, original, ...], ...], ..., src_lang ]
    # Concatenate all translated segments from data[0].
    try:
        translated = "".join(
            segment[0] for segment in data[0] if segment[0]
        )
    except (IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected GTX response format: {data!r}") from exc

    if not translated:
        raise RuntimeError("GTX returned an empty translation.")
    return translated
