"""Tests for src/correction.py — Levenshtein cross-matching."""
from __future__ import annotations

import pytest

from src.correction import _best_line_window, _normalize, best_match, best_match_with_details


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    """Unicode normalisation used for scoring."""

    def test_two_dot_leader_to_ellipsis(self) -> None:
        # ‥ (U+2025) → … (U+2026)
        assert _normalize("あ\u2025い") == "あ\u2026い"

    def test_midline_ellipsis_to_ellipsis(self) -> None:
        # ⋯ (U+22EF) → … (U+2026)
        assert _normalize("あ\u22efい") == "あ\u2026い"

    def test_collapse_consecutive_ellipsis(self) -> None:
        assert _normalize("あ……い") == "あ…い"
        assert _normalize("あ…………い") == "あ…い"

    def test_strip_dialog_quote_left(self) -> None:
        # \u201c LEFT DOUBLE QUOTATION MARK at line start
        assert _normalize('\u201c街に行く') == "街に行く"

    def test_strip_dialog_quote_multiline(self) -> None:
        text = '\u201c一行目\n\u201c二行目'
        assert _normalize(text) == "一行目\n二行目"

    def test_strip_wait_command(self) -> None:
        assert _normalize("テスト。\\w") == "テスト。"

    def test_strip_multiple_commands(self) -> None:
        assert _normalize("テスト\\w\\n") == "テスト"

    def test_passthrough_normal_text(self) -> None:
        text = "普通のテキスト、何も変わらない。"
        assert _normalize(text) == text

    def test_fullwidth_punctuation_nfkc(self) -> None:
        # NFKC converts fullwidth ！？ → ASCII !?  so OCR and memory score equally.
        assert _normalize("馬小屋！？") == "馬小屋!?"

    def test_ocr_space_between_punctuation_stripped(self) -> None:
        # Windows OCR sometimes inserts a space between punctuation marks.
        assert _normalize("馬小屋! ?") == "馬小屋!?"

    def test_katakana_middle_dot_pair_to_ellipsis(self) -> None:
        # ・・ (U+30FB × 2) — OCR renders VN ellipsis glyphs as middle dots.
        assert _normalize("大丈夫\u30fb\u30fb") == "大丈夫\u2026"

    def test_katakana_middle_dot_triple(self) -> None:
        assert _normalize("\u30fb\u30fb\u30fbん") == "\u2026ん"

    def test_single_katakana_middle_dot_unchanged(self) -> None:
        # A solitary \u30fb is a legitimate separator, not an ellipsis.
        assert _normalize("A\u30fbB") == "A\u30fbB"

    def test_strip_template_variable(self) -> None:
        assert _normalize("おはよう、{{主人公}}。") == "おはよう、。"

    def test_strip_multiple_templates(self) -> None:
        assert _normalize("{{A}}と{{B}}") == "と"

    def test_unwrap_character_name_tag(self) -> None:
        assert _normalize("~【落ち着いた人妻】") == "落ち着いた人妻"

    def test_unwrap_name_tag_multiline(self) -> None:
        text = "~【馬飼いの青年】\nダイアログ"
        result = _normalize(text)
        assert result == "馬飼いの青年\nダイアログ"

    def test_strip_vn_command_line(self) -> None:
        assert _normalize("~ジャンプ somewhere.txt").strip() == ""

    def test_strip_vn_command_keeps_name_tag(self) -> None:
        text = "~【名前】\n~ジャンプ x.txt"
        result = _normalize(text)
        assert "名前" in result
        assert "ジャンプ" not in result


# ---------------------------------------------------------------------------
# _best_line_window
# ---------------------------------------------------------------------------


class TestBestLineWindow:
    """Line-window matching for VN script candidates."""

    def test_single_line_returns_none(self) -> None:
        """Single-line candidates are handled by full-text ratio."""
        assert _best_line_window("テスト", "テスト") is None

    def test_finds_matching_window(self) -> None:
        """Best window skips engine/name lines to match dialog."""
        ocr = _normalize("街に行くまでの道で牧場がある\n馬がレンタル出来たはず")
        candidate = (
            "~【馬飼いの青年】\n"
            "街に行くまでの道で牧場がある\n"
            "馬がレンタル出来たはず\n"
            "~ジャンプ"
        )
        result = _best_line_window(ocr, candidate)
        assert result is not None
        window_text, score = result
        assert score >= 90
        # Window should contain the dialog lines, not the ~command lines.
        assert "街に行くまでの道で" in window_text
        assert "レンタル出来たはず" in window_text

    def test_normalises_window_before_scoring(self) -> None:
        """Quote markers and wait commands are stripped for scoring."""
        ocr = _normalize("テスト文字列。")
        candidate = '\u201cテスト文字列。\\w\nダミー行'
        result = _best_line_window(ocr, candidate)
        assert result is not None
        _, score = result
        assert score >= 90

    def test_returns_original_text(self) -> None:
        """Returned window text is from original candidate, not normalised."""
        ocr = _normalize("テスト")
        candidate = '\u201cテスト\nダミー'
        result = _best_line_window(ocr, candidate)
        assert result is not None
        window_text, _ = result
        # Original text with the " prefix should be preserved.
        assert "\u201c" in window_text

    def test_below_threshold_returns_none(self) -> None:
        # _best_line_window always returns best window (no threshold here);
        # threshold filtering happens in best_match_with_details.
        ocr = _normalize("全く違うテキスト")
        candidate = "ABCDEF\nGHIJKL\nMNOPQR"
        result = _best_line_window(ocr, candidate)
        # Either no result (ASCII lines have no unicode match) or score very low.
        if result is not None:
            _, score = result
            assert score < 60


