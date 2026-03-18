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
    SingleBoxDetector,  # backwards-compat alias
    SingleLineDetector,
    TableRowDetector,
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
# SingleLineDetector
# ==========================================================================

class TestSingleLineDetector:
    def test_empty_returns_none(self):
        det = SingleLineDetector()
        assert det.detect([], 50, 50) is None

    def test_nearest_line_above(self):
        det = SingleLineDetector()
        a = BoundingBox(0, 0, 100, 20, "line A")
        b = BoundingBox(0, 50, 100, 20, "line B")
        result = det.detect([a, b], 50, 5)
        assert result == [a]

    def test_nearest_line_below(self):
        det = SingleLineDetector()
        a = BoundingBox(0, 0, 100, 20, "line A")
        b = BoundingBox(0, 50, 100, 20, "line B")
        result = det.detect([a, b], 50, 55)
        assert result == [b]

    def test_beyond_max_distance_returns_none(self):
        det = SingleLineDetector(max_distance=10.0)
        boxes = [BoundingBox(0, 0, 50, 20)]
        assert det.detect(boxes, 200, 200) is None

    def test_within_max_distance(self):
        det = SingleLineDetector(max_distance=100.0)
        boxes = [BoundingBox(0, 0, 50, 20)]
        result = det.detect(boxes, 80, 10)
        assert result == [boxes[0]]

    def test_single_box_alias(self):
        """SingleBoxDetector is a backwards-compat alias for SingleLineDetector."""
        assert SingleBoxDetector is SingleLineDetector



def _make_paragraph_lines(
    n_lines: int = 3,
    line_h: int = 20,
    line_gap: int = 5,
    line_w: int = 240,
    x0: int = 10,
    y0: int = 10,
) -> list[BoundingBox]:
    """Build a synthetic paragraph as one BoundingBox per OCR line."""
    return [
        BoundingBox(x0, y0 + i * (line_h + line_gap), line_w, line_h)
        for i in range(n_lines)
    ]


class TestParagraphDetector:
    def test_detects_paragraph(self):
        det = ParagraphDetector()
        lines = _make_paragraph_lines(n_lines=3)
        # Cursor inside the first line (y=10..30 → center=15)
        result = det.detect(lines, 50, 15)
        assert result is not None
        assert len(result) == 3

    def test_single_line_below_min(self):
        det = ParagraphDetector(min_lines=2)
        lines = _make_paragraph_lines(n_lines=1)
        result = det.detect(lines, 50, 20)
        assert result is None

    def test_cursor_far_from_text(self):
        det = ParagraphDetector()
        lines = _make_paragraph_lines(n_lines=3, y0=10)
        result = det.detect(lines, 50, 500)
        assert result is None

    def test_height_mismatch_breaks_paragraph(self):
        """A title line with very different height is excluded from paragraph."""
        det = ParagraphDetector(height_ratio=0.1)
        title = BoundingBox(0, 0,  200, 40, "title")
        line1 = BoundingBox(0, 50, 200, 20, "dialog 1")
        line2 = BoundingBox(0, 75, 200, 20, "dialog 2")
        # Cursor on line1 → anchor=line1; growing up, title h=40 vs median=20 → mismatch
        result = det.detect([title, line1, line2], 50, 55)
        assert result is not None
        assert title not in result
        assert line1 in result
        assert line2 in result

    def test_large_gap_breaks_paragraph(self):
        """Lines separated by a large gap should not be merged."""
        det = ParagraphDetector(gap_ratio=0.5)
        line1 = BoundingBox(0, 0,   200, 20)
        line2 = BoundingBox(0, 200, 200, 20)  # gap=180 >> 0.5×20=10
        result = det.detect([line1, line2], 50, 10)
        assert result is None

    def test_two_line_paragraph(self):
        det = ParagraphDetector(min_lines=2)
        lines = _make_paragraph_lines(n_lines=2)
        result = det.detect(lines, 50, 15)
        assert result is not None
        assert len(result) == 2

    def test_title_excluded_from_paragraph(self):
        """A large-font title above the dialog should be excluded.

        Layout (OCR line boxes):
          line 0: y=10,  h=40  (large title)
          line 1: y=60,  h=20  (dialog)
          line 2: y=85,  h=20  (dialog)
        """
        det = ParagraphDetector(min_lines=2)
        title = BoundingBox(100, 10, 200, 40, "馬飼いの青年")
        line1 = BoundingBox(50,  60, 300, 20, "……はあ、僕の馬……")
        line2 = BoundingBox(50,  85, 300, 20, "大事に育てたのになあ……")
        result = det.detect([title, line1, line2], 200, 90)
        assert result is not None
        assert title not in result
        assert line1 in result
        assert line2 in result

    def test_cursor_on_title_detects_title_only(self):
        """Cursor on the title: anchor=title, dialog lines have different height
        so they are excluded; only 1 line → below min_lines → None."""
        det = ParagraphDetector(min_lines=2, height_ratio=0.1)
        title = BoundingBox(100, 10, 200, 40, "馬飼いの青年")
        line1 = BoundingBox(50,  60, 300, 20, "……はあ、僕の馬……")
        line2 = BoundingBox(50,  85, 300, 20, "大事に育てたのになあ……")
        result = det.detect([title, line1, line2], 200, 30)
        # title alone does not reach min_lines=2
        assert result is None

    def test_cursor_on_anchor_in_middle_expands_both_ways(self):
        """Cursor on the middle line; paragraph should grow both up and down."""
        det = ParagraphDetector(min_lines=2)
        lines = _make_paragraph_lines(n_lines=5)
        # line index 2: y = 10 + 2*25 = 60, center ≈ 70
        mid = 10 + 2 * 25 + 8
        result = det.detect(lines, 50, mid)
        assert result is not None
        assert len(result) == 5


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
        assert run_detectors([], 0, 0) == ([], "")

    def test_falls_through_to_single_line(self):
        # A single isolated line – ParagraphDetector and TableRowDetector miss,
        # SingleLineDetector catches it.
        lines = [BoundingBox(50, 50, 100, 30, "hello")]
        result, name = run_detectors(lines, 100, 65)
        assert result == lines
        assert name == "SingleLineDetector"

    def test_custom_detector_takes_priority(self):
        # RangeDetector is already imported at module level – no re-import needed.
        class AlwaysFirst(RangeDetector):
            def detect(self, lines, cx, cy):
                return [lines[0]] if lines else None

        lines = _make_paragraph_lines(n_lines=3)
        custom_chain = [AlwaysFirst(), SingleLineDetector()]
        result, name = run_detectors(lines, 50, 20, detectors=custom_chain)
        assert len(result) == 1
        assert name == "AlwaysFirst"

    def test_paragraph_wins_over_single(self):
        lines = _make_paragraph_lines(n_lines=3)
        cursor_y = 10 + 5  # inside first line
        result, name = run_detectors(lines, 50, cursor_y)
        assert len(result) == 3  # whole paragraph, not just 1 line
        assert name == "ParagraphDetector"

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
        # Create a blank white image – should return empty lists, not crash.
        img = Image.new("RGB", (320, 100), (255, 255, 255))
        word_boxes, line_boxes = ocr.recognise(img)
        assert isinstance(word_boxes, list)
        assert isinstance(line_boxes, list)

    def test_recognise_text_returns_str(self):
        from src.ocr.windows_ocr import WindowsOcr
        ocr = WindowsOcr("ja")
        img = Image.new("RGB", (320, 100), (255, 255, 255))
        text = ocr.recognise_text(img)
        assert isinstance(text, str)

