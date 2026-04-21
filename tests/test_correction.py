# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for src/correction.py — needle-anchored correction."""
from __future__ import annotations

import pytest

from src.correction import (
    _common_suffix_len,
    _find_needle_pos,
    _is_symbol_char,
    _normalize,
    _refine_left_boundary,
    _refine_right_boundary,
    _template_aware_score,
    best_match_with_details,
    strip_common_garbage,
)


# ---------------------------------------------------------------------------
# _normalize  (unchanged helper — keep full coverage)
# ---------------------------------------------------------------------------


class TestNormalize:
    """Unicode normalisation used for scoring."""

    def test_two_dot_leader_to_ellipsis(self) -> None:
        assert _normalize("あ\u2025い") == "あ\u2026い"

    def test_midline_ellipsis_to_ellipsis(self) -> None:
        assert _normalize("あ\u22efい") == "あ\u2026い"

    def test_collapse_consecutive_ellipsis(self) -> None:
        assert _normalize("あ……い") == "あ…い"
        assert _normalize("あ…………い") == "あ…い"

    def test_strip_dialog_quote_left(self) -> None:
        assert _normalize("\u201c街に行く") == "街に行く"

    def test_strip_dialog_quote_multiline(self) -> None:
        assert _normalize("\u201c一行目\n\u201c二行目") == "一行目\n二行目"

    def test_strip_wait_command(self) -> None:
        assert _normalize("テスト。\\w") == "テスト。"

    def test_strip_multiple_commands(self) -> None:
        assert _normalize("テスト\\w\\n") == "テスト"

    def test_passthrough_normal_text(self) -> None:
        text = "普通のテキスト、何も変わらない。"
        assert _normalize(text) == text

    def test_fullwidth_punctuation_nfkc(self) -> None:
        assert _normalize("馬小屋！？") == "馬小屋!?"

    def test_ocr_space_between_punctuation_stripped(self) -> None:
        assert _normalize("馬小屋! ?") == "馬小屋!?"

    def test_katakana_middle_dot_pair_to_ellipsis(self) -> None:
        assert _normalize("大丈夫\u30fb\u30fb") == "大丈夫\u2026"

    def test_katakana_middle_dot_triple(self) -> None:
        assert _normalize("\u30fb\u30fb\u30fbん") == "\u2026ん"

    def test_single_katakana_middle_dot_unchanged(self) -> None:
        assert _normalize("A\u30fbB") == "A\u30fbB"

    def test_strip_template_variable(self) -> None:
        assert _normalize("おはよう、{{主人公}}。") == "おはよう、。"

    def test_strip_multiple_templates(self) -> None:
        assert _normalize("{{A}}と{{B}}") == "と"

    def test_unwrap_character_name_tag(self) -> None:
        assert _normalize("~【落ち着いた人妻】") == "落ち着いた人妻"

    def test_unwrap_name_tag_multiline(self) -> None:
        assert _normalize("~【馬飼いの青年】\nダイアログ") == "馬飼いの青年\nダイアログ"

    def test_strip_vn_command_line(self) -> None:
        assert _normalize("~ジャンプ somewhere.txt").strip() == ""

    def test_strip_vn_command_keeps_name_tag(self) -> None:
        result = _normalize("~【名前】\n~ジャンプ x.txt")
        assert "名前" in result
        assert "ジャンプ" not in result


# ---------------------------------------------------------------------------
# _is_symbol_char
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch, expected", [
    # Punctuation / symbol → True
    ("…", True),   # U+2026 HORIZONTAL ELLIPSIS  (Po)
    ("！", True),  # U+FF01 fullwidth exclamation (Po)
    ("？", True),  # U+FF1F fullwidth question    (Po)
    ("、", True),  # U+3001 ideographic comma      (Po)
    ("。", True),  # U+3002 ideographic period     (Po)
    ("【", True),  # U+3010 LEFT BLACK LENTICULAR  (Ps)
    ("】", True),  # U+3011 RIGHT BLACK LENTICULAR (Pe)
    ("©", True),  # U+00A9 copyright sign         (So)
    # Letters / digits / spaces → False
    ("強", False),
    ("A", False),
    ("1", False),
    (" ", False),
    ("\n", False),
])
def test_is_symbol_char(ch: str, expected: bool) -> None:
    assert _is_symbol_char(ch) is expected


# ---------------------------------------------------------------------------
# _find_needle_pos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, needle, expected_range", [
    # Exact match at a known position.
    ("強化の丸薬を手に入れた", "化の丸薬", range(1, 2)),
    ("ABCDE", "BCD", range(1, 2)),
    # Exact at start / end.
    ("化の丸薬テスト", "化の丸薬", range(0, 1)),
    ("テスト化の丸薬", "化の丸薬", range(3, 4)),
    # Fuzzy match when exact fails (within gate).  The best window of 4
    # chars may align at pos 0 or 1 depending on score; accept either.
    ("強化の丸業を手に入れた", "化の丸薬", range(0, 2)),   # 業 ≈ 薬
    # No match below gate → None.
    ("ABCDE", "化の丸薬", None),
])
def test_find_needle_pos(
    text: str, needle: str, expected_range: "range | None"
) -> None:
    result = _find_needle_pos(text, needle)
    if expected_range is None:
        assert result is None
    else:
        assert result in expected_range


