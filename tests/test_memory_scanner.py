"""Tests for src/memory/ — memory scanner, needle picker, string extraction.

Pure unit tests use synthetic data.  ``TestSelfScan`` reads the *current*
process's memory (no external target needed) to verify the end-to-end
ReadProcessMemory pipeline.
"""
from __future__ import annotations

import os

import pytest

from src.memory import MemoryScanner, ScanResult, pick_needle
from src.memory._search import find_all_positions
from src.memory.scanner import (
    _extract_string,
    _extract_utf16le,
    _extract_byte_delimited,
    _is_cjk,
    _is_quality_text,
    _try_encode,
)


# ==========================================================================
# _is_cjk
# ==========================================================================


class TestIsCjk:
    def test_hiragana(self) -> None:
        assert _is_cjk("あ")
        assert _is_cjk("ん")

    def test_katakana(self) -> None:
        assert _is_cjk("ア")
        assert _is_cjk("ン")

    def test_kanji(self) -> None:
        assert _is_cjk("漢")
        assert _is_cjk("字")

    def test_ascii(self) -> None:
        assert not _is_cjk("A")
        assert not _is_cjk("1")

    def test_latin(self) -> None:
        assert not _is_cjk("é")

    def test_cjk_extension_a(self) -> None:
        # U+3400 (first char in Extension A)
        assert _is_cjk("\u3400")

    def test_fullwidth(self) -> None:
        # Fullwidth Latin is NOT CJK for our purposes.
        assert not _is_cjk("Ａ")  # U+FF21


# ==========================================================================
# pick_needle
# ==========================================================================


class TestPickNeedle:
    def test_pure_cjk(self) -> None:
        assert pick_needle("テスト文字列") == "テスト文字列"

    def test_mixed_text_picks_cjk_run(self) -> None:
        result = pick_needle("Hello テスト World")
        assert result == "テスト"

    def test_prefers_longest_run(self) -> None:
        result = pick_needle("aテストbこんにちはc")
        assert "こんにちは" in result

    def test_long_run_picks_from_middle(self) -> None:
        text = "あいうえおかきくけこさしすせそ"  # 15 chars
        result = pick_needle(text, max_length=6)
        assert len(result) == 6
        # Middle of 15 with length 6: start = (15-6)//2 = 4 → chars 4-9
        assert result == "おかきくけこ"

    def test_short_run_below_min_length(self) -> None:
        # CJK run length 2, below default min_length=3
        result = pick_needle("abテスcd")
        # Falls back to raw text
        assert result == "abテスcd"

    def test_min_length_override(self) -> None:
        result = pick_needle("abテスcd", min_length=2)
        assert result == "テス"

    def test_empty_string(self) -> None:
        assert pick_needle("") == ""

    def test_no_cjk_falls_back(self) -> None:
        result = pick_needle("Hello World!", max_length=5)
        assert result == "Hello"

    def test_max_length_caps_result(self) -> None:
        result = pick_needle("テスト文字列", max_length=4)
        assert len(result) == 4


# ==========================================================================
# _extract_utf16le
# ==========================================================================


class TestExtractUtf16le:
    def _make_utf16le(self, text: str) -> bytes:
        """Return null-terminated UTF-16LE bytes for *text*."""
        return text.encode("utf-16-le") + b"\x00\x00"

    def test_simple_string(self) -> None:
        data = self._make_utf16le("テスト")
        # Needle match at offset 0.
        result = _extract_utf16le(data, 0)
        assert result == "テスト"

    def test_match_in_middle(self) -> None:
        # "ABCテストDEF"
        text = "ABCテストDEF"
        data = self._make_utf16le(text)
        # 'テ' starts at char index 3, byte offset = 3 * 2 = 6
        result = _extract_utf16le(data, 6)
        assert result == text

    def test_odd_offset_returns_none(self) -> None:
        data = self._make_utf16le("テスト")
        assert _extract_utf16le(data, 1) is None

    def test_string_between_nulls(self) -> None:
        # Two strings separated by null terminator.
        s1 = "前の文".encode("utf-16-le") + b"\x00\x00"
        s2 = "次の文".encode("utf-16-le") + b"\x00\x00"
        data = s1 + s2
        # Match in second string — should only extract second string.
        offset = len(s1)  # Start of s2 data
        result = _extract_utf16le(data, offset)
        assert result == "次の文"

    def test_empty_data(self) -> None:
        assert _extract_utf16le(b"", 0) is None

    def test_no_null_terminator(self) -> None:
        # String without null terminator — extraction to buffer end.
        data = "テスト".encode("utf-16-le")
        result = _extract_utf16le(data, 0)
        assert result == "テスト"


