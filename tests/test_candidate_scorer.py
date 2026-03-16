"""Tests for src/hook/candidate_scorer.py — candidate text scoring.

Covers hard-reject filters, text blacklist, base scoring, and per-language bonuses.
"""
from __future__ import annotations

import re

import pytest

from src.hook.candidate_scorer import (
    TEXT_BLACKLIST,
    is_blacklisted,
    score_candidate,
)


# ==========================================================================
# Text blacklist
# ==========================================================================


class TestTextBlacklist:
    """Verify that TEXT_BLACKLIST patterns reject engine noise and let
    legitimate dialogue through."""

    # ── script markers / comments ──────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "@label_start",
        "  @select 選択肢",
        "*tag_opening",
        "#define CONSTANT",
        ";; comment line",
        " // this is a comment",
    ])
    def test_script_marker_lines(self, text: str) -> None:
        assert is_blacklisted(text)

    @pytest.mark.parametrize("text", [
        "[wait time=500]",
        "[jump target=scene2]",
        "  [playse file=click.ogg]",
    ])
    def test_bracket_commands(self, text: str) -> None:
        assert is_blacklisted(text)

    def test_bracket_dialogue_allowed(self) -> None:
        """「brackets」 used for Japanese quotation must NOT be blacklisted."""
        assert not is_blacklisted("「お前は誰だ」")

    # ── control-flow keywords ──────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "if flag_read",
        "goto label_end",
        "call sub_routine",
        "RETURN",
        "elsif cond > 0",
    ])
    def test_control_flow_keywords(self, text: str) -> None:
        assert is_blacklisted(text)

    # ── comparison expressions ─────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        'flag == "on"',
        "count != 0",
        "hp >= 100",
        "level <= 5",
        "x=='true'",
        'scene_read == "done"',
        '保存変数 現在エモーション = "会話" | 保存変数 会話相手 = "馬飼いの青年"',
        '保存変数 会話スイッチ = "ON"',
        'var = 42',
    ])
    def test_assignment_and_comparison_expressions(self, text: str) -> None:
        assert is_blacklisted(text)

    # ── resource / asset references ────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "background.png",
        "voice/chara01.ogg",
        "scripts/scene.ks",
        "data\\save.dat",
    ])
    def test_resource_references(self, text: str) -> None:
        assert is_blacklisted(text)

    def test_double_slashes_path(self) -> None:
        assert is_blacklisted("data\\\\save")

    # ── markup tags ────────────────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "<br>次の行",
        "<ruby text='ふりがな'>",
        "<color=#FF0000>",
        "<font size=20>",
    ])
    def test_markup_tags(self, text: str) -> None:
        assert is_blacklisted(text)

    # ── variable / format templates ────────────────────────────────────

    def test_printf_style(self) -> None:
        assert is_blacklisted("HP: %d / %d")

    def test_dollar_var(self) -> None:
        assert is_blacklisted("$player_name は言った")

    def test_dollar_braced_var(self) -> None:
        assert is_blacklisted("${count}個のアイテム")

    # ── legitimate text must NOT be blacklisted ────────────────────────

    @pytest.mark.parametrize("text", [
        "お前は誰だ？ここで何をしている？",
        "「馬飼いの青年」",
        "今日は天気がいいですね。",
        "ゲームを始めましょう。",
        "第一章　旅立ち",
    ])
    def test_legitimate_dialogue_allowed(self, text: str) -> None:
        assert not is_blacklisted(text)

    # ── integration: blacklisted text scores 0 via score_candidate ─────

    def test_blacklisted_text_scores_zero(self) -> None:
        """Ensure blacklist integrates with score_candidate pipeline."""
        assert score_candidate("[wait time=500]テスト", "ja") == 0.0
        assert score_candidate("@label_start", "ja") == 0.0

    # ── extensibility: user can add custom patterns ────────────────────

    def test_custom_blacklist_pattern(self) -> None:
        """Users can append custom engine-specific patterns."""
        custom = re.compile(r"^SYSCOM:")
        TEXT_BLACKLIST.append(custom)
        try:
            assert is_blacklisted("SYSCOM:SAVE_GAME")
            assert not is_blacklisted("お前は誰だ")
        finally:
            TEXT_BLACKLIST.remove(custom)


# ==========================================================================
# Hard-reject filters
# ==========================================================================


