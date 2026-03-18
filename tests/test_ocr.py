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
    TableRowDetector,   # backwards-compat alias for ParagraphDetector
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


# ==========================================================================
# ParagraphDetector (BFS proximity)
# ==========================================================================

class TestParagraphDetector:
    """Tests for the BFS proximity detector."""

    # ---- anchor finding ------------------------------------------------

    def test_cursor_not_on_any_line_returns_none(self):
        det = ParagraphDetector(cursor_margin=0)
        lines = _make_paragraph_lines(n_lines=3)
        result = det.detect(lines, 500, 500)
        assert result is None

    def test_cursor_on_line_returns_at_least_anchor(self):
        det = ParagraphDetector()
        lines = _make_paragraph_lines(n_lines=1)
        result = det.detect(lines, 50, 15)
        assert result == lines

    def test_cursor_margin_expands_anchor_lookup(self):
        det = ParagraphDetector(cursor_margin=10)
        line = BoundingBox(100, 100, 200, 20)
        result = det.detect([line], 200, 94)
        assert result == [line]

    # ---- vertical paragraph expansion ----------------------------------

    def test_three_line_paragraph(self):
        det = ParagraphDetector()
        lines = _make_paragraph_lines(n_lines=3)
        result = det.detect(lines, 50, 15)
        assert result is not None
        assert len(result) == 3

    def test_expands_both_ways_from_middle(self):
        det = ParagraphDetector()
        lines = _make_paragraph_lines(n_lines=5)
        # line index 2: y = 10 + 2*25 = 60, h=20 -> center~70; cursor at 68
        result = det.detect(lines, 50, 68)
        assert result is not None
        assert len(result) == 5

    def test_large_vertical_gap_breaks_expansion(self):
        """Gap > 1 x char_h stops expansion."""
        det = ParagraphDetector(max_v_gap_chars=1.0)
        line1 = BoundingBox(0, 0,   200, 20)
        line2 = BoundingBox(0, 200, 200, 20)
        result = det.detect([line1, line2], 50, 10)
        assert result == [line1]

    def test_propagates_through_intermediate_line(self):
        """Line 3 unreachable from anchor directly, but reachable via line 2."""
        det = ParagraphDetector(max_v_gap_chars=1.0)
        line1 = BoundingBox(0, 0,  200, 20)
        line2 = BoundingBox(0, 25, 200, 20)
        line3 = BoundingBox(0, 50, 200, 20)
        result = det.detect([line1, line2, line3], 50, 10)
        assert result is not None
        assert len(result) == 3

    def test_cursor_far_from_all_lines(self):
        det = ParagraphDetector(cursor_margin=0)
        lines = _make_paragraph_lines(n_lines=3, y0=10)
        result = det.detect(lines, 50, 500)
        assert result is None

    # ---- font-size gate ------------------------------------------------

    def test_title_excluded_by_size_difference(self):
        det = ParagraphDetector(size_ratio=0.4)
        title = BoundingBox(50,  0,  300, 40, "title_bigfont")
        line1 = BoundingBox(50, 50, 300, 20, "dialog_line1x")
        line2 = BoundingBox(50, 75, 300, 20, "dialog_line2x")
        result = det.detect([title, line1, line2], 200, 60)
        assert result is not None
        assert title not in result
        assert line1 in result
        assert line2 in result

    def test_title_returned_alone_when_cursor_on_title(self):
        det = ParagraphDetector(size_ratio=0.4)
        title = BoundingBox(50,  0,  300, 40, "title_bigfont")
        line1 = BoundingBox(50, 50, 300, 20, "dialog_line1x")
        line2 = BoundingBox(50, 75, 300, 20, "dialog_line2x")
        result = det.detect([title, line1, line2], 200, 20)
        assert result == [title]

    # ---- horizontal expansion (table-row-style) ------------------------

    def test_side_by_side_lines_grouped(self):
        det = ParagraphDetector(max_h_gap_chars=4.0)
        left  = BoundingBox(0,  0, 60, 20, "abc")
        right = BoundingBox(90, 0, 60, 20, "def")
        result = det.detect([left, right], 10, 10)
        assert result is not None
        assert left  in result
        assert right in result

    def test_too_wide_horizontal_gap_excluded(self):
        det = ParagraphDetector(max_h_gap_chars=4.0)
        left = BoundingBox(0,   0, 60, 20, "abc")
        far  = BoundingBox(560, 0, 60, 20, "xyz")
        result = det.detect([left, far], 10, 10)
        assert result == [left]

    # ---- alias ---------------------------------------------------------

    def test_table_row_detector_is_alias(self):
        assert TableRowDetector is ParagraphDetector


# ==========================================================================
# TableRowDetector (alias for ParagraphDetector -- smoke tests)
# ==========================================================================

def _make_table_row(
    n_cols: int = 3,
    col_w: int = 60,
    col_h: int = 20,
    col_gap: int = 20,
    text_per_col: str = "col",
    y0: int = 100,
    x0: int = 10,
) -> list[BoundingBox]:
    """Build a synthetic table row with text so _char_size works."""
    return [
        BoundingBox(x0 + i * (col_w + col_gap), y0, col_w, col_h, text_per_col)
        for i in range(n_cols)
    ]


class TestTableRowDetector:
    def test_detects_row(self):
        # col_gap=20, char_w=col_w/3=20, max_h=4*20=80 -> gap fits
        boxes = _make_table_row(n_cols=3)
        det = TableRowDetector()
        result = det.detect(boxes, 30, 110)
        assert result is not None
        assert len(result) == 3

    def test_cursor_not_on_any_box_returns_none(self):
        boxes = _make_table_row(n_cols=3)
        det = TableRowDetector(cursor_margin=0)
        result = det.detect(boxes, 30, 50)
        assert result is None

    def test_far_column_excluded(self):
        normal = _make_table_row(n_cols=3)
        far    = BoundingBox(500, 100, 60, 20, "col")
        det = TableRowDetector()
        result = det.detect(normal + [far], 30, 110)
        assert result is not None
        assert far not in result
        assert len(result) == 3

    def test_result_sorted_top_to_bottom(self):
        boxes = _make_table_row(n_cols=3)
        det = TableRowDetector()
        result = det.detect(boxes, 90, 110)
        assert result is not None
        ys = [b.y for b in result]
        assert ys == sorted(ys)


# ==========================================================================
# run_detectors integration
# ==========================================================================

class TestRunDetectors:
    def test_empty_boxes_returns_empty(self):
        assert run_detectors([], 0, 0) == ([], "")

    def test_falls_through_to_single_line(self):
        # Cursor far from the box -- ParagraphDetector returns None,
        # SingleLineDetector (max_distance=80) also returns None for a cursor
        # that is 300+ px away, so run_detectors returns ( [ ], "" ).
        lines = [BoundingBox(50, 50, 100, 30, "hello")]
        # Cursor at centre of box -- ParagraphDetector takes it.
        result_para, name_para = run_detectors(lines, 100, 65)
        assert name_para == "ParagraphDetector"
        # Cursor beyond SingleLineDetector max_distance (>80 px from box edge).
        result_far, name_far = run_detectors(lines, 50, 300)  # 270 px below
        assert result_far == []
        assert name_far == ""

    def test_custom_detector_takes_priority(self):
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
        assert len(DEFAULT_DETECTORS) == 2


# ==========================================================================
# WindowsOcr -- unit tests (no text-recognition hardware needed)
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
            pytest.skip("Japanese OCR language pack is installed -- no error expected")

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

