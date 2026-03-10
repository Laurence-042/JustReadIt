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


# ==========================================================================
# aggregate_by_text
# ==========================================================================

from src.hook.hook_code import HookCandidate, aggregate_by_text
from src.hook.hook_code import (
    StructGroup,
    TextGroup,
    build_struct_groups,
    build_text_groups,
    compute_fragment_texts,
    compute_redundant_hook_vas,
)


class TestAggregateByText:

    @staticmethod
    def _cand(text: str, score: float, hits: int = 1, rva: int = 0,
              pattern: str = "r0") -> HookCandidate:
        return HookCandidate(
            module="game.exe", rva=rva, access_pattern=pattern,
            encoding="utf16", text=text, hit_count=hits, score=score,
        )

    def test_empty_input(self) -> None:
        assert aggregate_by_text([]) == []

    def test_single_candidate(self) -> None:
        c = self._cand("hello", 10.0)
        result = aggregate_by_text([c])
        assert len(result) == 1
        rep, members = result[0]
        assert rep.text == "hello"
        assert len(members) == 1

    def test_groups_by_text(self) -> None:
        c1 = self._cand("AAA", 10.0, hits=1, rva=0x100, pattern="r0")
        c2 = self._cand("AAA", 20.0, hits=3, rva=0x200, pattern="r1")
        c3 = self._cand("BBB", 15.0, hits=2, rva=0x300, pattern="r2")
        result = aggregate_by_text([c1, c2, c3])
        assert len(result) == 2
        # Sorted by score descending: c2's group (score=20) then c3 (score=15)
        rep_a, mem_a = result[0]
        rep_b, mem_b = result[1]
        assert rep_a.text == "AAA"
        assert rep_a.score == 20.0
        assert rep_a.hit_count == 4  # 1 + 3
        assert len(mem_a) == 2
        assert rep_b.text == "BBB"
        assert len(mem_b) == 1

    def test_representative_is_highest_score(self) -> None:
        c1 = self._cand("X", 5.0, rva=0x10)
        c2 = self._cand("X", 50.0, rva=0x20)
        c3 = self._cand("X", 25.0, rva=0x30)
        [(rep, members)] = aggregate_by_text([c1, c2, c3])
        assert rep.rva == 0x20   # highest score → representative
        assert rep.hit_count == 3  # sum of all

    def test_does_not_mutate_originals(self) -> None:
        c1 = self._cand("T", 10.0, hits=2)
        c2 = self._cand("T", 20.0, hits=5)
        aggregate_by_text([c1, c2])
        assert c1.hit_count == 2
        assert c2.hit_count == 5


# ==========================================================================
# build_struct_groups
# ==========================================================================