# ==========================================================================
# _extract_byte_delimited (UTF-8)
# ==========================================================================


class TestExtractUtf8:
    def test_simple_string(self) -> None:
        data = "テスト".encode("utf-8") + b"\x00"
        result = _extract_byte_delimited(data, 0, "utf-8")
        assert result == "テスト"

    def test_match_in_middle(self) -> None:
        text = "ABCテストDEF"
        data = text.encode("utf-8") + b"\x00"
        # 'テ' in UTF-8 starts at byte 3 (after "ABC")
        result = _extract_byte_delimited(data, 3, "utf-8")
        assert result == text

    def test_string_between_nulls(self) -> None:
        s1 = "前".encode("utf-8") + b"\x00"
        s2 = "後".encode("utf-8") + b"\x00"
        data = s1 + s2
        offset = len(s1)
        result = _extract_byte_delimited(data, offset, "utf-8")
        assert result == "後"


# ==========================================================================
# _extract_byte_delimited (Shift-JIS)
# ==========================================================================


class TestExtractShiftJis:
    def test_simple_string(self) -> None:
        data = "テスト".encode("cp932") + b"\x00"
        result = _extract_byte_delimited(data, 0, "shift-jis")
        assert result == "テスト"

    def test_match_in_middle(self) -> None:
        text = "ABCテストDEF"
        data = text.encode("cp932") + b"\x00"
        # 'テ' in CP932: 0x83 0x65 — starts after "ABC" (3 bytes)
        result = _extract_byte_delimited(data, 3, "shift-jis")
        assert result == text


# ==========================================================================
# _is_quality_text
# ==========================================================================

class TestIsQualityText:
    def test_normal_text(self) -> None:
        assert _is_quality_text("テスト")

    def test_empty_rejected(self) -> None:
        assert not _is_quality_text("")

    def test_single_char_rejected(self) -> None:
        assert not _is_quality_text("あ")

    def test_whitespace_only_rejected(self) -> None:
        assert not _is_quality_text("   \n\t  ")

    def test_two_printable_accepted(self) -> None:
        assert _is_quality_text("AB")


# ==========================================================================
# _try_encode
# ==========================================================================


class TestTryEncode:
    def test_utf16le(self) -> None:
        result = _try_encode("テスト", "utf-16-le")
        assert result == "テスト".encode("utf-16-le")

    def test_utf8(self) -> None:
        result = _try_encode("テスト", "utf-8")
        assert result == "テスト".encode("utf-8")

    def test_shift_jis(self) -> None:
        result = _try_encode("テスト", "shift-jis")
        assert result == "テスト".encode("cp932")

    def test_unencodable_returns_none(self) -> None:
        # Emoji not in Shift-JIS
        assert _try_encode("😀", "shift-jis") is None


# ==========================================================================
# find_all_positions (Python fallback)
# ==========================================================================