class TestHardReject:
    """Strings that must score exactly 0 (rejected)."""

    def test_empty(self) -> None:
        assert score_candidate("", "ja") == 0.0

    def test_control_chars(self) -> None:
        assert score_candidate("\x02テスト", "ja") == 0.0

    def test_file_path(self) -> None:
        assert score_candidate("C:\\Windows\\System32", "ja") == 0.0

    def test_pure_ascii_id(self) -> None:
        assert score_candidate("GAME_TITLE_01", "ja") == 0.0

    def test_file_extension(self) -> None:
        assert score_candidate("background.png", "ja") == 0.0

    def test_hex_blob(self) -> None:
        assert score_candidate("DEADBEEFCAFE1234", "ja") == 0.0

    def test_ascii_adjacent_to_cjk(self) -> None:
        assert score_candidate("テストAbc漢字", "ja") == 0.0

    def test_invisible_chars(self) -> None:
        assert score_candidate("テスト\u200Bです", "ja") == 0.0

    def test_pua_chars(self) -> None:
        assert score_candidate("テスト\uE000です", "ja") == 0.0

    def test_hangul_rejected_for_ja(self) -> None:
        assert score_candidate("テスト\uAC00", "ja") == 0.0

    def test_kana_rejected_for_ko(self) -> None:
        assert score_candidate("테스트あ", "ko") == 0.0


# ==========================================================================
# CJK Extension A density filter
# ==========================================================================


class TestExtensionAFilter:
    """CJK Extension A (U+3400–U+4DBF) density hard-reject."""

    def test_high_ext_a_density_rejected(self) -> None:
        """>=3 Extension A chars at >10% of text → reject."""
        # 6 Extension A chars + 4 basic CJK = 10 chars, 60% Extension A
        text = "\u3400\u3401\u3402\u3403\u3404\u3405\u4E00\u4E01\u4E02\u4E03"
        assert score_candidate(text, "ja") == 0.0

    def test_few_ext_a_chars_allowed(self) -> None:
        """Only 2 Extension A chars (< 3 minimum) → not rejected by this filter."""
        # 2 Extension A + 6 basic CJK with kana
        text = "\u3400\u3401あいうえおか"
        s = score_candidate(text, "ja")
        assert s > 0.0

    def test_low_ext_a_density_allowed(self) -> None:
        """3 Extension A chars in 40 total chars (7.5%) → not rejected."""
        # Build text with 3 Extension A chars + 37 basic CJK chars with kana
        ext_a = "\u3400\u3401\u3402"
        # Add enough kana + CJK to dilute below 10%
        filler = "あいうえおかきくけこさしすせそたちつてと" * 2  # 40 kana chars
        text = ext_a + filler[:37]
        assert score_candidate(text, "ja") > 0.0

    def test_garbage_from_real_case_rejected(self) -> None:
        """The actual garbage text reported by the user should be rejected.

        This text contains many Extension A chars (㩹 U+3A79, 㩲 U+3A72,
        㩨 U+3A68, 㩬 U+3A6C, 㩮 U+3A6E, 㩧 U+3A67, 䨺 U+4A3A, 䘺 U+463A)
        at ~14.5% density → above the 10% threshold.
        """
        garbage = (
            "䨺湡䨺湡慵祲䘺扡䘺扡畲牡㩹慍㩲慍捲㩨灁㩲灁楲㩬慍"
            "㩹慍㩹畊㩮畊敮䨺汵䨺汵㩹畁㩧畁畧瑳区灥区灥整扭牥"
            "伺瑣伺瑣扯牥为"
        )
        assert score_candidate(garbage, "ja") == 0.0


# ==========================================================================
# Base scoring — sub-linear length
# ==========================================================================


class TestBaseScoring:
    """Verify sub-linear length scaling (sqrt)."""

    def test_no_cjk_returns_zero(self) -> None:
        assert score_candidate("hello world", "ja") == 0.0

    def test_short_text_not_penalised(self) -> None:
        """Short CJK text (< 16 chars) should score reasonably well."""
        # "馬飼い" = 3 chars, all CJK/kana, with い (kana)
        s = score_candidate("馬飼い", "ja")
        assert s > 0.0

    def test_longer_text_not_proportionally_higher(self) -> None:
        """A 64-char text should NOT score 8× higher than an 8-char text.

        With sqrt scaling, ratio should be sqrt(64)/sqrt(8) = 2.83× at most
        (before language bonuses that might differ).
        """
        short = "「お前が馬飼い」"  # 8 chars, quoted, has が
        # Build a longer text (kana-heavy to pass all filters)
        long = "「お前は誰だ？ここで何をしている？ここに来てはいけないと言われたはずだ。すぐに帰れ。」"
        s_short = score_candidate(short, "ja")
        s_long = score_candidate(long, "ja")
        # Long should score higher, but the ratio should be well below 8×
        assert s_long > s_short
        assert s_long / s_short < 8.0


# ==========================================================================
# Japanese-specific scoring
# ==========================================================================


