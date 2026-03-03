"""Tests for src/ocr/ – range detectors and Windows OCR.

Range detector tests are pure unit tests (no hardware needed).
Windows OCR tests require hardware and are marked accordingly.
"""
from __future__ import annotations

import math

import pytest
from PIL import Image

from src.ocr.range_detectors import (
    BoundingBox,
    DEFAULT_DETECTORS,
    ParagraphDetector,
    RangeDetector,
    SingleBoxDetector,
    TableRowDetector,
    _group_into_lines,
    run_detectors,
)


# ==========================================================================
# BoundingBox
# ==========================================================================

class TestBoundingBox:
    def test_right(self):
        b = BoundingBox(10, 20, 30, 40)
        assert b.right == 40

    def test_bottom(self):
        b = BoundingBox(10, 20, 30, 40)
        assert b.bottom == 60

    def test_center(self):
        b = BoundingBox(10, 20, 30, 40)
        assert b.center_x == pytest.approx(25.0)
        assert b.center_y == pytest.approx(40.0)

    def test_contains_inside(self):
        b = BoundingBox(0, 0, 100, 50)
        assert b.contains(50, 25)

    def test_contains_edge(self):
        b = BoundingBox(0, 0, 100, 50)
        assert b.contains(0, 0)
        assert b.contains(100, 50)

    def test_contains_outside(self):
        b = BoundingBox(0, 0, 100, 50)
        assert not b.contains(101, 25)
        assert not b.contains(50, 51)

    def test_distance_inside_is_zero(self):
        b = BoundingBox(0, 0, 100, 100)
        assert b.distance_to_point(50, 50) == pytest.approx(0.0)

    def test_distance_outside_right(self):
        b = BoundingBox(0, 0, 100, 100)
        assert b.distance_to_point(110, 50) == pytest.approx(10.0)

    def test_distance_outside_corner(self):
        b = BoundingBox(0, 0, 100, 100)
        assert b.distance_to_point(103, 104) == pytest.approx(5.0)


# ==========================================================================
# _group_into_lines
# ==========================================================================

class TestGroupIntoLines:
    def test_empty(self):
        assert _group_into_lines([]) == []

    def test_single_box(self):
        boxes = [BoundingBox(0, 0, 50, 20)]
        lines = _group_into_lines(boxes)
        assert len(lines) == 1
        assert lines[0] == boxes

    def test_two_separate_lines(self):
        # Line 1: y=0-20; Line 2: y=30-50 — no overlap
        a = BoundingBox(0, 0, 50, 20)
        b = BoundingBox(0, 30, 50, 20)
        lines = _group_into_lines([a, b])
        assert len(lines) == 2

    def test_same_line_overlap(self):
        # Both at y=5-25 and y=8-28 — overlap > 50 % of smaller height
        a = BoundingBox(0, 5, 50, 20)
        b = BoundingBox(60, 8, 50, 20)
        lines = _group_into_lines([a, b])
        assert len(lines) == 1
        assert len(lines[0]) == 2

    def test_line_sorted_left_to_right(self):
        a = BoundingBox(100, 0, 50, 20)
        b = BoundingBox(0, 2, 50, 20)
        lines = _group_into_lines([a, b])
        assert lines[0][0].x == 0
        assert lines[0][1].x == 100

    def test_three_lines(self):
        boxes = [
            BoundingBox(0, 0, 100, 20),
            BoundingBox(100, 2, 100, 20),   # same line as above
            BoundingBox(0, 35, 100, 20),    # new line
            BoundingBox(0, 70, 100, 20),    # new line
        ]
        lines = _group_into_lines(boxes)
        assert len(lines) == 3
        assert len(lines[0]) == 2


# ==========================================================================
# SingleBoxDetector
# ==========================================================================

class TestSingleBoxDetector:
    def _make_boxes(self):
        return [
            BoundingBox(0, 0, 50, 20, "A"),
            BoundingBox(100, 0, 50, 20, "B"),
            BoundingBox(0, 50, 50, 20, "C"),
        ]

    def test_cursor_inside_box(self):
        det = SingleBoxDetector()
        boxes = self._make_boxes()
        result = det.detect(boxes, 25, 10)
        assert result == [boxes[0]]

    def test_cursor_nearest_box(self):
        det = SingleBoxDetector()
        boxes = self._make_boxes()
        # Cursor between B (right) and A (left); closer to B
        result = det.detect(boxes, 90, 10)
        assert result == [boxes[1]]

    def test_empty_boxes(self):
        det = SingleBoxDetector()
        assert det.detect([], 50, 50) is None

    def test_beyond_max_distance(self):
        det = SingleBoxDetector(max_distance=10.0)
        boxes = [BoundingBox(0, 0, 50, 20)]
        # Cursor at (200, 200) – far from the box
        result = det.detect(boxes, 200, 200)
        assert result is None

    def test_within_max_distance(self):
        det = SingleBoxDetector(max_distance=100.0)
        boxes = [BoundingBox(0, 0, 50, 20)]
        result = det.detect(boxes, 80, 10)
        assert result == [boxes[0]]


