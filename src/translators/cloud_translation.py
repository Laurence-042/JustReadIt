# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Google Cloud Translation API backend (Basic / v2).

Suitable for short, stateless text such as UI labels and menu items.
Billed per character; cheaper than per-token LLM APIs for high-frequency
short strings.

Requirements::

    pip install google-cloud-translate

Authentication (pick one):
  * Set ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable to a
    service-account JSON key path.
  * Pass an explicit ``api_key`` string (restricted API key from Cloud Console).

Usage::

    from src.translators.cloud_translation import CloudTranslationTranslator

    translator = CloudTranslationTranslator(api_key="AIza...")
    result = translator.translate("おはようございます", target_lang="zh-CN")
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.translators._installer import ensure_package
from src.translators.base import AuthError, NetworkError, TranslationError, Translator

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.cloud.translate_v2 import Client as _GCTClient


class CloudTranslationTranslator(Translator):
    """Translation backend backed by Google Cloud Translation API (Basic / v2).

    Args:
        api_key: Restricted API key string.  If *None*, falls back to the
            ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable (ADC).
        timeout: Per-request timeout in seconds (default 10).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        progress: "Callable[[str], None] | None" = None,
    ) -> None:
        ensure_package(
            "google-cloud-translate>=3.0",
            "google.cloud.translate_v2",
            progress=progress,
        )
        try:
            from google.cloud import translate_v2 as _gct  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-translate is not installed.  "
                "Run: pip install google-cloud-translate"
            ) from exc

        if api_key:
            # client_options={"api_key": ...} still triggers google.auth.default()
            # internally.  Use google.auth.api_key.Credentials to bypass ADC entirely.
            from google.auth.api_key import Credentials as _ApiKeyCreds  # type: ignore[import-untyped]
            self._client: _GCTClient = _gct.Client(credentials=_ApiKeyCreds(api_key))
        else:
            self._client = _gct.Client()  # ADC / service account

    # ── Translator ────────────────────────────────────────────────────

    def translate(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "en",
    ) -> str:
        """Translate *text* via Cloud Translation API v2.

        Args:
            text: Source text.
            source_lang: BCP-47 source language code (e.g. ``"ja"``).
                Pass ``"auto"`` or ``""`` to let the API auto-detect.
            target_lang: BCP-47 target language code (e.g. ``"en"``, ``"zh-CN"``).

        Returns:
            Translated string.

        Raises:
            RuntimeError: If the API call fails.
        """
        if not text.strip():
            return text

        source: str | None = source_lang if source_lang not in ("auto", "") else None
        try:
            result = self._client.translate(
                text,
                source_language=source,
                target_language=target_lang,
            )
        except Exception as exc:
            msg = str(exc)
            if "403" in msg or "credentials" in msg.lower() or "API key" in msg:
                raise AuthError(f"Cloud Translation API auth failed: {exc}") from exc
            if "429" in msg or "quota" in msg.lower():
                raise NetworkError(f"Cloud Translation API rate limited: {exc}") from exc
            raise TranslationError(
                f"Cloud Translation API request failed: {exc}"
            ) from exc

        translated: str = result["translatedText"]
        return translated
