"""Frida text hook and cleaner rule chain.

Public API
----------
.. autoclass:: TextHook
.. autoclass:: Cleaner
.. autofunction:: run_cleaners
"""
from __future__ import annotations

from .cleaner import (
    Cleaner,
    DEFAULT_CLEANERS,
    DeduplicateLines,
    StripControlChars,
    TrimWhitespace,
    run_cleaners,
)
from .text_hook import (
    HookAttachError,
    HookScriptError,
    TextHook,
)

__all__ = [
    "Cleaner",
    "DEFAULT_CLEANERS",
    "DeduplicateLines",
    "HookAttachError",
    "HookScriptError",
    "StripControlChars",
    "TextHook",
    "TrimWhitespace",
    "run_cleaners",
]
