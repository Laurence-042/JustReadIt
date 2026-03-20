# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Translator plugin interface, error hierarchy, and provider registry.

Design
------
* **Error hierarchy** — catch ``TranslationError`` to handle all backends
  uniformly; use subtypes for specific recovery strategies.
* **``PROVIDERS``** — ordered registry of every backend, used by both the
  factory and the UI to stay in sync without hard-coding lists in two places.
* **Language codes** — callers must supply valid BCP-47 tags (e.g.
  ``"zh-CN"``, ``"ja"``, ``"en"``).  Adapters are responsible for converting
  standard BCP-47 to whatever their underlying library requires; no conversion
  happens at this layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class TranslationError(RuntimeError):
    """Base class for all translation backend errors."""


class AuthError(TranslationError):
    """API key missing, invalid, or not authorised."""


class RateLimitError(TranslationError):
    """Request rejected due to quota or rate limiting."""


class NetworkError(TranslationError):
    """Network or HTTP-level failure communicating with the backend."""


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderInfo:
    """Metadata for a translation backend, used by the factory and the UI."""

    key: str
    """Config key stored in ``AppConfig.translator_backend`` (e.g. ``"google_free"``)."""

    display_name: str
    """Human-readable label for dropdowns (e.g. ``"Google Translate (free)"``). """

    needs_api_key: bool = False
    """Whether the backend requires an API key from the user."""

    pip_extras: str = ""
    """``pyproject.toml`` optional-dependency group that installs this provider's deps."""


#: Ordered list of available providers.  Defines the UI dropdown order.
PROVIDERS: list[ProviderInfo] = [
    ProviderInfo(
        key="google_free",
        display_name="Google Translate (free, no key)",
        needs_api_key=False,
        pip_extras="translators-free",
    ),
    ProviderInfo(
        key="cloud",
        display_name="Google Cloud Translation (API key)",
        needs_api_key=True,
        pip_extras="translators-cloud",
    ),
    ProviderInfo(
        key="openai",
        display_name="OpenAI",
        needs_api_key=True,
        pip_extras="translators-openai",
    ),
]

#: Fast lookup by ``key``.
PROVIDERS_BY_KEY: dict[str, ProviderInfo] = {p.key: p for p in PROVIDERS}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Translator(ABC):
    """ABC for translation backends.

    All implementations live in this package.  Adapters must:

    * Accept standard BCP-47 tags for *source_lang* and *target_lang*
      (e.g. ``"zh-CN"``, ``"ja"``, ``"en"``).
    * Convert BCP-47 to the underlying library’s own codes internally if
      needed — that mapping belongs in the adapter, not at the call site.
    * Raise :class:`TranslationError` (or a subtype) on failure.
    """

    @abstractmethod
    def translate(self, text: str, source_lang: str = "ja", target_lang: str = "en") -> str:
        """Translate *text* and return the translated string."""