# ==========================================================================
# ParagraphDetector
# ==========================================================================

def _make_paragraph_boxes(
    n_lines: int = 3,
    n_words: int = 3,
    line_h: int = 20,
    line_gap: int = 5,
    word_w: int = 80,
    word_gap: int = 5,
    x0: int = 10,
    y0: int = 10,
) -> list[BoundingBox]:
    """Build a synthetic paragraph layout."""
    boxes = []
    for row in range(n_lines):
        y = y0 + row * (line_h + line_gap)
        for col in range(n_words):
            x = x0 + col * (word_w + word_gap)
            boxes.append(BoundingBox(x, y, word_w, line_h))
    return boxes


class TestParagraphDetector:
    def test_detects_paragraph(self):
        det = ParagraphDetector()
        boxes = _make_paragraph_boxes(n_lines=3)
        # Cursor inside middle line
        mid_y = 10 + 25 + 10  # y0 + (line_h + gap) + some offset
        result = det.detect(boxes, 50, mid_y)
        assert result is not None
        assert len(result) == 9  # 3 lines × 3 words

    def test_single_line_below_min(self):
        det = ParagraphDetector(min_lines=2)
        boxes = _make_paragraph_boxes(n_lines=1)
        result = det.detect(boxes, 50, 20)
        assert result is None

    def test_cursor_far_from_text(self):
        det = ParagraphDetector()
        boxes = _make_paragraph_boxes(n_lines=3, y0=10)
        # Cursor at y=500, far below all text
        result = det.detect(boxes, 50, 500)
        assert result is None

    def test_height_mismatch_breaks_paragraph(self):
        """Lines with inconsistent heights should not be merged."""
        det = ParagraphDetector(height_ratio=0.1)
        # Two lines with very different heights
        line1 = [BoundingBox(0, 0, 100, 20)]
        line2 = [BoundingBox(0, 30, 100, 80)]  # 80 px height vs 20 → mismatch
        result = det.detect(line1 + line2, 50, 10)
        # Should not merge into 2 lines because height differs > 10%
        assert result is None  # only 1 line → below min_lines

    def test_large_gap_breaks_paragraph(self):
        """Lines separated by a large gap should not be merged."""
        det = ParagraphDetector(gap_ratio=0.5)
        # Line 1 at y=0, line 2 at y=200 (gap=180, way > 0.5 * 20)
        box1 = BoundingBox(0, 0, 100, 20)
        box2 = BoundingBox(0, 200, 100, 20)
        result = det.detect([box1, box2], 50, 10)
        assert result is None

    def test_two_line_paragraph(self):
        det = ParagraphDetector(min_lines=2)
        boxes = _make_paragraph_boxes(n_lines=2)
        cursor_y = 10 + 5  # inside first line
        result = det.detect(boxes, 50, cursor_y)
        assert result is not None
        assert len(result) == 6  # 2 lines × 3 words


# ==========================================================================
# TableRowDetector
# ==========================================================================

def _make_table_row(
    n_cols: int = 3,
    col_w: int = 60,
    col_h: int = 25,
    col_gap: int = 40,
    y0: int = 100,
    x0: int = 10,
) -> list[BoundingBox]:
    """Build a synthetic table row layout."""
    return [
        BoundingBox(x0 + i * (col_w + col_gap), y0, col_w, col_h)
        for i in range(n_cols)
    ]


class TestTableRowDetector:
    def test_detects_row(self):
        det = TableRowDetector()
        boxes = _make_table_row(n_cols=3)
        # Cursor inside first cell
        result = det.detect(boxes, 30, 110)
        assert result is not None
        assert len(result) == 3

    def test_cursor_not_in_box_returns_none(self):
        det = TableRowDetector()
        boxes = _make_table_row(n_cols=3)
        # Cursor above the row
        result = det.detect(boxes, 30, 50)
        assert result is None

    def test_too_many_cols_returns_none(self):
        det = TableRowDetector(max_cols=3)
        boxes = _make_table_row(n_cols=5)
        result = det.detect(boxes, 30, 110)
        assert result is None

    def test_boxes_too_close_together(self):
        """Boxes that touch each other should not be a table row."""
        det = TableRowDetector(min_h_gap_ratio=0.3)
        # Boxes with no gap between them
        boxes = [BoundingBox(i * 60, 100, 60, 25) for i in range(3)]
        result = det.detect(boxes, 30, 110)
        assert result is None

    def test_result_sorted_left_to_right(self):
        det = TableRowDetector()
        boxes = _make_table_row(n_cols=3)
        result = det.detect(boxes, 110, 110)  # cursor in middle cell
        assert result is not None
        xs = [b.x for b in result]
        assert xs == sorted(xs)

    def test_misaligned_bottom_returns_none(self):
        det = TableRowDetector(bottom_align_ratio=0.1)
        # Three boxes in the same vertical band (same center_y) but the last
        # starts higher and is taller, so its bottom is different.
        # box3: y=95, h=35 → center_y=112.5 (same as others), bottom=130.
        # med_bottom = 125; |130-125| = 5 > 0.1*25=2.5 → misalignment detected.
        row = [
            BoundingBox(10,  100, 60, 25),  # center_y=112.5, bottom=125
            BoundingBox(110, 100, 60, 25),  # center_y=112.5, bottom=125
            BoundingBox(210,  95, 60, 35),  # center_y=112.5, bottom=130 ← misaligned
        ]
        # Cursor inside first box
        result = det.detect(row, 15, 105)
        assert result is None


