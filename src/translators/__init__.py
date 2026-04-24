# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Translation plugin package.

Available backends
------------------
* :class:`~src.translators.cloud.GoogleCloudTranslator`
  — Google Cloud Translation API v2 (short text, low cost).
* :class:`~src.translators.openai.OpenAICompatTranslator`
  — Any OpenAI-compatible Chat Completions endpoint (OpenAI, OpenRouter,
  Ollama, Azure OpenAI, …) with rolling summary context agent.

All backends implement the :class:`~src.translators.base.Translator` ABC::

    translator.translate(text, source_lang="ja", target_lang="en")
"""
from __future__ import annotations

from src.translators.base import Translator
from src.translators.factory import build_translator
from src.translators.cloud import GoogleCloudTranslator
from src.translators.google_free import GoogleFreeTranslator
from src.translators.openai import OpenAICompatTranslator, OpenAITranslator

__all__ = [
    "Translator",
    "GoogleCloudTranslator",
    "GoogleFreeTranslator",
    "OpenAICompatTranslator",
    "OpenAITranslator",  # backward-compat alias
    "build_translator",
]