# ---------------------------------------------------------------------------
# best_match — integration
# ---------------------------------------------------------------------------


class TestBestMatch:
    """Tests for best_match() with normalisation + line-window + fallback."""

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
        candidate = "一応馬がレンタル出来たはずだけど"
        result = best_match(ocr, [candidate])
        assert result == candidate

    def test_line_window_returns_segment_not_whole(self) -> None:
        """Line-window matching returns the relevant segment, not the
        entire candidate including unrelated engine commands."""
        ocr = "レンタル出来たはず"
        candidate = "表示キャラ名\n街に行くまでの道で牧場がある\nレンタル出来たはずだけど"
        result = best_match(ocr, [candidate])
        assert result is not None
        # Should return a segment that contains the matching line.
        assert "レンタル出来たはずだけど" in result

    def test_threshold_parameter(self) -> None:
        ocr = "テスト"
        candidates = ["テスX"]
        assert best_match(ocr, candidates) is not None
        assert best_match("あいうえお", ["かきくけこさしすせそ"], threshold=90) is None

    def test_skip_empty_candidates(self) -> None:
        assert best_match("テスト", ["", "", "テスト"]) == "テスト"

    def test_all_empty_candidates(self) -> None:
        assert best_match("テスト", ["", ""]) is None

    # -- VN script real-world cases ----------------------------------------

    def test_lightvn_dialog_with_character_name(self) -> None:
        """Real case: memory has ~【name】 + " prefix + \\w suffix.

        OCR sees only the rendered dialog text with different ellipsis
        unicode (‥ U+2025 instead of … U+2026).
        """
        ocr = (
            "\u2025\u2025街に行くまでの道で、牧場がある村がある。\n"
            "一応、そこで馬がレンタル出来たはずだけど\u2025\u2025"
        )
        candidate = (
            "~【馬飼いの青年】\n"
            "\u201c……街に行くまでの道で、牧場がある村がある。\n"
            "一応、そこで馬がレンタル出来たはずだけど……。\\w"
        )
        result = best_match(ocr, [candidate])
        assert result is not None
        # Should contain the dialog text, not the character name tag.
        assert "街に行くまでの道で" in result
        assert "レンタル出来たはずだけど" in result

    def test_lightvn_dialog_multiple_candidates(self) -> None:
        """Pick the best segment when multiple scan results exist."""
        ocr = "一応、そこで馬がレンタル出来たはず"
        candidates = [
            "~暗転解除\n~入力禁止\n~ジャンプ",
            "~【馬飼いの青年】\n\u201c……街に行く\n一応、そこで馬がレンタル出来たはずだけど……。\\w",
        ]
        result = best_match(ocr, candidates)
        assert result is not None
        assert "レンタル出来たはず" in result

    def test_ellipsis_normalisation_enables_match(self) -> None:
        """Without normalisation, ‥‥ vs …… would drag the score below
        threshold.  Normalisation collapses both to a single …."""
        ocr = "テスト\u2025\u2025文字列"
        candidate = "テスト\u2026\u2026文字列"
        result = best_match(ocr, [candidate])
        assert result == candidate


