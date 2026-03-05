"""Hook discovery and cleaner rule chain.

Public API
----------
.. autoclass:: HookCode
.. autoclass:: HookSearcher
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

__all__ = [
    "Cleaner",
    "DEFAULT_CLEANERS",
    "DeduplicateLines",
    "StripControlChars",
    "TrimWhitespace",
    "run_cleaners",
]
