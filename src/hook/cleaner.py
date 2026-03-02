"""Extensible hook-text cleaner rule chain.

Each cleaner implements the Cleaner ABC.
Built-in rules:
  - StripControlChars  – remove control characters
  - DeduplicateLines   – remove consecutive duplicate lines
  - TrimWhitespace     – strip leading/trailing whitespace per line
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Cleaner(ABC):
    """ABC for hook-text cleaners."""

    @abstractmethod
    def clean(self, text: str) -> str:
        """Return cleaned text."""


# TODO: implement StripControlChars, DeduplicateLines, TrimWhitespace


DEFAULT_CLEANERS: list[Cleaner] = []