# ---------------------------------------------------------------------------
# _refine_left_boundary
# ---------------------------------------------------------------------------
#
# (text, rough_left, expected_refined_left)
# rough_left is the alignment-derived start position; the walker may move
# left to capture symbols the OCR missed while stopping at garbage bytes.

@pytest.mark.parametrize("text, rough_left, expected", [
    # Garbled CJK bytes (no symbols nearby) → stay.
    # ᦢ(Lo) 鿜(Lo) 輀(Lo) 退(Lo) are all letter-category, not symbols.
    ("ᦢ鿜輀退強化の丸薬", 4, 4),
    # Pure symbol prefix → walk all the way to start.
    ("……強化の丸薬", 2, 0),
    # Opening bracket → include bracket, stop.
    ("【強化の丸薬】", 1, 0),
    # 【A……BCD — 'A' is adjacent to '……' which is adjacent to '【' → include all.
    ("【A……BCD", 4, 0),
    # Newline terminator → stay.
    ("テスト\n強化", 4, 4),
    # ~ terminator → stay.
    ("~cmd強化", 4, 4),
    # Closing bracket on the left → stop before it.
    ("A】BCD", 3, 2),
    # Already at 0 → no movement.
    ("強化の丸薬", 0, 0),
    # Symbol sequence before text.
    ("？！強化", 2, 0),
])
def test_refine_left_boundary(text: str, rough_left: int, expected: int) -> None:
    assert _refine_left_boundary(text, rough_left) == expected


# ---------------------------------------------------------------------------
# _refine_right_boundary
# ---------------------------------------------------------------------------
#
# (text, rough_right, expected)
# rough_right is one past the last OCR char; walker includes trailing symbols
# / closing brackets that OCR missed.

@pytest.mark.parametrize("text, rough_right, expected", [
    # Trailing symbols included until end-of-string.
    ("強化の丸薬！！", 5, 7),
    # Closing bracket → include and stop; char after NOT included.
    # 強(0)化(1)の(2)丸(3)薬(4)？(5)】(6)E(7)
    ("強化の丸薬？】E", 5, 7),
    # Newline terminator → stop immediately.
    ("強化\nABC", 2, 2),
    # ~ terminator → stop immediately.
    ("強化~cmd", 2, 2),
    # \w suffix: '\' is Po (symbol), 'w' is letter with symbol nearby → both included.
    # 強(0)化(1)！(2)！(3)\(4)w(5)
    ("強化！！\\w", 2, 6),
    # Already at end → no movement.
    ("強化", 2, 2),
])
def test_refine_right_boundary(text: str, rough_right: int, expected: int) -> None:
    assert _refine_right_boundary(text, rough_right) == expected


# ---------------------------------------------------------------------------
# _template_aware_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ocr_raw, cand_raw, min_score, max_score", [
    # No template — behaves like fuzz.ratio on normalised text.
    ("強化の丸薬を手に入れた", "強化の丸薬を手に入れた", 99, 100),
    ("全然違う", "強化の丸薬を手に入れた", 0, 30),
    # Template variable matches any substituted value.
    ("おはよう、233。", "おはよう、{{主人公}}。", 95, 100),
    # Fixed parts present and in order → full score.
    ("ABCxxxDE", "ABC{{var}}DE", 95, 100),
    # Fixed parts present but out of order → lower score.
    ("DExxxABC", "ABC{{var}}DE", 0, 60),
])
def test_template_aware_score(
    ocr_raw: str, cand_raw: str, min_score: float, max_score: float
) -> None:
    score = _template_aware_score(_normalize(ocr_raw), cand_raw)
    assert min_score <= score <= max_score, (
        f"score={score:.1f} not in [{min_score}, {max_score}] "
        f"for ocr={ocr_raw!r} cand={cand_raw!r}"
    )


# ---------------------------------------------------------------------------
# MatchResult metadata
# ---------------------------------------------------------------------------


def test_match_result_phase_is_aligned() -> None:
    """Phase field is always 'aligned' in the new algorithm."""
    result = best_match_with_details(
        "強化の丸薬を1個手に入れた",
        ["強化の丸薬を1個手に入れた"],
        "化の丸薬",
    )
    assert result is not None
    assert result.phase == "aligned"
    assert result.score >= result.threshold


def test_match_result_score_high_for_clean_segment() -> None:
    result = best_match_with_details(
        "強化の丸薬を1個手に入れた",
        ["強化の丸薬を1個手に入れた"],
        "化の丸薬",
    )
    assert result is not None
    assert result.score > 95