class TestBestMatchWithDetails:
    def test_reports_phase_and_score(self) -> None:
        ocr = "た、確かに誰も居ないけど・・"
        candidates = [
            "幅聲■退う、馬小屋！？\nた、確かに誰も居ないけど……！！",
            "全然違う候補",
        ]
        result = best_match_with_details(ocr, candidates)
        assert result is not None
        assert result.phase in {"line-window", "full", "partial"}
        assert result.score >= result.threshold

    def test_matched_text_contains_dialog(self) -> None:
        """The matched result must contain the core dialog text regardless
        of which scoring phase wins."""
        ocr = "う、馬小屋！？\nた、確かに誰も居ないけど・・"
        candidate = "幅聲■退う、馬小屋！？\nた、確かに誰も居ないけど……！！"
        result = best_match_with_details(ocr, [candidate])
        assert result is not None
        assert result.score >= result.threshold
        assert "た、確かに誰も居ないけど" in result.text

    def test_realworld_ocr_fullwidth_vs_ascii_punctuation(self) -> None:
        """Real-world case: OCR transcribes ！？ as '! ?' (ASCII + space).

        The full dialog block is in memory; a short clean single-line copy
        is also present. Corrected result must include BOTH dialog lines,
        not just the last line that happened to score best on partial_ratio.
        """
        ocr = "1馬飼いの青年\nう、馬小屋! ?\nた、確かに誰も居ないけど・・"
        candidates = [
            "~【馬飼いの青年】\n\u201cう、馬小屋！？\nた、確かに誰も居ないけど……！！\\w",
            "た、確かに誰も居ないけど……！！",  # single-line clean copy — must NOT win alone
        ]
        result = best_match_with_details(ocr, candidates)
        assert result is not None
        assert "う、馬小屋" in result.text, "First dialog line must be present"
        assert "た、確かに誰も居ないけど" in result.text, "Second dialog line must be present"

    def test_realworld_partial_must_not_over_narrow(self) -> None:
        """Regression: 4-line OCR with 8 memory hits.

        Phase 3 (partial) used to report partial_ratio (inflated) as the
        comparable score, beating Phase 2's fuzz.ratio for the full dialog.
        Then `_best_line_window` narrowed the Phase 3 output to 2 lines,
        losing the character name and first dialogue line.

        After the fix, the comparable scores are all fuzz.ratio, so
        Phase 2's 4-line window (or Phase 1's full candidate) wins.
        """
        ocr = (
            "馬飼いの青年\n"
            "・・・ん、ああ、おはよう。\n"
            "こめん。僕の責任だ。ヒスカに騙されてね・・\n"
            "帰ってきたら折檻してやる!"
        )
        # Simulated memory scan results (first 5 of 8 hits).
        candidates = [
            # Candidate 1: garbage prefix + 3 dialogue lines (no name)
            "嘞鮱ᬀ退……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
            # Candidate 2: garbage prefix + single last line
            "혩鄳錀适帰ってきたら折檻してやる！\\w",
            # Candidate 3: the BEST — full VN script block with name tag
            "~【馬飼いの青年】\n"
            "\u201c……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！\\w",
            # Candidate 4: different dialogue (same character)
            "~【馬飼いの青年】\n"
            "\u201c……街に行くまでの道で、牧場がある村がある。",
            # Candidate 5: another garbage prefix copy
            "大魇─蠀……ん、あぁ、おはよう。\n"
            "……ごめん。僕の責任だ。ビスカに騙されてね……。\n"
            "帰ってきたら折檻してやる！",
        ]
        result = best_match_with_details(ocr, candidates)
        assert result is not None
        # Must include the character name AND the first dialogue line.
        assert "馬飼いの青年" in result.text, (
            f"Character name missing — got phase={result.phase} "
            f"score={result.score:.1f}: {result.text!r}"
        )
        assert "おはよう" in result.text, (
            f"First dialogue line missing — got phase={result.phase} "
            f"score={result.score:.1f}: {result.text!r}"
        )
        assert "帰ってきたら折檻してやる" in result.text

    def test_realworld_template_variable_prefers_vn_script(self) -> None:
        """Regression: {{template}} in VN script source must not lose to
        a rendered-text copy that contains the substituted value.

        OCR sees ``233`` (the player-chosen name), memory has both the
        template ``{{主人公}}`` form and a rendered form with ``233``.
        The VN script source (with ~【name】 tag) is the authoritative
        candidate and must win.
        """
        ocr = (
            "落ち着いた人妻\n"
            "おはよう、233。\n"
            "あのニ人、あなたが居なくて大丈夫なのかしら\u30fb\u30fb"
        )
        candidates = [
            # Candidate 0: the authoritative VN script block
            "~【落ち着いた人妻】\n"
            '\u201cおはよう、{{主人公}}。\n'
            "あの二人、あなたが居なくて大丈夫なのかしら……。\\w",
            # Candidate 1: VN jump command (noise)
            "~ジャンプ event/mobtalk_asnalo_00.txt 自由行動",
            # Candidate 2: garbage prefix + last line only
            "\u0832\u6172\u7350\u66b6\u9b9c\u1dc3退あの二人、"
            "あなたが居なくて大丈夫なのかしら……。\\w",
            # Candidate 3: garbage prefix + last line only
            "\ufC74\u9a96\u643e\u8000あの二人、"
            "あなたが居なくて大丈夫なのかしら……。\\w",
            # Candidate 4: rendered copy (has literal 233)
            "言おはよう、233。\n"
            "あの二人、あなたが居なくて大丈夫なのかしら……。",
            # Candidate 5: single last line
            "あの二人、あなたが居なくて大丈夫なのかしら……。",
        ]
        result = best_match_with_details(ocr, candidates)
        assert result is not None
        # Must contain the character name tag (from the VN script source).
        assert "落ち着いた人妻" in result.text, (
            f"Character name missing — got phase={result.phase} "
            f"score={result.score:.1f}: {result.text!r}"
        )
        # Must contain the greeting line.
        assert "おはよう" in result.text
        # Must contain the last dialogue line.
        assert "大丈夫なのかしら" in result.text
