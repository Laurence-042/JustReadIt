"""Tests for src/memory/ — memory scanner, needle picker, string extraction.

Pure unit tests use synthetic data.  ``TestSelfScan`` reads the *current*
process's memory (no external target needed) to verify the end-to-end
ReadProcessMemory pipeline.
"""
from __future__ import annotations

import os

import pytest

from src.memory import MemoryScanner, ScanResult, pick_needles
from src.memory._search import find_all_positions
from src.memory.scanner import (
    _extract_string,
    _extract_utf16le,
    _extract_byte_delimited,
    _is_cjk,
    _is_noisy_line,
    _is_quality_text,
    _refine_to_lines,
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
# pick_needles
# ==========================================================================


class TestPickNeedles:
    def test_empty_string(self) -> None:
        assert pick_needles("") == []

    def test_short_cjk_returns_single(self) -> None:
        # 3 chars <= needle_length=4 → returns as-is
        assert pick_needles("テスト") == ["テスト"]

    def test_medium_run_returns_centre_only(self) -> None:
        # 7 chars, needle_length=4 → n < needle_length*2=8 → centre only
        result = pick_needles("あいうえおかき")
        assert len(result) == 1
        # center = (7-4)//2 = 1 → "いうえお"
        assert result[0] == "いうえお"

    def test_long_run_returns_three_needles(self) -> None:
        text = "あいうえおかきくけこさしすせそ"  # 15 chars
        result = pick_needles(text)
        assert len(result) == 3
        # centre: (15-4)//2 = 5 → "かきくけ"
        # start: 0 → "あいうえ"
        # end: 15-4=11 → "しすせそ"
        assert result[0] == "かきくけ"
        assert result[1] == "あいうえ"
        assert result[2] == "しすせそ"

    def test_eight_char_run_returns_two_needles(self) -> None:
        text = "あいうえおかきく"  # 8 chars, >= needle_length*2
        result = pick_needles(text)
        # centre: (8-4)//2 = 2 → "うえおか"
        # start: 0 → "あいうえ"
        # end: 8-4=4 → "おかきく"
        assert len(result) == 3
        assert result[0] == "うえおか"
        assert result[1] == "あいうえ"
        assert result[2] == "おかきく"

    def test_deduplicates_identical_needles(self) -> None:
        # 8 chars with needle_length=4 — centre=2, start=0, end=4
        # All different, but for a shorter run they might collide.
        text = "あいうえおかきく"  # exactly 8, center=2 start=0 end=4
        result = pick_needles(text, needle_length=4)
        assert len(result) == len(set(result))

    def test_no_cjk_falls_back(self) -> None:
        result = pick_needles("Hello World!", needle_length=5)
        assert result == ["Hello"]

    def test_custom_needle_length(self) -> None:
        text = "あいうえおかきくけこさし"  # 12 chars
        result = pick_needles(text, needle_length=3)
        assert all(len(n) == 3 for n in result)
        assert len(result) == 3

    def test_max_needles_limits_output(self) -> None:
        text = "あいうえおかきくけこさしすせそ"  # 15 chars
        result = pick_needles(text, max_needles=2)
        assert len(result) == 2

    def test_mixed_text_uses_longest_run(self) -> None:
        result = pick_needles("aテストbこんにちはc")
        # こんにちは (5 chars) > テスト (3 chars)
        # 5 < 4*2=8 → centre only: (5-4)//2=0 → "こんにち"
        assert result == ["こんにち"]


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
# _refine_to_lines
# ==========================================================================


class TestRefineToLines:
    """Line-level refinement for VN script blocks."""

    def test_short_text_unchanged(self) -> None:
        """Text shorter than threshold is returned as-is."""
        short = "テスト文字列"
        assert _refine_to_lines(short, "テスト") == short

    def test_isolates_needle_line(self) -> None:
        script = "\r\n".join([
            "~ジャンプ scene_02",
            "~表示 キャラ名",
            "街に行くまでの道で、牧場がある村がある。",
            "一応、そこで馬がレンタル出来たはずだけど",
            "~入力禁止 false",
            "~暗転 黒",
        ])
        result = _refine_to_lines(script, "レンタル出来たは")
        assert "レンタル出来たは" in result
        # Should include the dialog line and nearby context.
        lines = result.split("\n")
        assert len(lines) <= 7  # context_lines=3 default

    def test_trims_empty_boundary_lines(self) -> None:
        script = "\r\n".join([
            "",
            "",
            "テスト対象のテキスト",
            "",
            "",
        ])
        result = _refine_to_lines(script, "テスト対象")
        assert result.strip() == "テスト対象のテキスト"

    def test_context_lines_parameter(self) -> None:
        lines = [f"line{i}" for i in range(20)]
        lines[10] = "needleの入った行"
        script = "\r\n".join(lines)
        result = _refine_to_lines(script, "needle", context_lines=1)
        result_lines = result.split("\n")
        assert len(result_lines) == 3  # 1 before + needle + 1 after

    def test_needle_not_found_returns_original(self) -> None:
        text = "何もない文章"
        assert _refine_to_lines(text, "存在しない") == text

    def test_needle_at_start(self) -> None:
        script = "\r\n".join([
            "needleの最初の行",
            "二行目",
            "三行目",
            "四行目",
        ])
        result = _refine_to_lines(script, "needle", context_lines=2)
        assert result.startswith("needleの最初の行")

    def test_needle_at_end(self) -> None:
        script = "\r\n".join([
            "一行目",
            "二行目",
            "三行目",
            "needleの最後の行",
        ])
        result = _refine_to_lines(script, "needle", context_lines=2)
        assert result.endswith("needleの最後の行")

    def test_drops_noisy_surrounding_line(self) -> None:
        text = "\n".join([
            "뿅臥ᨀ阀う、馬小屋！？",
            "た、確かに誰も居ないけど……！！",
        ])
        result = _refine_to_lines(text, "も居ない", context_lines=1)
        assert "た、確かに誰も居ないけど" in result
        assert "뿅臥ᨀ阀" not in result


class TestNoiseLineHeuristic:
    def test_noisy_line_true(self) -> None:
        assert _is_noisy_line("뿅臥ᨀ阀う、馬小屋！？")

    def test_normal_japanese_line_false(self) -> None:
        assert not _is_noisy_line("馬飼いの青年")


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
# Multi-boundary extraction — right boundary
# ==========================================================================


class TestRightBoundary:
    """``_extract_utf16le`` / ``_extract_byte_delimited`` right-boundary refinement."""

    @staticmethod
    def _utf16le_block(*parts: str) -> bytes:
        """Encode *parts* as a single null-terminated UTF-16LE block."""
        return "".join(parts).encode("utf-16-le") + b"\x00\x00"

    def test_short_block_uses_null_boundary(self) -> None:
        # Below threshold (200 chars) → strong-boundary unchanged.
        text = "テストです" * 5  # 25 chars, well below threshold
        data = self._utf16le_block(text)
        result = _extract_utf16le(data, 0)
        assert result == text

    def test_long_block_with_newline_truncates(self) -> None:
        # > 200 chars total, with \n inside → right side cuts at \n.
        prefix = "前置パディング" * 30  # ~210 chars before match
        match = "テストの本文"
        suffix = "\n" + ("後置パディング" * 30)  # forces \n inside the block
        data = self._utf16le_block(prefix + match + suffix)
        match_pos = (len(prefix)) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        # Newline is excluded from the right side.
        assert "\n" not in result
        assert "後置パディング" not in result

    def test_long_block_with_closing_quote_truncates(self) -> None:
        # > threshold, no \n, but a closing quote → cuts at quote.
        prefix = "ぱでぃんぐ" * 50
        match = "テスト本文"
        suffix = "」" + ("あとぱでぃんぐ" * 50)
        data = self._utf16le_block(prefix + match + suffix)
        match_pos = len(prefix) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        assert "」" not in result
        assert "あとぱでぃんぐ" not in result

    def test_picks_nearest_when_both_present(self) -> None:
        # Both \n and 」 follow the match — cut at the closer one.
        # Need long *suffix* (post-match) to engage right-boundary refinement.
        match = "テスト"
        # \n appears first (closer), then 」 farther away.
        suffix = "あ\nい」う" + ("あとぱでぃんぐ" * 50)
        data = self._utf16le_block(match + suffix)
        match_pos = 0
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        # Should stop at the *nearer* boundary (\n).
        assert "い" not in result
        assert "」" not in result


# ==========================================================================
# Multi-boundary extraction — left boundary
# ==========================================================================


class TestLeftBoundary:
    """Left-boundary refinement with strong-quote / weak-newline fallback."""

    @staticmethod
    def _utf16le_block(text: str) -> bytes:
        return text.encode("utf-16-le") + b"\x00\x00"

    def test_clean_open_quote_used(self) -> None:
        # Quote close to match, clean text between → quote wins.
        far_prefix = "とおいぱでぃんぐ" * 30  # > threshold from match
        # 「 is ~5 chars before match — within threshold and clean.
        near = "「ちかい"
        match = "テスト本文"
        suffix = "」"
        data = self._utf16le_block(far_prefix + near + match + suffix)
        match_pos = (len(far_prefix) + len(near)) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        # Quote is excluded; far_prefix is cut off.
        assert "「" not in result
        assert "とおいぱでぃんぐ" not in result
        assert "ちかい" in result

    def test_noisy_quote_falls_back_to_newline(self) -> None:
        # Quote exists but the slice between quote and match is full of
        # binary noise → must reject quote and fall back to \n.
        far_prefix = "ぱでぃんぐ" * 50
        # Layout from left: …far_prefix…「NOISE\ncleantext MATCH
        # (the opening quote is part of `noisy_chunk`).  The newline is
        # closer to MATCH than the quote, and the quote→newline slice is
        # full of control chars → quote is rejected, \n wins.
        noisy_chunk = "「" + "\u0001\u0002\u0003\u0004\u0005" + "\nきれい"
        match = "テスト"
        data = self._utf16le_block(far_prefix + noisy_chunk + match)
        match_pos = (len(far_prefix) + len(noisy_chunk)) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        # Should NOT include the noisy block before \n.
        assert "\u0001" not in result
        assert "「" not in result
        # Should include the clean text after \n.
        assert "きれい" in result

    def test_no_quote_uses_newline(self) -> None:
        # No quotes anywhere — \n is the only soft boundary.
        far_prefix = "ぱでぃんぐ" * 60
        near = "ちかい\nあとろう"
        match = "テスト"
        data = self._utf16le_block(far_prefix + near + match)
        match_pos = (len(far_prefix) + len(near)) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        assert match in result
        assert "あとろう" in result
        assert "\n" not in result
        assert "ちかい" not in result
        assert "ぱでぃんぐ" not in result

    def test_no_soft_boundary_falls_back_to_null(self) -> None:
        # Block > threshold but contains no \n / quote → use \0 boundary.
        prefix = "ぱでぃんぐ" * 60  # > threshold, no soft boundary
        match = "テスト"
        data = self._utf16le_block(prefix + match)
        match_pos = len(prefix) * 2
        result = _extract_utf16le(data, match_pos)
        assert result is not None
        # With no soft boundary, fallback to \0 — full block returned.
        assert result.endswith(match)
        assert result.startswith("ぱ")


# ==========================================================================
# Multi-boundary extraction — encoding symmetry
# ==========================================================================


class TestBoundarySymmetry:
    """Same logical layout should yield equivalent windows across encodings."""

    @staticmethod
    def _build(prefix: str, match: str, suffix: str, encoding: str) -> tuple[bytes, int]:
        text = prefix + match + suffix
        codec = "cp932" if encoding == "shift-jis" else encoding
        encoded = text.encode(codec)
        if encoding == "utf-16-le":
            data = encoded + b"\x00\x00"
        else:
            data = encoded + b"\x00"
        # Compute byte offset of `match` start.
        match_pos = len(prefix.encode(codec))
        return data, match_pos

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-8", "shift-jis"])
    def test_newline_truncation_consistent(self, encoding: str) -> None:
        # Long suffix to exceed forward-byte thresholds in all encodings.
        prefix = "ぱでぃんぐ" * 5  # short — left side need not engage
        match = "テスト本文"
        suffix = "\n" + ("あとぱでぃんぐ" * 80)
        data, match_pos = self._build(prefix, match, suffix, encoding)
        if encoding == "utf-16-le":
            result = _extract_utf16le(data, match_pos)
        else:
            result = _extract_byte_delimited(data, match_pos, encoding)
        assert result is not None
        assert match in result
        assert "\n" not in result
        assert "あとぱでぃんぐ" not in result

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-8", "shift-jis"])
    def test_open_quote_consistent(self, encoding: str) -> None:
        # Far prefix sized to exceed left-side byte threshold for all
        # encodings (UTF-8 ≈ 3 bytes/char CJK).
        far_prefix = "とおい" * 250  # 750 chars
        near = "「ちかい"
        match = "テスト"
        suffix = "」"
        data, match_pos = self._build(far_prefix + near, match, suffix, encoding)
        if encoding == "utf-16-le":
            result = _extract_utf16le(data, match_pos)
        else:
            result = _extract_byte_delimited(data, match_pos, encoding)
        assert result is not None
        assert match in result
        assert "「" not in result
        assert "とおい" not in result
        assert "ちかい" in result


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