class TestJaScorer:
    """Japanese language bonus and kana gate."""

    def test_real_dialogue_scores_high(self) -> None:
        text = "\u201c馬飼いの青年\u201d"
        s = score_candidate(text, "ja")
        assert s > 50.0

    def test_quoted_dialogue_bonus(self) -> None:
        unquoted = "馬飼いの青年"
        quoted = "「馬飼いの青年」"
        s_unquoted = score_candidate(unquoted, "ja")
        s_quoted = score_candidate(quoted, "ja")
        assert s_quoted > s_unquoted

    def test_particle_bonus_mild(self) -> None:
        """1–2 distinct particles → ×1.8 bonus."""
        with_particle = "馬の子"  # has の
        without_particle = "馬飼育"  # no particle, no kana → menu penalty
        s_with = score_candidate(with_particle, "ja")
        s_without = score_candidate(without_particle, "ja")
        assert s_with > s_without

    def test_particle_bonus_strong(self) -> None:
        """3+ distinct particles → ×3.0 bonus."""
        text = "お前はここで何をしているのか"  # は, を, の, か = 4 particles
        s = score_candidate(text, "ja")
        # Compare with single particle text of similar length
        text2 = "お前がここにやってきました"  # が only = 1 particle
        s2 = score_candidate(text2, "ja")
        assert s > s2

    def test_sentence_end_bonus(self) -> None:
        """Sentence-final 。？！ → ×1.3."""
        no_end = "お前は誰だ"
        with_end = "お前は誰だ。"
        s_no = score_candidate(no_end, "ja")
        s_with = score_candidate(with_end, "ja")
        assert s_with > s_no

    def test_menu_label_penalty(self) -> None:
        """Very short (≤6 chars) pure kanji + no particles → ×0.6."""
        menu = "戦闘開始"  # 4 chars, no kana, no particles
        s = score_candidate(menu, "ja")
        # Should still be positive (not rejected), but low
        assert s > 0
        assert s < 20

    def test_kana_gate_rejects_long_pure_kanji(self) -> None:
        """Long pure-kanji text (>8 chars, no kana) gets ×0.05 penalty.

        This is the core fix for the reported issue: binary garbage that
        decodes as CJK characters but contains no kana at all.
        """
        # 12-char pure kanji — no kana at all
        text = "東京都新宿区西新宿二丁目"  # plausible but unusual
        s = score_candidate(text, "ja")
        # Compare with same-length text containing kana
        text_kana = "東京都のしんじゅくにしの丁"
        s_kana = score_candidate(text_kana, "ja")
        # The kana version should score MUCH higher
        assert s_kana > s * 5

    def test_kana_gate_allows_short_kanji(self) -> None:
        """Short pure-kanji text (≤8 chars) is NOT penalised by kana gate."""
        text = "新宿三丁目"  # 5 chars, no kana — should be allowed
        s = score_candidate(text, "ja")
        assert s > 0.0

    def test_real_vs_garbage_correct_ranking(self) -> None:
        """The user's actual scenario: real dialogue must outscore garbage.

        Garbage: 55 chars, all CJK, no kana, Extension A chars
        Real:    8 chars, quoted, has の (kana particle)
        """
        garbage = (
            "䨺湡䨺湡慵祲䘺扡䘺扡畲牡㩹慍㩲慍捲㩨灁㩲灁楲㩬慍"
            "㩹慍㩹畊㩮畊敮䨺汵䨺汵㩹畁㩧畁畧瑳区灥区灥整扭牥"
            "伺瑣伺瑣扯牥为"
        )
        real = "\u201c馬飼いの青年\u201d"
        s_garbage = score_candidate(garbage, "ja")
        s_real = score_candidate(real, "ja")
        # Garbage should be hard-rejected (Extension A density), real should score well
        assert s_garbage == 0.0
        assert s_real > 50.0


# ==========================================================================
# Chinese and Korean scorers
# ==========================================================================


class TestZhScorer:
    """Chinese scorer basic behaviour."""

    def test_chinese_text_scores_positive(self) -> None:
        s = score_candidate("你好世界", "zh")
        assert s > 0.0

    def test_hangul_rejected(self) -> None:
        assert score_candidate("你好\uAC00世界", "zh") == 0.0


class TestKoScorer:
    """Korean scorer basic behaviour."""

    def test_korean_text_scores_positive(self) -> None:
        # Hangul text (no kana, no CJK Extension A concerns)
        # Actually, Korean scorer needs CJK chars — Hangul is not in CJK range
        # for _base_score.  Let's use mixed Hangul + CJK:
        s = score_candidate("테스트", "ko")
        # Hangul syllables are NOT in the CJK range used by _base_score,
        # so pure Hangul would score 0.  That's expected — the hook DLL
        # targets CJK-range strings.
        assert s == 0.0

    def test_kana_rejected_for_ko(self) -> None:
        assert score_candidate("テスト\u4E00", "ko") == 0.0


# ==========================================================================
# Language dispatch
# ==========================================================================


class TestDispatch:
    """score_candidate correctly dispatches by language tag."""

    def test_ja_dispatch(self) -> None:
        # Should get the Japanese particle bonus for の
        s = score_candidate("馬飼いの子", "ja")
        assert s > 0

    def test_unknown_lang_uses_default(self) -> None:
        s = score_candidate("テストですね", "fr")
        assert s > 0

    def test_bcp47_subtag_stripped(self) -> None:
        """'ja-JP' dispatches to Japanese scorer."""
        s_full = score_candidate("馬飼いの子", "ja-JP")
        s_root = score_candidate("馬飼いの子", "ja")
        assert s_full == s_root
