"""Tests for src/correction.py — Levenshtein cross-matching."""
from __future__ import annotations

import pytest

from src.correction import best_match


class TestBestMatch:
    """Tests for best_match() with ratio and partial_ratio fallback."""

    def test_exact_match(self) -> None:
        assert best_match("テスト文字列", ["テスト文字列"]) == "テスト文字列"

    def test_empty_ocr(self) -> None:
        assert best_match("", ["テスト"]) is None

    def test_empty_candidates(self) -> None:
        assert best_match("テスト", []) is None

    def test_no_candidate_meets_threshold(self) -> None:
        assert best_match("テスト", ["ABCDEFGHIJKLMNOP"], threshold=90) is None

    def test_picks_best_among_candidates(self) -> None:
        ocr = "テスト文字列"
        candidates = ["テスト漢字列", "テスト文字列!", "完全に違う"]
        result = best_match(ocr, candidates)
        # "テスト文字列!" is closest to the OCR text.
        assert result == "テスト文字列!"

    def test_partial_ratio_fallback_when_candidate_is_substring(self) -> None:
        """When ratio is low due to length mismatch, partial_ratio kicks in."""
        ocr = "街に行くまでの道で牧場がある村がある一応馬がレンタル出来たはずだけど"
        # Candidate is only one dialog line (half the OCR text).
        candidate = "一応馬がレンタル出来たはずだけど"
        # fuzz.ratio would be ~50% (below default 60 threshold) but
        # partial_ratio should be very high because the candidate is
        # a near-exact substring.
        result = best_match(ocr, [candidate])
        assert result == candidate

    def test_partial_ratio_fallback_ocr_is_substring(self) -> None:
        """OCR text is shorter than candidate — partial still matches."""
        ocr = "レンタル出来たはず"
        # Candidate has context lines from memory scan.
        candidate = "表示キャラ名\n街に行くまでの道で牧場がある\nレンタル出来たはずだけど"
        result = best_match(ocr, [candidate])
        assert result == candidate

    def test_threshold_parameter(self) -> None:
        ocr = "テスト"
        candidates = ["テスX"]
        # Default threshold (60) should accept this.
        assert best_match(ocr, candidates) is not None
        # Completely different string should not match even with fallback.
        assert best_match("あいうえお", ["かきくけこさしすせそ"], threshold=90) is None

    def test_skip_empty_candidates(self) -> None:
        assert best_match("テスト", ["", "", "テスト"]) == "テスト"

    def test_all_empty_candidates(self) -> None:
        assert best_match("テスト", ["", ""]) is None
