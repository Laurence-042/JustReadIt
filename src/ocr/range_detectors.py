"""Extensible translation-range detector rule chain.

Each detector implements :class:`RangeDetector`.  The public helper
:func:`run_detectors` walks the chain in priority order and returns the first
non-``None`` result.

Built-in rules (priority order)
--------------------------------
1. :class:`ParagraphDetector`
   Uniform line width / height + tight vertical spacing → whole paragraph.
2. :class:`TableRowDetector`
   2-4 boxes aligned on the same horizontal baseline → full table row.
3. :class:`SingleBoxDetector`
   Fallback: the single bounding box nearest to the cursor.

Adding a custom detector
------------------------
Subclass :class:`RangeDetector`, implement :meth:`detect`, and insert it at
the desired priority position in ``DEFAULT_DETECTORS``::

    from src.ocr.range_detectors import RangeDetector, DEFAULT_DETECTORS

    class MyDetector(RangeDetector):
        def detect(self, boxes, cursor_x, cursor_y):
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
        boxes: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        """Return the detected range, or ``None`` if this rule does not match.

        Parameters
        ----------
        boxes:
            All bounding boxes returned by Windows OCR for the current frame.
        cursor_x, cursor_y:
            Current cursor position in the same coordinate space as the boxes
            (i.e. relative to the captured game-window image).

        Returns
        -------
        list[BoundingBox] | None
            The boxes that form the detected translation range, or ``None``
            when this detector's conditions are not satisfied.
        """


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------

def _group_into_lines(boxes: Sequence[BoundingBox]) -> list[list[BoundingBox]]:
    """Cluster boxes into text lines by overlapping vertical extents.

    Two boxes are on the same line when their y-ranges overlap by at least
    half the smaller box's height.  Returns lines sorted top-to-bottom, each
    line sorted left-to-right.
    """
    if not boxes:
        return []

    sorted_boxes = sorted(boxes, key=lambda b: b.y)
    lines: list[list[BoundingBox]] = []
    current_line: list[BoundingBox] = [sorted_boxes[0]]

    for box in sorted_boxes[1:]:
        ref = current_line[0]
        overlap_top = max(ref.y, box.y)
        overlap_bot = min(ref.bottom, box.bottom)
        min_h = min(ref.h, box.h) or 1
        if overlap_bot - overlap_top >= 0.5 * min_h:
            current_line.append(box)
        else:
            lines.append(sorted(current_line, key=lambda b: b.x))
            current_line = [box]

    lines.append(sorted(current_line, key=lambda b: b.x))
    return lines


def _line_stats(line: list[BoundingBox]) -> tuple[float, float, float, float]:
    """Return (median_h, min_x, max_right, center_y) for a line."""
    median_h = statistics.median(b.h for b in line) if line else 0
    min_x = min(b.x for b in line)
    max_right = max(b.right for b in line)
    center_y = statistics.median(b.center_y for b in line)
    return median_h, min_x, max_right, center_y


# ---------------------------------------------------------------------------
# Built-in detector 1 – Paragraph
# ---------------------------------------------------------------------------

class ParagraphDetector(RangeDetector):
    """Detect a block of paragraph text by uniform line geometry.

    Conditions
    ----------
    * The cursor must be vertically within (or near) a text line.
    * At least ``min_lines`` text lines are found in the candidate block.
    * Adjacent lines share a consistent box height (within ``height_ratio``).
    * The vertical gap between consecutive lines is smaller than
      ``gap_ratio * median_h`` (tight spacing, typical of dialog boxes).
    * Lines are horizontally overlapping (same column, not side-by-side text).

    Parameters
    ----------
    height_ratio:
        Max allowed height deviation between adjacent lines, as a fraction
        of the reference line's median height.  Default 0.4 (±40 %).
    gap_ratio:
        Max vertical gap between consecutive lines as a multiple of median
        box height.  Default 1.2.
    min_lines:
        Minimum number of consecutive matching lines to qualify.  Default 2.
    cursor_margin:
        Extra pixels around each line when testing cursor proximity.
        Default 4 px.
    """

    def __init__(
        self,
        height_ratio: float = 0.4,
        gap_ratio: float = 1.2,
        min_lines: int = 2,
        cursor_margin: int = 4,
    ) -> None:
        self.height_ratio = height_ratio
        self.gap_ratio = gap_ratio
        self.min_lines = min_lines
        self.cursor_margin = cursor_margin

    def detect(
        self,
        boxes: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        lines = _group_into_lines(boxes)
        if len(lines) < self.min_lines:
            return None

        # Find the line that contains the cursor (vertical proximity).
        anchor_idx: int | None = None
        for i, line in enumerate(lines):
            top = min(b.y for b in line) - self.cursor_margin
            bot = max(b.bottom for b in line) + self.cursor_margin
            if top <= cursor_y <= bot:
                anchor_idx = i
                break
        if anchor_idx is None:
            return None

        def _lines_compatible(
            a: list[BoundingBox], b_line: list[BoundingBox]
        ) -> bool:
            """True when adjacent lines share height + horizontal overlap."""
            h_a, ax0, ax1, _ = _line_stats(a)
            h_b, bx0, bx1, _ = _line_stats(b_line)
            if h_a == 0:
                return False
            if abs(h_b - h_a) / h_a > self.height_ratio:
                return False
            # At least 30 % horizontal overlap.
            overlap = min(ax1, bx1) - max(ax0, bx0)
            min_width = min(ax1 - ax0, bx1 - bx0) or 1
            return overlap / min_width >= 0.3

        def _gap_ok(
            upper: list[BoundingBox], lower: list[BoundingBox]
        ) -> bool:
            gap = min(b.y for b in lower) - max(b.bottom for b in upper)
            med_h = statistics.median(
                [b.h for b in upper] + [b.h for b in lower]
            )
            return gap <= self.gap_ratio * med_h

        # Grow paragraph upward from anchor.
        para_start = anchor_idx
        while para_start > 0:
            upper = lines[para_start - 1]
            lower = lines[para_start]
            if _lines_compatible(upper, lower) and _gap_ok(upper, lower):
                para_start -= 1
            else:
                break

        # Grow paragraph downward from anchor.
        para_end = anchor_idx
        while para_end < len(lines) - 1:
            upper = lines[para_end]
            lower = lines[para_end + 1]
            if _lines_compatible(upper, lower) and _gap_ok(upper, lower):
                para_end += 1
            else:
                break

        n_lines = para_end - para_start + 1
        if n_lines < self.min_lines:
            return None

        result = [
            box
            for line in lines[para_start : para_end + 1]
            for box in line
        ]
        return result if result else None


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
        boxes: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        if not boxes:
            return None

        # Cursor must be inside a box.
        anchor = next(
            (b for b in boxes if b.contains(cursor_x, cursor_y)), None
        )
        if anchor is None:
            return None

        med_h = statistics.median(b.h for b in boxes)
        band_half = self.band_ratio * med_h

        # Candidate boxes in the same horizontal band.
        row_candidates = [
            b for b in boxes
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
# Built-in detector 3 – Single box (default fallback)
# ---------------------------------------------------------------------------

class SingleBoxDetector(RangeDetector):
    """Return the single bounding box nearest to the cursor.

    This is the guaranteed fallback: it never returns ``None`` as long as
    *boxes* is non-empty.

    Parameters
    ----------
    max_distance:
        Maximum distance (pixels) from the cursor to the nearest point on the
        box.  If the closest box is still farther away, returns ``None``.
        Default 80 px.
    """

    def __init__(self, max_distance: float = 80.0) -> None:
        self.max_distance = max_distance

    def detect(
        self,
        boxes: Sequence[BoundingBox],
        cursor_x: int,
        cursor_y: int,
    ) -> list[BoundingBox] | None:
        if not boxes:
            return None
        nearest = min(
            boxes, key=lambda b: b.distance_to_point(cursor_x, cursor_y)
        )
        if nearest.distance_to_point(cursor_x, cursor_y) > self.max_distance:
            return None
        return [nearest]


# ---------------------------------------------------------------------------
# Default detector chain + runner
# ---------------------------------------------------------------------------

DEFAULT_DETECTORS: list[RangeDetector] = [
    ParagraphDetector(),
    TableRowDetector(),
    SingleBoxDetector(),
]
"""Default rule chain, priority high → low.

