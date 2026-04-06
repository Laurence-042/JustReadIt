# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for src/correction.py — needle-anchored correction."""
from __future__ import annotations

import pytest

from src.correction import (
    _find_needle_pos,
    _is_symbol_char,
    _normalize,
    _refine_left_boundary,
    _refine_right_boundary,
    _template_aware_score,
    best_match,
    best_match_with_details,
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
# best_match — parametrized basic cases
# ---------------------------------------------------------------------------
#
# (id, ocr, needle, candidates, must_contain_all, expect_none)

_BASIC_CASES: "list[tuple]" = [
    # -- edge cases ---------------------------------------------------------
    ("exact_match",
     "テスト文字列", "スト文字", ["テスト文字列"],
     ["テスト文字列"], False),

    ("empty_ocr",
     "", "テスト", ["テスト"],
     [], True),

    ("empty_candidates",
     "テスト", "テスト", [],
     [], True),

    ("empty_needle",
     "テスト", "", ["テスト"],
     [], True),

    ("all_empty_candidates",
     "テスト", "テスト", ["", ""],
     [], True),

    ("skip_empty_candidates",
     "テスト", "テスト", ["", "", "テスト"],
     ["テスト"], False),

    ("needle_absent_from_all_candidates",
     "テスト", "テスト", ["ABCDEFGHIJKLMNOP"],
     [], True),

    # -- candidate selection ------------------------------------------------
    ("picks_best_candidate",
     "テスト文字列", "スト文字",
     ["テスト漢字列", "テスト文字列!", "完全に違う"],
     ["テスト文字列!"], False),

    # -- ellipsis normalisation ---------------------------------------------
    # needle must be contiguous in both OCR and candidate; ‥‥/…… breaks
    # スト文字 so use テスト which spans only the Latin chars before the gap.
    ("ellipsis_variants_match",
     "テスト\u2025\u2025文字列", "テスト",
     ["テスト\u2026\u2026文字列"],
     ["テスト", "文字列"], False),

    # -- VN script with character-name tag ----------------------------------
    ("lightvn_name_tag_and_ellipsis",
     "……街に行くまでの道で、牧場がある村がある。\n一応、そこで馬がレンタル出来たはずだけど……",
     "に行くまで",
     ["~【馬飼いの青年】\n\u201c……街に行くまでの道で、牧場がある村がある。\n"
      "一応、そこで馬がレンタル出来たはずだけど……。\\w"],
     ["街に行くまでの道で", "レンタル出来たはずだけど"], False),

    ("lightvn_second_candidate_wins",
     "一応、そこで馬がレンタル出来たはず",
     "レンタル出来",
     ["~暗転解除\n~入力禁止\n~ジャンプ",
      "~【馬飼いの青年】\n\u201c……街に行く\n一応、そこで馬がレンタル出来たはずだけど……。\\w"],
     ["レンタル出来たはず"], False),
]


@pytest.mark.parametrize(
    "ocr, needle, candidates, must_contain, expect_none",
    [c[1:] for c in _BASIC_CASES],
    ids=[c[0] for c in _BASIC_CASES],
)
def test_basic_match(
    ocr: str,
    needle: str,
    candidates: "list[str]",
    must_contain: "list[str]",
    expect_none: bool,
) -> None:
    result = best_match(ocr, candidates, needle)
    if expect_none:
        assert result is None
    else:
        assert result is not None
        for sub in must_contain:
            assert sub in result, f"{sub!r} not in result {result!r}"


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
# Real-world regression tests
# ---------------------------------------------------------------------------


class TestRealWorld:
    """End-to-end OCR → memory → expected corrected text scenarios."""

    def test_garbage_prefix_stripped(self) -> None:
        """Bug report: ᦢ鿜輀退強化の丸薬を1個手に入れた → 強化の丸薬を1個手に入れた.

        The needle anchors alignment to the clean text region;
        _refine_left_boundary stops at the garbage CJK letters (no nearby symbols).
        """
        ocr = "強化の丸薬を1個手に入れた"
        needle = "化の丸薬"
        candidates = [
            "ᦢ鿜輀退強化の丸薬を1個手に入れた",
            # different context — needle present but surrounding text diverges
            "囉鹋退保存変数 道具_強化の丸薬所持数 += 1",
        ]
        result = best_match(ocr, candidates, needle)
        assert result == "強化の丸薬を1個手に入れた", repr(result)

    def test_engine_log_extracts_quoted_dialog(self) -> None:
        """Engine log line embeds dialog text after a quoted argument.

        _refine_left_boundary stops at the opening '\"' (LEFT_OPEN_SET),
        and the normalised result equals the OCR text.
        """
        ocr = "強化の丸薬を1個手に入れた"
        needle = "化の丸薬"
        log_line = (
            "\u17c0\u9957LogClockPassed: ~文字 アイテム入手文字 "
            'Corporate_Yawamin.ttf 20 \u30ab\u30e1\u30e9\u4ed8\u7740 '
            '"強化の丸薬を1個手に入れた'
        )
        result = best_match(ocr, [log_line], needle)
        assert result is not None
        # May include the leading " (stripped by _normalize during translation).
        assert "強化の丸薬を1個手に入れた" in result

    def test_fullwidth_punctuation_both_lines_present(self) -> None:
        """OCR renders ！？ as '! ?'; memory stores ！？.

        Both dialog lines must appear in the corrected result.
        """
        ocr = "馬飼いの青年\nう、馬小屋! ?\nた、確かに誰も居ないけど・・"
        needle = "確かに誰も"
        candidates = [
            "~【馬飼いの青年】\n\u201cう、馬小屋！？\nた、確かに誰も居ないけど……！！\\w",
            "た、確かに誰も居ないけど……！！",  # short copy — must NOT win alone
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None
        assert "う、馬小屋" in result.text, "First dialog line missing"
        assert "た、確かに誰も居ないけど" in result.text, "Second dialog line missing"

    def test_garbage_prefix_multiline_best_wins(self) -> None:
        """4-line OCR; several candidates have garbage prefixes or are shorter.

        The clean VN script block (candidate with ~【name】) must win because
        its aligned segment scores highest after the garbage offset is excluded.
        """
        ocr = (
            "馬飼いの青年\n"
            "・・・ん、ああ、おはよう。\n"
            "こめん。僕の責任だ。ヒスカに騙されてね・・\n"
            "帰ってきたら折檻してやる!"
        )
        needle = "おはよう"
        candidates = [
            # garbage prefix + 3 dialog lines, no name tag
            "嘞鮱\u1b00退……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            # garbage + single line
            "\u9000帰ってきたら折檻してやる！\\w",
            # BEST: full VN script block with name tag
            "~【馬飼いの青年】\n"
            "\u201c……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！\\w",
            # different dialogue (wrong context)
            "~【馬飼いの青年】\n\u201c……街に行くまでの道で、牧場がある村がある。",
            # another garbage prefix copy
            "大魇─\u8500……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None, "Expected a match"
        # The VN script candidate must win (has \w wait command
        # that garbage-prefix copies lack).
        assert "\\w" in result.text, (
            f"VN script candidate did not win — "
            f"score={result.score:.1f}: {result.text!r}"
        )
        assert "おはよう" in result.text, (
            f"First dialog line missing — score={result.score:.1f}: {result.text!r}"
        )
        assert "帰ってきたら折檻してやる" in result.text

    def test_template_variable_prefers_vn_script(self) -> None:
        """{{主人公}} template must score as well as the rendered literal '233'.

        Template-aware scoring treats the template as a wildcard so the
        authoritative VN script block wins over garbage-prefixed rendered copies.
        """
        ocr = (
            "落ち着いた人妻\n"
            "おはよう、233。\n"
            "あのニ人、あなたが居なくて大丈夫なのかしら\u30fb\u30fb"
        )
        needle = "大丈夫なのか"
        candidates = [
            # authoritative VN script block
            "~【落ち着いた人妻】\n"
            "\u201cおはよう、{{主人公}}。\n"
            "あの二人、あなたが居なくて大丈夫なのかしら……。\\w",
            # VN jump command (noise — needle absent)
            "~ジャンプ event/mobtalk_asnalo_00.txt 自由行動",
            # garbage prefix + last line only
            "\u0832\u6172\u7350\u66b6\u9b9c\u1dc3退あの二人、"
            "あなたが居なくて大丈夫なのかしら……。\\w",
            # rendered copy with literal '233'
            "言おはよう、233。\nあの二人、あなたが居なくて大丈夫なのかしら……。",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None
        # The VN script candidate must win (template variable present).
        assert "{{主人公}}" in result.text, (
            f"VN template missing — rendered copy won instead. "
            f"score={result.score:.1f}: {result.text!r}"
        )
        assert "おはよう" in result.text
        assert "大丈夫なのかしら" in result.text

    def test_multigarbage_real_world_vn_block_wins(self) -> None:
        """Real-world capture: 5 candidates — 4 with garbage prefixes and one
        wrong-context VN script block.  A valid match containing the core
        dialog must be returned (previously the algorithm returned None because
        OCR was *longer* than all candidates, causing edlib HW mode to reject
        every candidate).

        OCR from screenshot has header noise ("3馬飼いの青年 SKIP A …CLOSE"),
        OCR kana degradation (こめん → ごめん, ヒスカ → ビスカ).

        Note: when OCR is longer than all candidates (due to captured UI
        chrome), a reversed alignment fallback is used.  Differentiating the
        clean VN script block from garbage-prefix copies in this mode requires
        a more sophisticated scorer — current behaviour is to accept any
        candidate whose dialog content scores above the threshold.
        """
        ocr = (
            "3馬飼いの青年\n"
            "SKIP A [理0 SAVE LOAD EDIT CON呂CLOSE\n"
            "・・ん、ああ、おはよう。\n"
            "こめん。僕の責任だ。ヒスカに騙されてね・・\n"
            "帰ってきたら折檻してやる!"
        )
        needle = "おはよう"
        candidates = [
            # Clean VN script block with name tag and \w
            "~【馬飼いの青年】\n"
            "\u201c……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！\\w",
            # different dialogue (wrong context)
            "~【馬飼いの青年】\n\u201c……街に行くまでの道で、牧場がある村がある。",
            # garbage prefix :Ma + 3 dialog lines
            ":Ma\u9696\uffb4\u0e7c\u8000……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            # garbage prefix 茳￘ᕍ退
            "\u8cb3\uffD8\u154d\u9000……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            # garbage prefix 뱌菀蓥세 — only last line
            "\ub874\u83c0\u84e5\uc138\ufba7\ufd2a\uc246\u9000"
            "帰ってきたら折檻してやる！",
            # garbage prefix 㾀嬆鋺瘇ꬩﾳ᥻退
            "\u3f80\u5b06\u937a\u7607\uab29\uff73\u195b\u9000……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        # Primary guarantee: the algorithm must return a match (not None).
        # Previously this returned None because all candidates were shorter
        # than the noisy OCR, causing edlib HW mode to fail for every one.
        assert result is not None, (
            "Expected a match — reversed-alignment fallback should fire "
            "when OCR is longer than all candidates."
        )
        # The result should carry the core dialog content regardless of which
        # candidate won.
        assert "おはよう" in result.text, f"Dialog line 1 missing: {result.text!r}"
        assert "帰ってきたら折檻してやる" in result.text, f"Dialog line 3 missing: {result.text!r}"
        # The wrong-context candidate must not win (different story).
        assert "街に行くまでの道" not in result.text, (
            f"Wrong-context candidate won: {result.text!r}"
        )

    def test_clean_partial_does_not_beat_full_vn_block(self) -> None:
        """Regression: a clean partial candidate (last N lines only, no garbage
        prefix) must NOT beat the full VN script block even when OCR contains
        UI chrome that inflates its length past all candidates.

        Root cause: the partial candidate (shorter) is forced into the reversed
        alignment path and scored with ``fuzz.partial_ratio`` (lenient), while
        the full VN block goes through normal HW alignment and is scored with
        ``fuzz.ratio`` against the noisy OCR — an unfair comparison that made
        the partial candidate win.

        Fix: reversed-direction candidates are only used when *no* normal-
        direction candidate passed; the full VN block (normal path) therefore
        always beats any reversed-direction partial match.
        """
        ocr = (
            "3馬飼いの青年\n"
            "SKIP A [理0 SAVE LOAD EDIT CON呂CLOSE\n"
            "・・ん、ああ、おはよう。\n"
            "S S KI P\n"
            "こめん。僕の責任だ。ヒスカに騙されてね・・\n"
            "帰ってきたら折檻してやる!"
        )
        needle = "たら折檻"
        candidates = [
            # 1 — correct: full VN script block with name tag and \w
            "~【馬飼いの青年】\n"
            "\u201c……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！\\w",
            # 2 — wrong context (different dialogue)
            "~【馬飼いの青年】\n\u201c……街に行くまでの道で、牧場がある村がある。",
            # 3-5 — garbage-prefix copies of the full dialog
            "\u0712\u8e80\ue000……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            "\u0755\u7050\ufc00\ue000……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            "\u0bb4\u9340\uec01……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            # 6 — clean partial (last 2 lines only, no prefix) — reversed path
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None

        # The full VN block (candidate 1) must win over the clean partial.
        assert "~【馬飼いの青年】" in result.text, (
            f"Name tag missing — partial candidate won instead: {result.text!r}"
        )
        assert "……ん、あぁ、おはよう" in result.text, (
            f"First dialog line missing: {result.text!r}"
        )
        assert "帰ってきたら折檻してやる" in result.text

        # Wrong-context candidate must not win.
        assert "街に行くまでの道" not in result.text

    def test_short_single_line_does_not_beat_full_vn_block(self) -> None:
        """Regression: short candidates (last line only, some with garbage prefix)
        must NOT beat the full VN script block when OCR contains UI chrome.

        Bug report: OCR captured "綺麗な老女 / SKIP AUTO SAVE … / ああそうた…  /
        せつかくたから…".  Memory produced the correct full block plus several
        single-line candidates consisting only of the last sentence (with or
        without garbage prefixes).

        Root cause: single-line candidates were forced into the reversed alignment
        path and scored with ``fuzz.partial_ratio`` (finding their one clean line
        inside OCR's last line), inflating the score to ~89.5 % and beating the
        full block.

        Fix: reversed-direction score is weighted by coverage so that a candidate
        covering only ~32 % of the OCR length is penalised appropriately.
        """
        ocr = (
            "綺麗な老女\n"
            "SKIP AUTO SAVE LOAD EDIT( CONFI 9LOSE\n"
            "ああそうた。外に行くんたろう?\n"
            "せつかくたからこれを持っていきなさい。"
        )
        needle = "これを持"
        candidates = [
            # 1 — correct: full VN script block with character name tag and \w
            '~【綺麗な老女】\n\u201cあぁそうだ。外に行くんだろう？\nせっかくだからこれを持っていきなさい。\\w',
            # 2 — partial: second half of dialog only (reversed path, no prefix)
            "うだ。外に行くんだろう？\nせっかくだからこれを持っていきなさい。\\w",
            # 3-5 — single last-line copies with garbage prefixes (reversed path)
            "\u05ce\u31ed\u9001せっかくだからこれを持っていきなさい。",
            "\u01e1\u06b6\u8c62\u9001せっかくだからこれを持っていきなさい。\\w",
            "\u0cfb\u6c69\u2ac3\u0545\u6c6d\u04c7\u44df\u9000せっかくだからこれを持っていきなさい。\\w",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None

        # Full VN block (candidate 1) must win.
        assert "~【綺麗な老女】" in result.text, (
            f"Name tag missing — single-line candidate won instead: {result.text!r}"
        )
        assert "あぁそうだ。外に行くんだろう" in result.text, (
            f"First dialog line missing: {result.text!r}"
        )
        assert "せっかくだからこれを持っていきなさい" in result.text

    def test_name_tag_included_across_newline(self) -> None:
        """Bug: ~【name】 on a separate line must be included in the result.

        The old needle-anchored expansion stopped at ``\\n`` / ``"``
        boundaries and lost the name tag.  With edlib semi-global
        alignment in normalised space (``\\n`` → space), the alignment
        spans the full multi-line block.  ``_refine_left_boundary``
        then extends through ``【`` and ``~`` to capture the tag.
        """
        ocr = (
            "綺麗な老女\n"
            "私が調合した丸薬だよ。\n"
            "効果はあるはずたから、233が使いなさいな。"
        )
        needle = "調合した丸薬"
        candidates = [
            # authoritative VN script block with name tag on separate line
            "~【綺麗な老女】\n"
            "\u201c私が調合した丸薬だよ。\n"
            "効果はあるはずだから、{{主人公}}が使いなさいな。\\w",
            # garbage prefix + dialog only (no name tag)
            "\u1b00\u9c80退私が調合した丸薬だよ。\n"
            "効果はあるはずだから、233が使いなさいな。",
        ]
        result = best_match_with_details(ocr, candidates, needle)
        assert result is not None
        # Name tag must be present.
        assert "~【綺麗な老女】" in result.text, (
            f"Name tag missing — result: {result.text!r}"
        )
        # Dialog content must be present.
        assert "私が調合した丸薬だよ" in result.text
        assert "使いなさいな" in result.text