class TestFindAllPositions:
    def test_basic(self) -> None:
        haystack = b"aabcaabcaabc"
        needle = b"abc"
        positions = find_all_positions(haystack, needle)
        assert positions == [1, 5, 9]

    def test_overlapping(self) -> None:
        haystack = b"aaaa"
        needle = b"aa"
        positions = find_all_positions(haystack, needle)
        assert positions == [0, 1, 2]

    def test_no_match(self) -> None:
        assert find_all_positions(b"hello", b"xyz") == []

    def test_empty_needle(self) -> None:
        assert find_all_positions(b"hello", b"") == []

    def test_empty_haystack(self) -> None:
        assert find_all_positions(b"", b"abc") == []

    def test_max_results(self) -> None:
        haystack = b"aaaa"
        positions = find_all_positions(haystack, b"a", max_results=2)
        assert len(positions) == 2

    def test_utf16le_pattern(self) -> None:
        text = "テスト"
        encoded = text.encode("utf-16-le")
        # Embed in a larger buffer.
        haystack = b"\x00" * 20 + encoded + b"\x00" * 20
        positions = find_all_positions(haystack, encoded)
        assert positions == [20]


# ==========================================================================
# Self-scan (end-to-end ReadProcessMemory on current process)
# ==========================================================================


class TestSelfScan:
    """Read the current process's own memory to verify the full pipeline.

    CPython stores BMP strings (hiragana, katakana, kanji) as UCS-2 which
    is identical to UTF-16LE on little-endian architectures.  So a string
    assigned in Python is findable by a UTF-16LE memory scan.
    """

    def test_find_known_string_utf16le(self) -> None:
        # A distinctive marker string that exists in this process's memory.
        # Using a unique combination to avoid matching test infrastructure.
        marker = "JRI独自検証マーカ42X"
        pid = os.getpid()

        with MemoryScanner(pid) as ms:
            results = ms.scan("独自検証マーカ", encodings=["utf-16-le"])

        texts = [r.text for r in results]
        assert any("独自検証マーカ" in t for t in texts), (
            f"Expected marker substring in scan results, got {texts[:5]}"
        )
        # Keep marker alive until after assertion.
        _ = marker

    def test_scan_result_fields(self) -> None:
        marker = "JRIメモリ確認QW7"
        pid = os.getpid()

        with MemoryScanner(pid) as ms:
            results = ms.scan("メモリ確認", encodings=["utf-16-le"])

        matching = [r for r in results if "メモリ確認" in r.text]
        assert matching, "Expected at least one result"

        r = matching[0]
        assert r.encoding == "utf-16-le"
        assert r.address > 0
        assert r.region_base > 0
        assert r.region_base <= r.address
        _ = marker

    def test_learned_encoding_updated(self) -> None:
        marker = "JRI学習エンコ8Z"
        pid = os.getpid()

        ms = MemoryScanner(pid)
        assert ms.learned_encoding is None

        results = ms.scan("学習エンコ")
        if results:
            assert ms.learned_encoding is not None

        ms.close()
        _ = marker

    def test_hot_region_reuse(self) -> None:
        """Second scan should benefit from hot-region caching."""
        marker_a = "JRI熱領域テストA9"
        marker_b = "JRI熱領域テストB9"
        pid = os.getpid()

        with MemoryScanner(pid) as ms:
            # First scan populates hot regions.
            results_a = ms.scan("熱領域テストA", encodings=["utf-16-le"])
            hot_count_after_first = len(ms._hot_regions)

            # Second scan should find the hot region.
            results_b = ms.scan("熱領域テストB", encodings=["utf-16-le"])

        assert any("熱領域テストA" in r.text for r in results_a)
        assert any("熱領域テストB" in r.text for r in results_b)
        assert hot_count_after_first > 0
        _ = (marker_a, marker_b)

    def test_context_manager_closes(self) -> None:
        pid = os.getpid()
        with MemoryScanner(pid) as ms:
            assert ms._handle != 0
        assert ms._handle == 0

    def test_scan_after_close_raises(self) -> None:
        pid = os.getpid()
        ms = MemoryScanner(pid)
        ms.close()
        with pytest.raises(RuntimeError, match="closed"):
            ms.scan("テスト")
