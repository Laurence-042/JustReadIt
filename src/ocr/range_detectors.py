"""Extensible range-detector rule chain.

Each detector implements the RangeDetector ABC.
Built-in rules (priority order):
  1. ParagraphDetector   – uniform line width/height, tight spacing
  2. TableRowDetector    – aligned baselines, 2-3 boxes on same horizontal band
  3. SingleBoxDetector   – fallback: nearest single bounding box
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class BoundingBox:
    """Axis-aligned bounding box returned by Windows OCR."""

    def __init__(self, x: int, y: int, w: int, h: int, text: str = "") -> None:
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.text = text

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


class RangeDetector(ABC):
    """ABC for translation-range detectors."""

    @abstractmethod
    def detect(
        self,
        boxes: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        """Return the detected range boxes, or None if this rule doesn't match."""


# TODO: implement ParagraphDetector, TableRowDetector, SingleBoxDetector


DEFAULT_DETECTORS: list[RangeDetector] = []
