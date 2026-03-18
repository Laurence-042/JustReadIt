"""Extensible translation-range detector rule chain.

Each detector implements :class:`RangeDetector`.  The public helper
:func:`run_detectors` walks the chain in priority order and returns the first
non-``None`` result.

Built-in rules (priority order)
--------------------------------
1. :class:`ParagraphDetector`
   Character-size proximity BFS → paragraph text and table rows alike.
   ``TableRowDetector`` is a backwards-compatible alias.
2. :class:`SingleLineDetector`
   Fallback: the single OCR line nearest to the cursor.
   ``SingleBoxDetector`` is a backwards-compatible alias.

All detectors receive *line* bounding boxes (one :class:`BoundingBox` per
OCR-recognised text line) rather than individual word boxes.  Word-to-line
grouping is performed upstream by :class:`~src.ocr.windows_ocr.WindowsOcr`.

Adding a custom detector
------------------------
Subclass :class:`RangeDetector`, implement :meth:`detect`, and insert it at
the desired priority position in ``DEFAULT_DETECTORS``::

    from src.ocr.range_detectors import RangeDetector, DEFAULT_DETECTORS

    class MyDetector(RangeDetector):
        def detect(self, lines, cursor_x, cursor_y):
            ...

    DEFAULT_DETECTORS.insert(0, MyDetector())
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

@dataclass
class BoundingBox:
    """Axis-aligned bounding box returned by Windows OCR.

    ``x``, ``y`` are the top-left corner in the pixel space of the captured
    image (origin = top-left of the game window capture).
    """

    x: int
    y: int
    w: int
    h: int
    text: str = ""

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2

    def contains(self, px: int | float, py: int | float) -> bool:
        """Return True if the point (px, py) is inside (or on the edge of) this box."""
        return self.x <= px <= self.right and self.y <= py <= self.bottom

    def distance_to_point(self, px: float, py: float) -> float:
        """Euclidean distance from *point* to the nearest edge (0 if inside)."""
        dx = max(self.x - px, 0.0, px - self.right)
        dy = max(self.y - py, 0.0, py - self.bottom)
        return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class RangeDetector(ABC):
    """Abstract base for translation-range detectors."""

    @abstractmethod
    def detect(
        self,
        lines: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        """Return the detected range, or ``None`` if this rule does not match.

        Parameters
        ----------
        lines:
            OCR-provided line bounding boxes for the current frame (one box
            per text line, as returned by :meth:`~src.ocr.windows_ocr.WindowsOcr.recognise`).
        cursor_x, cursor_y:
            Current cursor position in the same coordinate space as *lines*
            (i.e. relative to the captured game-window image).

        Returns
        -------
        list[BoundingBox] | None
            The line boxes that form the detected translation range, or
            ``None`` when this detector's conditions are not satisfied.
        """


# ---------------------------------------------------------------------------
# Character-size estimator
# ---------------------------------------------------------------------------

def _char_size(line: BoundingBox) -> tuple[float, float]:
    """Estimate ``(char_height, char_width)`` from a line bounding box.

    *char_height* is the line's pixel height, which matches the font cap
    height for CJK glyphs.  *char_width* is derived from the line width
    divided by the character count found in ``.text``; when the text is
    empty (e.g. a box with no recognised text) the line height is used as
    a fallback (CJK characters are approximately square).
    """
    char_h = float(line.h)
    n = len(line.text.replace(" ", ""))
    char_w = line.w / n if n > 0 else char_h
    return char_h, char_w


# ---------------------------------------------------------------------------
# Built-in detector 1 – Paragraph / table row (unified)
# ---------------------------------------------------------------------------

class ParagraphDetector(RangeDetector):
    """Detect a group of OCR lines that belong together using character-size
    proximity.

    This single detector handles both vertically-stacked paragraph text and
    horizontally-arranged table rows because the same two-axis proximity
    test naturally covers both layouts.

    Algorithm
    ---------
    1. **Anchor**: find the OCR line whose bounding box contains the cursor
       (expanded by ``cursor_margin`` pixels on each side).  If no line
       contains the cursor, return ``None`` and let :class:`SingleLineDetector`
       handle the fall-through.
    2. **Character-size reference**: estimate ``(char_h, char_w)`` from the
       anchor line via :func:`_char_size`.
    3. **BFS flood-fill**: starting from the anchor, iteratively examine
       every not-yet-grouped line against the *current frontier*.  A
       candidate is added to the group when **all three** conditions hold:

       a. *Font-size compatibility* – the candidate’s char height deviates
          from the anchor’s by at most ``size_ratio`` (default ±40 %).

       b. *Vertical proximity* – the pixel gap between the candidate and the
          current frontier box is ≤ ``max_v_gap_chars × char_h``
          (default 1 char height).

       c. *Horizontal proximity* – the pixel gap is ≤
          ``max_h_gap_chars × char_w`` (default 4 char widths).

       Both gap tests use the *minimum* positive distance between the two
       box edges (0 when the boxes overlap on that axis), so overlapping
       boxes always pass.

    The flood-fill propagates through intermediate lines, so a 5-line
    paragraph is fully captured even though lines 1 and 5 are not directly
    adjacent.

    Parameters
    ----------
    max_v_gap_chars:
        Maximum vertical gap in units of anchor char height.  Default 1.0.
    max_h_gap_chars:
        Maximum horizontal gap in units of anchor char width.  Default 4.0.
    size_ratio:
        Maximum relative deviation of a candidate’s char height from the
        anchor’s.  Default 0.4 (±40 %).
    cursor_margin:
        Extra pixels added around each line when testing cursor containment.
        Default 8 px.
    """

    def __init__(
        self,
        max_v_gap_chars: float = 0.5,
        max_h_gap_chars: float = 4.0,
        size_ratio: float = 0.4,
        cursor_margin: int = 8,
    ) -> None:
        self.max_v_gap_chars = max_v_gap_chars
        self.max_h_gap_chars = max_h_gap_chars
        self.size_ratio = size_ratio
        self.cursor_margin = cursor_margin

    def detect(
        self,
        lines: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        if not lines:
            return None

        # 1. Find anchor: OCR line whose box (expanded by cursor_margin) contains
        #    the cursor.  If the cursor is not on any line, yield to SingleLineDetector.
        m = self.cursor_margin
        anchor: BoundingBox | None = None
        for line in lines:
            if (line.x - m <= cursor_x <= line.right  + m
                    and line.y - m <= cursor_y <= line.bottom + m):
                anchor = line
                break
        if anchor is None:
            return None

        # 2. Derive per-character dimensions from the anchor line.
        anchor_char_h, anchor_char_w = _char_size(anchor)
        max_v = self.max_v_gap_chars * anchor_char_h
        max_h = self.max_h_gap_chars * anchor_char_w

        # 3. BFS flood-fill.  Track membership by object identity (BoundingBox
        #    is a plain @dataclass so it is not hashable; id() is safe here).
        seen: set[int] = {id(anchor)}
        group: list[BoundingBox] = [anchor]
        queue: list[BoundingBox] = [anchor]

        while queue:
            current = queue.pop(0)
            for candidate in lines:
                if id(candidate) in seen:
                    continue

                # Font-size gate: compare candidate height against anchor's.
                cand_h, _ = _char_size(candidate)
                if anchor_char_h > 0:
                    if abs(cand_h - anchor_char_h) / anchor_char_h > self.size_ratio:
                        continue

                # Proximity gate: both axes must pass.
                v_gap = max(0.0, candidate.y - current.bottom,
                            current.y - candidate.bottom)
                h_gap = max(0.0, candidate.x - current.right,
                            current.x - candidate.right)
                if v_gap <= max_v and h_gap <= max_h:
                    seen.add(id(candidate))
                    group.append(candidate)
                    queue.append(candidate)

        return sorted(group, key=lambda b: b.y)


# ``TableRowDetector`` is now a backwards-compatible alias: the BFS proximity
# algorithm in ``ParagraphDetector`` naturally handles both paragraph-style
# (vertically stacked) and table-row-style (horizontally arranged) layouts.
TableRowDetector = ParagraphDetector


# ---------------------------------------------------------------------------
# Built-in detector 3 – Single line (default fallback)
# ---------------------------------------------------------------------------

class SingleLineDetector(RangeDetector):
    """Return the single OCR line nearest to the cursor.

    This is the guaranteed fallback.  It finds the line whose bounding box
    is closest to the cursor and returns it as a single-element list.  When
    the nearest line is further than ``max_distance`` pixels, ``None`` is
    returned.

    Parameters
    ----------
    max_distance:
        Maximum distance (pixels) from the cursor to the nearest point on
        the nearest line box.  Returns ``None`` when exceeded.  Default 80 px.
    """

    def __init__(self, max_distance: float = 80.0) -> None:
        self.max_distance = max_distance

    def detect(
        self,
        lines: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        if not lines:
            return None
        nearest = min(lines, key=lambda b: b.distance_to_point(cursor_x, cursor_y))
        if nearest.distance_to_point(cursor_x, cursor_y) > self.max_distance:
            return None
        return [nearest]


SingleBoxDetector = SingleLineDetector  # backwards-compatible alias


# ---------------------------------------------------------------------------
# Default detector chain + runner
# ---------------------------------------------------------------------------

DEFAULT_DETECTORS: list[RangeDetector] = [
    ParagraphDetector(),
    SingleLineDetector(),
]
"""Default rule chain, priority high → low.