# ---------------------------------------------------------------------------
# _common_suffix_len
# ---------------------------------------------------------------------------


class TestCommonSuffixLen:
    """Longest common suffix helper."""

    def test_identical(self) -> None:
        assert _common_suffix_len("abc", "abc") == 3

    def test_partial(self) -> None:
        assert _common_suffix_len("Xbc", "Ybc") == 2

    def test_no_overlap(self) -> None:
        assert _common_suffix_len("abc", "xyz") == 0

    def test_empty(self) -> None:
        assert _common_suffix_len("", "abc") == 0
        assert _common_suffix_len("abc", "") == 0

    def test_different_lengths(self) -> None:
        assert _common_suffix_len("hello", "llo") == 3


# ---------------------------------------------------------------------------
# strip_common_garbage
# ---------------------------------------------------------------------------


class TestStripCommonGarbage:
    """Cross-comparison garbage prefix stripping."""

    def test_basic_garbage_prefix(self) -> None:
        """Three dialogue copies with different 4-char garbage prefixes."""
        needle = "苦労する"
        candidates = [
            "句承搀退……はぁ。本当に騒々しい二人だ。\n233も苦労するね。",
            "庰攢匀訁……はぁ。本当に騒々しい二人だ。\n233も苦労するね。",
            "廂敜崀訁……はぁ。本当に騒々しい二人だ。\n233も苦労するね。",
        ]
        result = strip_common_garbage(candidates, needle)
        expected = "……はぁ。本当に騒々しい二人だ。\n233も苦労するね。"
        assert result[0] == expected
        assert result[1] == expected
        assert result[2] == expected

    def test_mixed_script_and_dialogue(self) -> None:
        """Script-source candidates must not be affected by dialogue ones."""
        needle = "苦労する"
        candidates = [
            '~もし (妊娠腹 == 2) -"{{主人公}}も苦労するね。\\w',
            "句承搀退……はぁ。本当に騒々しい二人だ。\n233も苦労するね。",
            "庰攢匀訁……はぁ。本当に騒々しい二人だ。\n233も苦労するね。",
        ]
        result = strip_common_garbage(candidates, needle)
        # Script candidate unchanged (common suffix with dialogue is only も).
        assert result[0] == candidates[0]
        # Dialogue candidates cleaned.
        expected = "……はぁ。本当に騒々しい二人だ。\n233も苦労するね。"
        assert result[1] == expected
        assert result[2] == expected

    def test_single_candidate(self) -> None:
        """Single candidate → no cross-comparison → unchanged."""
        result = strip_common_garbage(
            ["句承搀退……はぁ。苦労するね。"], "苦労する"
        )
        assert result == ["句承搀退……はぁ。苦労するね。"]

    def test_no_needle_in_candidates(self) -> None:
        """No candidate contains the needle → all unchanged."""
        result = strip_common_garbage(["foo", "bar"], "xyz")
        assert result == ["foo", "bar"]

    def test_identical_candidates_no_strip(self) -> None:
        """Identical candidates have no divergent prefix → no stripping."""
        c = "……はぁ。苦労するね。"
        result = strip_common_garbage([c, c], "苦労する")
        assert result == [c, c]

    def test_different_garbage_lengths(self) -> None:
        """Garbage prefixes of different lengths are both stripped."""
        needle = "テスト"
        candidates = [
            "XY……はぁ。テスト文字列",
            "ABCD……はぁ。テスト文字列",
        ]
        result = strip_common_garbage(candidates, needle)
        assert result[0] == "……はぁ。テスト文字列"
        assert result[1] == "……はぁ。テスト文字列"

    def test_short_common_part_not_stripped(self) -> None:
        """Common suffix shorter than needle → too risky → no stripping."""
        needle = "テスト"
        candidates = [
            "全く違うテキストもテスト結果",
            "別の文章もテスト結果",
        ]
        # Common suffix of pre-needle = "も" (1 char) < needle length (3).
        result = strip_common_garbage(candidates, needle)
        assert result == list(candidates)

    def test_garbage_longer_than_clean_part_not_stripped(self) -> None:
        """Garbage longer than clean common suffix → not confident → skip."""
        needle = "AB"
        candidates = [
            "XXXXXXXXXXcAB後",  # 10-char garbage, 1-char clean ("c")
            "YYYYYYYYYYcAB後",  # same structure
        ]
        # common suffix of pre-needle = "c" (1 char), garbage = 10 chars.
        # 10 > 1 → strip <= common guard fails → no stripping.
        # Also common (1) < needle len (2) → skipped by first guard.
        result = strip_common_garbage(candidates, needle)
        assert result == list(candidates)

    def test_empty_candidates(self) -> None:
        assert strip_common_garbage([], "x") == []

    def test_empty_needle(self) -> None:
        result = strip_common_garbage(["a", "b"], "")
        assert result == ["a", "b"]