# ==========================================================================
# run_detectors integration
# ==========================================================================

class TestRunDetectors:
    def test_empty_boxes_returns_empty(self):
        assert run_detectors([], 0, 0) == []

    def test_falls_through_to_single_box(self):
        # A single isolated box – ParagraphDetector and TableRowDetector miss,
        # SingleBoxDetector catches it.
        boxes = [BoundingBox(50, 50, 100, 30, "hello")]
        result = run_detectors(boxes, 100, 65)
        assert result == boxes

    def test_custom_detector_takes_priority(self):
        # RangeDetector is already imported at module level – no re-import needed.
        class AlwaysFirst(RangeDetector):
            def detect(self, boxes, cx, cy):
                return [boxes[0]] if boxes else None

        boxes = _make_paragraph_boxes(n_lines=3)
        custom_chain = [AlwaysFirst(), SingleBoxDetector()]
        result = run_detectors(boxes, 50, 20, detectors=custom_chain)
        assert len(result) == 1

    def test_paragraph_wins_over_single(self):
        boxes = _make_paragraph_boxes(n_lines=3)
        cursor_y = 10 + 5  # inside first line
        result = run_detectors(boxes, 50, cursor_y)
        assert len(result) == 9  # whole paragraph, not just 1 box

    def test_default_detectors_nonempty(self):
        assert len(DEFAULT_DETECTORS) == 3


# ==========================================================================
# WindowsOcr – unit tests (no text-recognition hardware needed)
# ==========================================================================

class TestWindowsOcrUnit:
    def test_instantiation_raises_without_japanese(self):
        """Creating WindowsOcr('ja') raises MissingOcrLanguageError when the Japanese
        OCR capability is not installed."""
        from src.ocr.windows_ocr import WindowsOcr, MissingOcrLanguageError
        import winrt.windows.media.ocr as wocr
        import winrt.windows.globalization as glob

        ja = glob.Language("ja")
        if wocr.OcrEngine.is_language_supported(ja):
            pytest.skip("Japanese OCR language pack is installed – no error expected")

        with pytest.raises(MissingOcrLanguageError, match="install_ja_ocr"):
            WindowsOcr("ja")

    def test_pil_bgra_conversion(self):
        """_pil_to_software_bitmap round-trips without error."""
        from src.ocr.windows_ocr import _pil_to_software_bitmap

        img = Image.new("RGB", (64, 64), (255, 0, 128))
        bmp = _pil_to_software_bitmap(img)
        import winrt.windows.graphics.imaging as gi
        assert bmp.pixel_width == 64
        assert bmp.pixel_height == 64
        assert bmp.bitmap_pixel_format == gi.BitmapPixelFormat.BGRA8

    def test_language_tag_property(self):
        """language_tag returns a non-empty BCP-47 string (uses a known-installed language)."""
        from src.ocr.windows_ocr import WindowsOcr, MissingOcrLanguageError
        import winrt.windows.media.ocr as wocr
        import winrt.windows.globalization as glob

        # Pick the first available OCR language on this machine.
        available = [l.language_tag for l in wocr.OcrEngine.available_recognizer_languages]
        if not available:
            pytest.skip("No Windows OCR languages available")

        ocr = WindowsOcr(available[0])
        assert isinstance(ocr.language_tag, str)
        assert len(ocr.language_tag) > 0


@pytest.mark.xfail(
    strict=False,
    reason="Requires Japanese OCR language pack installed in Windows",
)
class TestWindowsOcrRecognise:
    def test_recognise_returns_boxes(self):
        from src.ocr.windows_ocr import WindowsOcr
        ocr = WindowsOcr("ja")
        # Create a blank white image – should return empty list, not crash.
        img = Image.new("RGB", (320, 100), (255, 255, 255))
        boxes = ocr.recognise(img)
        assert isinstance(boxes, list)

    def test_recognise_text_returns_str(self):
        from src.ocr.windows_ocr import WindowsOcr
        ocr = WindowsOcr("ja")
        img = Image.new("RGB", (320, 100), (255, 255, 255))
        text = ocr.recognise_text(img)
        assert isinstance(text, str)