class TestBuildStructGroups:

    @staticmethod
    def _cand(
        text: str = "テスト",
        score: float = 80.0,
        hits: int = 1,
        rva: int = 0x100,
        pattern: str = "r0",
        hook_va: int = 0,
        str_ptr: int = 0,
        seq: int = 0,
    ) -> HookCandidate:
        return HookCandidate(
            module="game.exe", rva=rva, access_pattern=pattern,
            encoding="utf16", text=text, hit_count=hits, score=score,
            hook_va=hook_va, str_ptr=str_ptr, first_seen_seq=seq,
        )

    def test_empty_input(self) -> None:
        assert build_struct_groups([]) == []

    def test_single_candidate(self) -> None:
        c = self._cand(str_ptr=0x1000, seq=0)
        groups = build_struct_groups([c])
        assert len(groups) == 1
        assert groups[0].leader is c
        assert groups[0].members == [c]

    def test_nearby_str_ptrs_grouped(self) -> None:
        c1 = self._cand(str_ptr=0x1000, seq=0, rva=0x100, hook_va=0xA)
        c2 = self._cand(str_ptr=0x1080, seq=1, rva=0x200, hook_va=0xB)
        groups = build_struct_groups([c1, c2], tolerance=256)
        assert len(groups) == 1
        assert groups[0].leader is c1  # earlier first_seen_seq
        assert len(groups[0].members) == 2

    def test_distant_str_ptrs_separate(self) -> None:
        c1 = self._cand(str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(str_ptr=0x2000, seq=1, rva=0x200)
        groups = build_struct_groups([c1, c2], tolerance=256)
        assert len(groups) == 2

    def test_leader_is_earliest_seq(self) -> None:
        # c2 has smaller first_seen_seq → should be leader
        c1 = self._cand(str_ptr=0x1000, seq=5, rva=0x100)
        c2 = self._cand(str_ptr=0x1010, seq=2, rva=0x200)
        groups = build_struct_groups([c1, c2])
        assert groups[0].leader is c2

    def test_zero_str_ptr_separate_groups(self) -> None:
        c1 = self._cand(str_ptr=0, seq=0, rva=0x100)
        c2 = self._cand(str_ptr=0, seq=1, rva=0x200)
        groups = build_struct_groups([c1, c2])
        assert len(groups) == 2  # each in own singleton group

    def test_groups_sorted_by_leader_seq(self) -> None:
        c1 = self._cand(str_ptr=0x5000, seq=10, rva=0x100)
        c2 = self._cand(str_ptr=0x1000, seq=3, rva=0x200)
        groups = build_struct_groups([c1, c2])
        assert groups[0].leader.first_seen_seq == 3
        assert groups[1].leader.first_seen_seq == 10

    def test_total_hits_property(self) -> None:
        c1 = self._cand(str_ptr=0x1000, seq=0, hits=5, rva=0x100)
        c2 = self._cand(str_ptr=0x1010, seq=1, hits=3, rva=0x200)
        [sg] = build_struct_groups([c1, c2])
        assert sg.total_hits == 8

    def test_hook_vas_property(self) -> None:
        c1 = self._cand(str_ptr=0x1000, seq=0, hook_va=0xAAA, rva=0x100)
        c2 = self._cand(str_ptr=0x1010, seq=1, hook_va=0xBBB, rva=0x200)
        c3 = self._cand(str_ptr=0x1020, seq=2, hook_va=0xAAA, rva=0x100, pattern="r1")
        [sg] = build_struct_groups([c1, c2, c3])
        assert sg.hook_vas == {0xAAA, 0xBBB}


# ==========================================================================
# build_text_groups
# ==========================================================================


class TestBuildTextGroups:

    @staticmethod
    def _cand(
        text: str = "テスト",
        score: float = 80.0,
        hits: int = 1,
        rva: int = 0x100,
        pattern: str = "r0",
        hook_va: int = 0,
        str_ptr: int = 0,
        seq: int = 0,
    ) -> HookCandidate:
        return HookCandidate(
            module="game.exe", rva=rva, access_pattern=pattern,
            encoding="utf16", text=text, hit_count=hits, score=score,
            hook_va=hook_va, str_ptr=str_ptr, first_seen_seq=seq,
        )

    def test_empty_input(self) -> None:
        assert build_text_groups([]) == []

    def test_single_struct_group(self) -> None:
        c = self._cand(text="hello", str_ptr=0x1000, seq=0)
        sgs = build_struct_groups([c])
        tgs = build_text_groups(sgs)
        assert len(tgs) == 1
        assert tgs[0].text == "hello"
        assert tgs[0].leader is c

    def test_same_text_merged(self) -> None:
        # Two struct groups (far apart str_ptr) with same text
        c1 = self._cand(text="same", str_ptr=0x1000, seq=0, rva=0x100, hook_va=0xA)
        c2 = self._cand(text="same", str_ptr=0x9000, seq=5, rva=0x200, hook_va=0xB)
        sgs = build_struct_groups([c1, c2])
        tgs = build_text_groups(sgs)
        assert len(tgs) == 1
        assert tgs[0].text == "same"
        assert len(tgs[0].structs) == 2
        assert tgs[0].leader is c1  # earlier first_seen_seq

    def test_different_texts_separate(self) -> None:
        c1 = self._cand(text="AAA", str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(text="BBB", str_ptr=0x9000, seq=1, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        tgs = build_text_groups(sgs)
        assert len(tgs) == 2

    def test_sorted_by_score_descending(self) -> None:
        c1 = self._cand(text="low", score=50.0, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(text="high", score=90.0, str_ptr=0x9000, seq=1, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        tgs = build_text_groups(sgs)
        assert tgs[0].text == "high"
        assert tgs[1].text == "low"

    def test_score_uses_max_across_structs(self) -> None:
        # Two struct groups with same text, different scores
        c1 = self._cand(text="X", score=50.0, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(text="X", score=90.0, str_ptr=0x9000, seq=5, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        [tg] = build_text_groups(sgs)
        assert tg.score == 90.0

    def test_total_hooks_across_structs(self) -> None:
        c1 = self._cand(text="T", str_ptr=0x1000, seq=0, hook_va=0xA, rva=0x100)
        c2 = self._cand(text="T", str_ptr=0x1010, seq=1, hook_va=0xB, rva=0x200)
        c3 = self._cand(text="T", str_ptr=0x9000, seq=2, hook_va=0xC, rva=0x300)
        sgs = build_struct_groups([c1, c2, c3])
        [tg] = build_text_groups(sgs)
        assert tg.total_hooks == 3

    def test_priority_struct_has_earliest_leader(self) -> None:
        c1 = self._cand(text="X", str_ptr=0x9000, seq=10, rva=0x100, hook_va=0xA)
        c2 = self._cand(text="X", str_ptr=0x1000, seq=2, rva=0x200, hook_va=0xB)
        sgs = build_struct_groups([c1, c2])
        [tg] = build_text_groups(sgs)
        assert tg.priority_struct.leader.first_seen_seq == 2
        assert tg.leader is c2


# ==========================================================================
# compute_redundant_hook_vas
# ==========================================================================


class TestComputeRedundantHookVas:

    @staticmethod
    def _cand(
        text: str = "テスト",
        score: float = 80.0,
        rva: int = 0x100,
        pattern: str = "r0",
        hook_va: int = 0,
        str_ptr: int = 0,
        seq: int = 0,
    ) -> HookCandidate:
        return HookCandidate(
            module="game.exe", rva=rva, access_pattern=pattern,
            encoding="utf16", text=text, hit_count=1, score=score,
            hook_va=hook_va, str_ptr=str_ptr, first_seen_seq=seq,
        )

    def test_empty_input(self) -> None:
        assert compute_redundant_hook_vas([]) == set()

    def test_single_member_no_redundancy(self) -> None:
        c = self._cand(hook_va=0xA, str_ptr=0x1000, seq=0)
        sgs = build_struct_groups([c])
        assert compute_redundant_hook_vas(sgs) == set()

    def test_leader_kept_others_redundant(self) -> None:
        c1 = self._cand(hook_va=0xA, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(hook_va=0xB, str_ptr=0x1010, seq=5, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        redundant = compute_redundant_hook_vas(sgs)
        assert redundant == {0xB}

    def test_confirmed_excluded(self) -> None:
        c1 = self._cand(hook_va=0xA, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(hook_va=0xB, str_ptr=0x1010, seq=5, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        redundant = compute_redundant_hook_vas(sgs, confirmed_vas={0xB})
        assert redundant == set()

    def test_multi_group_leader_in_one(self) -> None:
        # hook_va 0xB is leader in group 2 but non-leader in group 1 →
        # should NOT be marked redundant (it leads at least one group).
        c1 = self._cand(hook_va=0xA, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(hook_va=0xB, str_ptr=0x1010, seq=5, rva=0x200)
        c3 = self._cand(hook_va=0xB, str_ptr=0x9000, seq=1, rva=0x200, pattern="r1")
        sgs = build_struct_groups([c1, c2, c3])
        redundant = compute_redundant_hook_vas(sgs)
        # 0xB is leader of the group at 0x9000 → not redundant
        assert 0xB not in redundant

    def test_all_different_groups_no_redundancy(self) -> None:
        c1 = self._cand(hook_va=0xA, str_ptr=0x1000, seq=0, rva=0x100)
        c2 = self._cand(hook_va=0xB, str_ptr=0x9000, seq=1, rva=0x200)
        sgs = build_struct_groups([c1, c2])
        assert compute_redundant_hook_vas(sgs) == set()


# ==========================================================================
# compute_fragment_texts
# ==========================================================================


class TestComputeFragmentTexts:

    @staticmethod
    def _cand(text: str, score: float = 80.0) -> HookCandidate:
        return HookCandidate(
            module="game.exe", rva=0x100, access_pattern="r0",
            encoding="utf16", text=text, hit_count=1, score=score,
            hook_va=0xA000, str_ptr=0x5000, first_seen_seq=0,
        )

    def test_empty_returns_empty(self) -> None:
        assert compute_fragment_texts([]) == set()

    def test_single_candidate_returns_empty(self) -> None:
        assert compute_fragment_texts([self._cand("hello")]) == set()

    def test_no_concatenation_returns_empty(self) -> None:
        """Unrelated texts produce no fragments."""
        cands = [self._cand("ABC"), self._cand("XYZ")]
        assert compute_fragment_texts(cands) == set()

    def test_simple_two_piece_concat(self) -> None:
        """A = B + C  →  hide B and C."""
        cands = [
            self._cand("こんにちは世界"),
            self._cand("こんにちは"),
            self._cand("世界"),
        ]
        frags = compute_fragment_texts(cands)
        assert frags == {"こんにちは", "世界"}

    def test_three_piece_concat(self) -> None:
        """A = B + C + D  →  hide B, C, D."""
        cands = [
            self._cand("ABCDEF"),
            self._cand("AB"),
            self._cand("CD"),
            self._cand("EF"),
        ]
        frags = compute_fragment_texts(cands)
        assert frags == {"AB", "CD", "EF"}

    def test_composite_not_in_fragments(self) -> None:
        """The long text itself must NOT appear in fragments."""
        cands = [
            self._cand("ABCD"),
            self._cand("AB"),
            self._cand("CD"),
        ]
        frags = compute_fragment_texts(cands)
        assert "ABCD" not in frags

    def test_partial_overlap_no_match(self) -> None:
        """Pieces that don't exactly tile the target produce no fragments."""
        cands = [
            self._cand("ABCDE"),
            self._cand("ABC"),
            self._cand("DE_"),  # trailing underscore — doesn't match
        ]
        assert compute_fragment_texts(cands) == set()

    def test_repeated_piece(self) -> None:
        """Same piece used twice: ABAB = AB + AB."""
        cands = [
            self._cand("ABAB"),
            self._cand("AB"),
        ]
        frags = compute_fragment_texts(cands)
        assert frags == {"AB"}

    def test_standalone_text_not_hidden_if_no_composite(self) -> None:
        """If no longer text exists that concatenates these, nothing is hidden."""
        cands = [self._cand("AB"), self._cand("CD")]
        assert compute_fragment_texts(cands) == set()

    def test_transitive_fragments(self) -> None:
        """ABCDEF = ABCD + EF;  ABCD = AB + CD.

        AB and CD are fragments of ABCD; ABCD and EF are fragments of ABCDEF.
        All four should appear in the result.
        """
        cands = [
            self._cand("ABCDEF"),
            self._cand("ABCD"),
            self._cand("EF"),
            self._cand("AB"),
            self._cand("CD"),
        ]
        frags = compute_fragment_texts(cands)
        assert frags == {"ABCD", "EF", "AB", "CD"}

