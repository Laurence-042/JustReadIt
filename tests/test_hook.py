"""Tests for src/hook/ — cleaner rule chain and TextHook.

Cleaner tests are pure unit tests (no hardware / live process needed).
TextHook tests require a live process and are skipped accordingly.
"""
from __future__ import annotations

import pytest

from src.hook.cleaner import (
    Cleaner,
    DEFAULT_CLEANERS,
    DeduplicateLines,
    StripControlChars,
    TrimWhitespace,
    run_cleaners,
)


# ==========================================================================
# StripControlChars
# ==========================================================================


class TestStripControlChars:
    def setup_method(self) -> None:
        self.c = StripControlChars()

    def test_empty_string(self) -> None:
        assert self.c.clean("") == ""

    def test_no_control_chars(self) -> None:
        assert self.c.clean("こんにちは") == "こんにちは"

    def test_null_byte_removed(self) -> None:
        assert self.c.clean("abc\x00def") == "abcdef"

    def test_c0_removed_except_whitespace(self) -> None:
        # \x01-\x08, \x0b, \x0c, \x0e-\x1f should be stripped
        raw = "a\x01b\x02c\x0bd\x0ce\x0ef\x1fg"
        assert self.c.clean(raw) == "abcdefg"

    def test_preserves_tab_newline_cr(self) -> None:
        assert self.c.clean("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_c1_removed(self) -> None:
        # \x7f (DEL) and \x80-\x9f (C1 control)
        raw = "hello\x7fworld\x80\x9f!"
        assert self.c.clean(raw) == "helloworld!"

    def test_mixed_japanese_with_control(self) -> None:
        raw = "\x02テスト\x03文字\x1f列"
        assert self.c.clean(raw) == "テスト文字列"


# ==========================================================================
# DeduplicateLines
# ==========================================================================


class TestDeduplicateLines:
    def setup_method(self) -> None:
        self.c = DeduplicateLines()

    def test_empty_string(self) -> None:
        assert self.c.clean("") == ""

    def test_no_duplicates(self) -> None:
        text = "line1\nline2\nline3"
        assert self.c.clean(text) == "line1\nline2\nline3"

    def test_consecutive_duplicates(self) -> None:
        text = "aaa\naaa\nbbb\nbbb\nbbb\nccc"
        assert self.c.clean(text) == "aaa\nbbb\nccc"

    def test_non_consecutive_duplicates_kept(self) -> None:
        text = "aaa\nbbb\naaa"
        assert self.c.clean(text) == "aaa\nbbb\naaa"

    def test_trailing_whitespace_ignored_for_dedup(self) -> None:
        text = "hello  \nhello\nhello \n"
        result = self.c.clean(text)
        assert result.count("hello") == 1

    def test_single_line(self) -> None:
        assert self.c.clean("only one") == "only one"

    def test_all_identical(self) -> None:
        text = "same\nsame\nsame\n"
        result = self.c.clean(text)
        assert result.strip() == "same"


# ==========================================================================
# TrimWhitespace
# ==========================================================================


class TestTrimWhitespace:
    def setup_method(self) -> None:
        self.c = TrimWhitespace()

    def test_empty_string(self) -> None:
        assert self.c.clean("") == ""

    def test_strips_per_line(self) -> None:
        assert self.c.clean("  hello  \n  world  ") == "hello\nworld"

    def test_removes_blank_lines(self) -> None:
        assert self.c.clean("a\n   \nb") == "a\nb"

    def test_strips_leading_trailing(self) -> None:
        assert self.c.clean("   text   ") == "text"

    def test_multiple_blank_lines_removed(self) -> None:
        text = "first\n\n\n\nsecond"
        assert self.c.clean(text) == "first\nsecond"

    def test_tabs_and_spaces(self) -> None:
        assert self.c.clean("\t hello \t") == "hello"


# ==========================================================================
# run_cleaners
# ==========================================================================


class TestRunCleaners:
    def test_empty_chain(self) -> None:
        assert run_cleaners([], "foo") == "foo"

    def test_single_cleaner(self) -> None:
        result = run_cleaners([TrimWhitespace()], "  hello  ")
        assert result == "hello"

    def test_full_default_chain(self) -> None:
        raw = "\x02テスト\n\x03テスト\n  結果  \n\n"
        result = run_cleaners(DEFAULT_CLEANERS, raw)
        assert result == "テスト\n結果"

    def test_control_then_dedup_then_trim(self) -> None:
        raw = "\x01hello\x02  \nhello  \n  world  \n  \n"
        result = run_cleaners(DEFAULT_CLEANERS, raw)
        assert result == "hello\nworld"

    def test_chain_order_matters(self) -> None:
        # With default order: control chars stripped first, then dedup, then trim
        raw = "abc\x01\nabc\n"
        result = run_cleaners(DEFAULT_CLEANERS, raw)
        assert result == "abc"


# ==========================================================================
# DEFAULT_CLEANERS sanity
# ==========================================================================


class TestDefaultCleaners:
    def test_length(self) -> None:
        assert len(DEFAULT_CLEANERS) == 3

    def test_types(self) -> None:
        assert isinstance(DEFAULT_CLEANERS[0], StripControlChars)
        assert isinstance(DEFAULT_CLEANERS[1], DeduplicateLines)
        assert isinstance(DEFAULT_CLEANERS[2], TrimWhitespace)

    def test_all_are_cleaners(self) -> None:
        for c in DEFAULT_CLEANERS:
            assert isinstance(c, Cleaner)