Rules are evaluated in order; the first non-``None`` result is returned.
Append or insert custom detectors to customise behaviour per game layout.
"""


def merge_boxes_text(lines: Sequence[BoundingBox]) -> str:
    """Merge OCR line texts into a single string.

    Lines are sorted top-to-bottom and joined with ``\\n``.

    Parameters
    ----------
    lines:
        The line bounding boxes whose ``.text`` to merge (typically the
        output of :func:`run_detectors`).

    Returns
    -------
    str
        The merged text.  Empty string when *lines* is empty.
    """
    if not lines:
        return ""
    return "\n".join(b.text for b in sorted(lines, key=lambda b: b.y))


def run_detectors(
    lines: Sequence[BoundingBox],
    cursor_x: int,
    cursor_y: int,
    detectors: Sequence[RangeDetector] = DEFAULT_DETECTORS,
) -> tuple[list[BoundingBox], str]:
    """Walk *detectors* in order and return the first non-``None`` result.

    Returns
    -------
    tuple[list[BoundingBox], str]
        The detected line boxes and the class name of the detector that
        matched.  Returns ``([], "")`` when *lines* is empty or all
        detectors return ``None`` (which should not happen with the default
        chain that ends with :class:`SingleLineDetector`).

    Parameters
    ----------
    lines:
        OCR-provided line bounding boxes for the current frame.
    cursor_x, cursor_y:
        Cursor position in the same coordinate space as *lines*.
    detectors:
        Detector chain to use.  Defaults to :data:`DEFAULT_DETECTORS`.
    """
    for detector in detectors:
        result = detector.detect(lines, cursor_x, cursor_y)
        if result is not None:
            return result, type(detector).__name__
    return [], ""
