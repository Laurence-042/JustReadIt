"""Translator plugin interface."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Translator(ABC):
    """ABC for translation backends.

    Implementations live in this package, e.g.:
      - src/translators/cloud_translation.py  (Google Cloud Translation API)
      - src/translators/openai_translator.py  (OpenAI with rolling summary agent)
    """

    @abstractmethod
    def translate(self, text: str, source_lang: str = "ja", target_lang: str = "en") -> str:
        """Translate *text* and return the translated string."""
