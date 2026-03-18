"""Extensible translation-range detector rule chain.

Each detector implements :class:`RangeDetector`.  The public helper
:func:`run_detectors` walks the chain in priority order and returns the first
non-``None`` result.

Built-in rules (priority order)
--------------------------------
1. :class:`ParagraphDetector`
   Uniform line height + tight vertical spacing → whole paragraph.
2. :class:`TableRowDetector`
   2-4 lines aligned on the same horizontal baseline → full table row.
3. :class:`SingleLineDetector`
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
import statistics
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
# Built-in detector 1 – Paragraph
# ---------------------------------------------------------------------------

class ParagraphDetector(RangeDetector):
    """Detect a paragraph: consecutive OCR lines with uniform height and tight
    vertical spacing.

    The input *lines* are OCR-provided line bounding boxes (one box per text
    line).  No word-level re-grouping is performed; that is handled upstream
    by :class:`~src.ocr.windows_ocr.WindowsOcr`.

    Algorithm
    ---------
    1. Sort lines top-to-bottom and find the anchor (the line whose vertical
       extent ± ``cursor_margin`` contains *cursor_y*).
    2. Grow upward and downward from the anchor, accepting each adjacent line
       only when **both** conditions hold:

       a. *Height compatibility* – the candidate line's height is within
          ``height_ratio`` of the **accumulated paragraph median** height.

       b. *Gap uniformity* – the pixel gap to the candidate is ≤
          ``gap_ratio × local_median_h`` (absolute) **and** (once at least
          one gap has been accepted) ≤ ``gap_outlier_scale × median(accepted
          gaps)`` (relative outlier
          gap).  Only applied once at least one gap is already accepted.

    Parameters
    ----------
    height_ratio:
        Max allowed fractional deviation of candidate height vs accumulated
        paragraph median height.  Default 0.5.
    gap_ratio:
        Absolute ceiling: max vertical gap as a multiple of local median line
        height.  Default 1.5.
    gap_outlier_scale:
        Relative outlier threshold.  Set to 0 to disable.  Default 2.5.
    min_lines:
        Minimum number of consecutive matching lines to qualify.  Default 2.
    cursor_margin:
        Extra pixels around each line when testing cursor proximity.  Default 8.
    """

    def __init__(
        self,
        height_ratio: float = 0.5,
        gap_ratio: float = 1.5,
        gap_outlier_scale: float = 2.5,
        min_lines: int = 2,
        cursor_margin: int = 8,
    ) -> None:
        self.height_ratio = height_ratio
        self.gap_ratio = gap_ratio
        self.gap_outlier_scale = gap_outlier_scale
        self.min_lines = min_lines
        self.cursor_margin = cursor_margin

    def detect(
        self,
        lines: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        sorted_lines = sorted(lines, key=lambda b: b.y)
        if len(sorted_lines) < self.min_lines:
            return None

        # Find the anchor line whose vertical extent contains the cursor.
        anchor_idx: int | None = None
        for i, line in enumerate(sorted_lines):
            if line.y - self.cursor_margin <= cursor_y <= line.bottom + self.cursor_margin:
                anchor_idx = i
                break
        if anchor_idx is None:
            return None

        # Accumulated paragraph heights (shared between both grow passes).
        all_heights: list[int] = [sorted_lines[anchor_idx].h]

        def _height_ok(candidate: BoundingBox) -> bool:
            para_med_h = statistics.median(all_heights)
            if para_med_h == 0:
                return False
            return abs(candidate.h - para_med_h) / para_med_h <= self.height_ratio

        def _gap_ok(
            upper: BoundingBox,
            lower: BoundingBox,
            accepted_gaps: list[float],
        ) -> bool:
            gap = lower.y - upper.bottom
            med_h = statistics.median([upper.h, lower.h])
            if gap > self.gap_ratio * med_h:
                return False
            if accepted_gaps and self.gap_outlier_scale > 0:
                ref = statistics.median(accepted_gaps)
                if ref > 0 and gap > self.gap_outlier_scale * ref:
                    return False
            return True

        def _grow(start: int, step: int) -> tuple[int, list[float]]:
            nonlocal all_heights
            idx = start
            gaps: list[float] = []
            while True:
                nxt = idx + step
                if not (0 <= nxt < len(sorted_lines)):
                    break
                candidate = sorted_lines[nxt]
                upper = sorted_lines[min(idx, nxt)]
                lower = sorted_lines[max(idx, nxt)]
                if not _height_ok(candidate):
                    break
                if not _gap_ok(upper, lower, gaps):
                    break
                gaps.append(max(0.0, lower.y - upper.bottom))
                all_heights.append(candidate.h)
                idx = nxt
            return idx, gaps

        para_start, _ = _grow(anchor_idx, -1)
        para_end,   _ = _grow(anchor_idx, +1)

        if para_end - para_start + 1 < self.min_lines:
            return None

        return sorted_lines[para_start : para_end + 1]


# ---------------------------------------------------------------------------
# Built-in detector 2 – Table row
# ---------------------------------------------------------------------------

class TableRowDetector(RangeDetector):
    """Detect a table row: 2-4 boxes on the same horizontal band.

    Conditions
    ----------
    * The cursor must be over one of the candidate boxes.
    * 2-4 boxes share a y-band (centers within ``band_ratio * h`` of each
      other) AND have roughly aligned bottom edges.
    * Boxes are horizontally separated (not merged into one cluster).

    Parameters
    ----------
    band_ratio:
        Vertical band half-width as a multiple of median box height.  Default 0.5.
    bottom_align_ratio:
        Max bottom-edge misalignment as a fraction of median height.  Default 0.3.
    min_cols, max_cols:
        Accepted column count.  Default 2–4.
    min_h_gap_ratio:
        Minimum horizontal gap between adjacent row boxes as a fraction of
        median box width.  Ensures boxes are truly separate columns.  Default 0.3.
    """

    def __init__(
        self,
        band_ratio: float = 0.5,
        bottom_align_ratio: float = 0.3,
        min_cols: int = 2,
        max_cols: int = 4,
        min_h_gap_ratio: float = 0.3,
    ) -> None:
        self.band_ratio = band_ratio
        self.bottom_align_ratio = bottom_align_ratio
        self.min_cols = min_cols
        self.max_cols = max_cols
        self.min_h_gap_ratio = min_h_gap_ratio

    def detect(
        self,
        lines: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        if not lines:
            return None

        # Cursor must be inside a line box.
        anchor = next(
            (b for b in lines if b.contains(cursor_x, cursor_y)), None
        )
        if anchor is None:
            return None

        med_h = statistics.median(b.h for b in lines)
        band_half = self.band_ratio * med_h

        # Candidate lines in the same horizontal band.
        row_candidates = [
            b for b in lines
            if abs(b.center_y - anchor.center_y) <= band_half
        ]
        if not (self.min_cols <= len(row_candidates) <= self.max_cols):
            return None

        # Bottom-edge alignment check.
        med_bottom = statistics.median(b.bottom for b in row_candidates)
        if any(
            abs(b.bottom - med_bottom) > self.bottom_align_ratio * med_h
            for b in row_candidates
        ):
            return None

        # Ensure boxes are genuinely spaced (not one fused cluster).
        sorted_row = sorted(row_candidates, key=lambda b: b.x)
        med_w = statistics.median(b.w for b in sorted_row)
        for a, b in zip(sorted_row, sorted_row[1:]):
            gap = b.x - a.right
            if gap < self.min_h_gap_ratio * med_w:
                return None

        return sorted_row


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
    TableRowDetector(),
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
