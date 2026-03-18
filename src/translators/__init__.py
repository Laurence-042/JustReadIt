# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Translation plugin package.

Available backends
------------------
* :class:`~src.translators.cloud_translation.CloudTranslationTranslator`
  — Google Cloud Translation API v2 (short text, low cost).
* :class:`~src.translators.openai_translator.OpenAITranslator`
  — OpenAI Chat Completions with rolling summary context agent.

All backends implement the :class:`~src.translators.base.Translator` ABC::

    translator.translate(text, source_lang="ja", target_lang="en")
"""
from __future__ import annotations

from src.translators.base import Translator
from src.translators.cloud_translation import CloudTranslationTranslator
from src.translators.factory import build_translator
from src.translators.openai_translator import OpenAITranslator

__all__ = [
    "Translator",
    "CloudTranslationTranslator",
    "OpenAITranslator",
    "build_translator",
]
