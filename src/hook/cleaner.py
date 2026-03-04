"""Extensible hook-text cleaner rule chain.

Each cleaner implements the :class:`Cleaner` ABC.  The public helper
:func:`run_cleaners` applies them sequentially and returns the final
cleaned string.

Built-in rules (applied in this order by default):

1. :class:`StripControlChars`
   Remove Unicode C0/C1 control characters (U+0000–U+001F, U+007F–U+009F)
   **except** common whitespace (``\\n``, ``\\r``, ``\\t``).

2. :class:`DeduplicateLines`
   Collapse consecutive identical lines into a single occurrence.

3. :class:`TrimWhitespace`
   Strip leading/trailing whitespace from each line, then strip the whole
   string.  Empty lines left after stripping are removed.

Adding a custom cleaner
-----------------------
Subclass :class:`Cleaner`, implement :meth:`clean`, and insert it at the
desired position in ``DEFAULT_CLEANERS``::

    from src.hook.cleaner import Cleaner, DEFAULT_CLEANERS

    class MyRule(Cleaner):
        def clean(self, text: str) -> str:
            return text.replace('foo', 'bar')

    DEFAULT_CLEANERS.insert(0, MyRule())
"""
from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from typing import Sequence


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class Cleaner(ABC):
    """Abstract base for hook-text cleaners."""

    @abstractmethod
    def clean(self, text: str) -> str:
        """Return cleaned *text*.

        Implementations must handle empty strings gracefully (return ``""``).
        """


# ---------------------------------------------------------------------------
# Built-in cleaners
# ---------------------------------------------------------------------------

# Matches Unicode C0 control chars (U+0000–U+001F) and C1 (U+007F–U+009F)
# EXCEPT the common whitespace characters \t (0x09), \n (0x0A), \r (0x0D).
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]",
)


class StripControlChars(Cleaner):
    """Remove Unicode control characters, preserving ``\\n``, ``\\r``, ``\\t``."""

    def clean(self, text: str) -> str:
        if not text:
            return text
        return _CONTROL_RE.sub("", text)


class DeduplicateLines(Cleaner):
    """Collapse consecutive identical lines into one."""

    def clean(self, text: str) -> str:
        if not text:
            return text
        lines = text.splitlines(keepends=True)
        result: list[str] = []
        prev: str | None = None
        for line in lines:
            # Compare fully-stripped content so whitespace differences
            # don't prevent deduplication.
            stripped = line.strip()
            if stripped != prev:
                result.append(line)
            prev = stripped
        return "".join(result)


class TrimWhitespace(Cleaner):
    """Strip per-line leading/trailing whitespace; drop resulting blank lines."""

    def clean(self, text: str) -> str:
        if not text:
            return text
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_cleaners(
    cleaners: Sequence[Cleaner],
    text: str,
) -> str:
    """Apply *cleaners* sequentially and return the final result.

    Parameters
    ----------
    cleaners:
        Ordered sequence of :class:`Cleaner` instances.
    text:
        Raw hook text to clean.

    Returns
    -------
    str
        Cleaned text (may be empty if all content was removed).
    """
    for cleaner in cleaners:
        text = cleaner.clean(text)
    return text


# ---------------------------------------------------------------------------
# Default chain
# ---------------------------------------------------------------------------

DEFAULT_CLEANERS: list[Cleaner] = [
    StripControlChars(),
    DeduplicateLines(),
    TrimWhitespace(),
]
