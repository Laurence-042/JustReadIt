# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OpenAI-compatible API backend module."""
from __future__ import annotations

from .translator import OpenAICompatTranslator
from .translator import OpenAITranslator
from .translator import build_from_config
from .translator import DEFAULT_SYSTEM_PROMPT
from .translator import OPENAI_PRESETS

__all__ = [
    "OpenAICompatTranslator",
    "OpenAITranslator",
    "build_from_config",
    "DEFAULT_SYSTEM_PROMPT",
    "OPENAI_PRESETS",
]