Rules are evaluated in order; the first non-``None`` result is returned.
Append or insert custom detectors to customise behaviour per game layout.
"""


def merge_boxes_text(boxes: Sequence[BoundingBox]) -> str:
    """Merge bounding-box texts into a single string, grouped by visual line.

    Boxes on the same visual line (determined by vertical overlap) are
    concatenated left-to-right **without** a space separator — appropriate
    for CJK scripts where characters are not space-delimited.  Lines are
    joined with ``\\n``.

    Parameters
    ----------
    boxes:
        The bounding boxes whose ``.text`` to merge (typically the output
        of :func:`run_detectors`).

    Returns
    -------
    str
        The merged text.  Empty string when *boxes* is empty.
    """
    if not boxes:
        return ""
    lines = _group_into_lines(list(boxes))
    return "\n".join("".join(b.text for b in line) for line in lines)


def run_detectors(
    boxes: Sequence[BoundingBox],
    cursor_x: int,
    cursor_y: int,
    detectors: Sequence[RangeDetector] = DEFAULT_DETECTORS,
) -> list[BoundingBox]:
    """Walk *detectors* in order and return the first non-``None`` result.

    Returns an empty list only when *boxes* is empty or all detectors return
    ``None`` (which should not happen with the default chain that ends with
    ``SingleBoxDetector``).

    Parameters
    ----------
    boxes:
        All bounding boxes for the current OCR frame.
    cursor_x, cursor_y:
        Cursor position in the same coordinate space as *boxes*.
    detectors:
        Detector chain to use.  Defaults to :data:`DEFAULT_DETECTORS`.
    """
    for detector in detectors:
        result = detector.detect(boxes, cursor_x, cursor_y)
        if result is not None:
            return result
    return []
