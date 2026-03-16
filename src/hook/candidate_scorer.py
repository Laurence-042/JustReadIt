"""Candidate text scoring for hook-site selection.

Contains hard-reject filters, a configurable text blacklist, and per-language
scorer classes.  Call :func:`score_candidate` to obtain a quality score for a
raw text string captured by the hook engine.

Text blacklist
--------------
:data:`TEXT_BLACKLIST` is the single extensibility point for rejecting
engine-specific noise (script source lines, resource tags, etc.).  Each entry
is a compiled :class:`re.Pattern` — if **any** pattern matches, the candidate
is instantly discarded (score 0).  Patterns are tried in order; put the most
common ones first.

To add a new filter, append a ``re.compile(...)`` call to ``TEXT_BLACKLIST``.

To add support for a new language, subclass :class:`_CandidateScorer`,
override :meth:`~_CandidateScorer._language_bonus`, and register an instance
in :data:`_LANG_SCORERS` under the appropriate BCP-47 root tag.
"""
from __future__ import annotations

import math
import re


# ---------------------------------------------------------------------------
# Text blacklist — extensible pattern list for engine-specific noise
# ---------------------------------------------------------------------------
# Each entry is a compiled regex.  If ANY pattern matches against the
# candidate text (via ``re.search``), the text is instantly discarded.
#
# Guidelines for adding patterns:
#   - Use re.IGNORECASE only when casing truly varies.
#   - Prefer anchored patterns (^ / $) where possible to avoid
#     false positives against legitimate dialogue.
#   - Document what engine / noise source the pattern targets.
#   - Place high-frequency patterns first for early-exit performance.

TEXT_BLACKLIST: list[re.Pattern[str]] = [
    # ── VN scripting language source lines ─────────────────────────────
    # Light.VN / KiriKiri / similar engines embed script interpreter calls
    # where source lines leak through as hooked strings.  Common markers:
    #   @label  *tag  [command ...]  //comment  #directive  ;comment
    re.compile(r"^\s*[@*#;]"),                       # line starts with script marker
    re.compile(r"^\s*\[(?!「)"),                      # [...] command (but not 「 bracket dialogue)
    re.compile(r"^\s*//"),                            # C-style line comment
    re.compile(r"\b(?:if|else|elsif|endif|return|goto|gosub|call|jump)\b", re.IGNORECASE),
    re.compile(r"\S+\s*[!=<>]?=\s*(?:[\"'].+?[\"']|\d+)"),  # assignments / comparisons: var = "on", flag == "on", count != 0

    # ── Resource / asset references ────────────────────────────────────
    re.compile(r"\.(png|jpg|jpeg|bmp|ogg|wav|mp3|mp4|avi|scn|ks|csv|txt|lua|dat)\b",
               re.IGNORECASE),
    re.compile(r"[/\\]{2,}"),                         # double slashes / backslashes (paths)

    # ── Engine internal tags ───────────────────────────────────────────
    re.compile(r"<\s*/?\s*(?:br|ruby|color|size|b|i|font|img|a)\b",
               re.IGNORECASE),                        # HTML-like markup tags

    # ── Variable / format-string templates ─────────────────────────────
    re.compile(r"%[0-9]*[dsfx]"),                     # printf-style formatters
    re.compile(r"\$\{?\w+\}?"),                       # $var or ${var} references
]


def is_blacklisted(text: str) -> bool:
    """Return ``True`` if *text* matches any pattern in :data:`TEXT_BLACKLIST`."""
    for pat in TEXT_BLACKLIST:
        if pat.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

_PATH_RE           = re.compile(r"[A-Za-z]:[/\\]")
_PURE_ASCII_ID_RE  = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_FILE_EXT_RE       = re.compile(r"\.[a-z]{2,5}\b", re.IGNORECASE)
_HEX_BLOB_RE       = re.compile(r"^[0-9a-fA-F]{8,}$")
_QUOTED_DIALOGUE_RE = re.compile(
    r'^[\s\u3000]*[「『\u201c\u201d"].*[」』\u201c\u201d"][\s\u3000]*$', re.DOTALL
)

# Binary garbage indicators — ASCII letters mixed with CJK, PUA, invisible
# formatting chars.  Any match → instant rejection.
_ASCII_LETTER_IN_CJK_RE = re.compile(r"[\u3000-\u9FFF\uFF00-\uFFEF][A-Za-z]|[A-Za-z][\u3000-\u9FFF\uFF00-\uFFEF]")
_INVISIBLE_CHARS_RE     = re.compile(r"[\u200B-\u200F\u2060-\u206F\uFE00-\uFE0F\uFFF0-\uFFFF]")
_PUA_RE                 = re.compile(r"[\uE000-\uF8FF]")

# CJK Unified Ideographs Extension A (U+3400..U+4DBF) — extremely rare in
# modern Japanese/Chinese text.  High density is a strong binary-garbage signal.
_CJK_EXT_A_RANGE = (0x3400, 0x4DBF)

# ---------------------------------------------------------------------------
# Per-language candidate scorers
# ---------------------------------------------------------------------------
# Each scorer encapsulates hard-reject rules and language-specific bonuses.
# Register new languages in ``_LANG_SCORERS``; ``score_candidate`` dispatches
# to the appropriate instance based on the BCP-47 root tag.
# ---------------------------------------------------------------------------

def _shannon_entropy(text: str) -> float:
    """Character-level Shannon entropy in bits."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Unicode ranges used for cross-script rejection.
_HANGUL_RANGES: list[tuple[int, int]] = [
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x3130, 0x318F),   # Hangul Compatibility Jamo
    (0xA960, 0xA97F),   # Hangul Jamo Extended-A
    (0xAC00, 0xD7A3),   # Hangul Syllables
    (0xD7B0, 0xD7FF),   # Hangul Jamo Extended-B
]
_KANA_RANGES: list[tuple[int, int]] = [
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0x31F0, 0x31FF),   # Katakana Phonetic Extensions
    (0xFF65, 0xFF9F),   # Halfwidth Katakana
]


class _CandidateScorer:
    """Language-agnostic scorer used as the base class and default fallback.

    Subclass and override :meth:`_language_bonus` to add language-specific
    multipliers on top of the shared base logic.
    """

    # Codepoint ranges for scripts this language should *never* contain.
    # Any character in these ranges causes an immediate score-0 rejection.
    _reject_script_ranges: list[tuple[int, int]] = []

    # ── hard-reject helpers ────────────────────────────────────────────

    def _hard_reject(self, text: str) -> bool:
        """Return True if *text* should be immediately discarded."""
        # Text blacklist (engine scripting source, resource refs, etc.)
        if is_blacklisted(text):
            return True
        # Non-printable control characters (null residue, ESC sequences …)
        # Allow only common whitespace: \t (0x09), \n (0x0A), \r (0x0D).
        if any(ord(c) < 0x20 and c not in '\t\n\r' for c in text):
            return True
        # Binary / high-entropy garbage
        if _shannon_entropy(text) > 5.5:
            return True
        # File / resource paths
        if _PATH_RE.search(text):
            return True
        stripped = text.strip()
        # Pure ASCII identifiers (internal IDs, enum names …)
        if _PURE_ASCII_ID_RE.match(stripped):
            return True
        # File extensions with short text → likely a filename token
        if _FILE_EXT_RE.search(stripped) and len(stripped) < 60:
            return True
        # Hex blobs
        if _HEX_BLOB_RE.match(stripped):
            return True
        # Binary garbage: ASCII letters adjacent to CJK characters
        if _ASCII_LETTER_IN_CJK_RE.search(text):
            return True
        # Invisible Unicode formatting characters
        if _INVISIBLE_CHARS_RE.search(text):
            return True
        # Private Use Area characters
        if _PUA_RE.search(text):
            return True
        # CJK Extension A density: characters U+3400–U+4DBF are archaic /
        # variant forms almost never seen in modern game text.  A high ratio
        # signals binary data decoded as CJK (e.g. stack noise / misaligned
        # memory reads).  Require at least 3 to avoid single-char false pos.
        ext_a_count = sum(
            1 for c in text
            if _CJK_EXT_A_RANGE[0] <= ord(c) <= _CJK_EXT_A_RANGE[1]
        )
        if ext_a_count >= 3 and ext_a_count / max(len(text), 1) > 0.10:
            return True
        # Cross-script rejection: a string containing characters from a
        # script foreign to the active OCR language is almost certainly
        # a false positive (e.g. Hangul on the stack in a Japanese game).
        if self._reject_script_ranges:
            for ch in text:
                cp = ord(ch)
                if any(lo <= cp <= hi for lo, hi in self._reject_script_ranges):
                    return True
        return False

    # ── base CJK check + scoring ───────────────────────────────────────

    def _base_score(self, text: str) -> float:
        """Shared base score before any language bonuses.

        Returns 0.0 if the text contains no CJK characters at all.

        Length contribution is sub-linear (``sqrt(len) * 4``) so that short
        real dialogue is not drowned out by long binary-garbage strings that
        happen to pass the hard-reject filters.
        """
        cjk_chars = sum(
            1 for c in text
            if "\u3000" <= c <= "\u9FFF" or "\uFF00" <= c <= "\uFFEF"
        )
        if cjk_chars == 0:
            return 0.0
        # Sub-linear length: sqrt(n)*4 crosses raw length at n=16,
        # slightly boosting short text while compressing long strings.
        score = math.sqrt(len(text)) * 4
        # Reward high CJK density (dialogue vs. mixed noise)
        cjk_ratio = cjk_chars / max(len(text), 1)
        score *= 1.0 + cjk_ratio
        # Strong bonus for bracket- or quote-delimited dialogue
        if _QUOTED_DIALOGUE_RE.match(text):
            score *= 2.5
        return score

    # ── language bonus (overridden by subclasses) ──────────────────────

    def _language_bonus(self, text: str) -> float:  # noqa: ARG002
        """Return a multiplier ≥ 1.0 for language-specific dialogue signals.

        The default implementation returns 1.0 (no bonus).
        """
        return 1.0

    # ── public entry point ──────────────────────────────────────────────

    def __call__(self, text: str) -> float:
        """Return quality score ≥ 0.  Zero means 'discard'."""
        if not text or self._hard_reject(text):
            return 0.0
        base = self._base_score(text)
        if base <= 0.0:
            return 0.0
        return base * self._language_bonus(text)


class _JaCandidateScorer(_CandidateScorer):
    """Japanese-specific scorer.

    Adds dialogue-indicator bonuses on top of the shared base:

    * **Kana gate** — Japanese text virtually always contains hiragana or
      katakana (particles, verb endings, auxiliaries).  A string longer than
      8 characters with *zero* kana is almost certainly binary garbage that
      happened to decode into CJK codepoints.  Penalty: ×0.05.
    * **Particle bonus** — grammatical particles (は/の/に/も/を/と/が/で/
      へ/か/な/よ/ね/わ) appear in virtually every natural sentence but not
      in short menu labels.  3+ distinct particles → ×3.0; 1–2 → ×1.8.
    * **Sentence-end bonus** — 。？！ at the end of the string → ×1.3.
    * **Menu penalty** — very short (≤6 chars) all-CJK string with no
      particles → ×0.6 (likely a menu label, deprioritise).

    Cross-script: Hangul characters → immediate score 0.
    """

    _reject_script_ranges = _HANGUL_RANGES

    # Hiragana function particles that appear in almost all natural sentences
    # but are rare in menu labels / short UI strings.
    _PARTICLES: frozenset[str] = frozenset("はのにもをとがでへかなよねわ")
    _SENTENCE_END: frozenset[str] = frozenset("。？！")

    def _language_bonus(self, text: str) -> float:
        bonus = 1.0

        # Kana gate: real Japanese virtually always contains hiragana /
        # katakana.  Pure-kanji strings > 8 chars are a strong garbage signal.
        kana_count = sum(
            1 for c in text
            if '\u3040' <= c <= '\u30FF'     # Hiragana + Katakana
            or '\u31F0' <= c <= '\u31FF'     # Katakana Phonetic Extensions
            or '\uFF65' <= c <= '\uFF9F'     # Halfwidth Katakana
        )
        if kana_count == 0 and len(text.strip()) > 8:
            bonus *= 0.05  # near-zero — almost certainly not Japanese

        distinct_particles = sum(1 for p in self._PARTICLES if p in text)
        if distinct_particles >= 3:
            bonus *= 3.0   # strong dialogue signal
        elif distinct_particles >= 1:
            bonus *= 1.8   # mild dialogue signal

        # Sentence-final punctuation
        stripped = text.rstrip()
        if stripped and stripped[-1] in self._SENTENCE_END:
            bonus *= 1.3

        # Menu-label penalty: very short, no particles
        if distinct_particles == 0 and len(text.strip()) <= 6:
            bonus *= 0.6

        return bonus


class _ZhCandidateScorer(_CandidateScorer):
    """Chinese-specific scorer — rejects Hangul, base scoring only."""
    _reject_script_ranges = _HANGUL_RANGES


class _KoCandidateScorer(_CandidateScorer):
    """Korean-specific scorer — rejects kana, base scoring only."""
    _reject_script_ranges = _KANA_RANGES


# BCP-47 root tag → scorer instance.
_LANG_SCORERS: dict[str, _CandidateScorer] = {
    "ja": _JaCandidateScorer(),
    "zh": _ZhCandidateScorer(),
    "ko": _KoCandidateScorer(),
}
_DEFAULT_SCORER = _CandidateScorer()


def score_candidate(text: str, ocr_lang: str = "") -> float:
    """Return a quality score ≥ 0.  Score 0 means 'discard'.

    Dispatches to the appropriate :class:`_CandidateScorer` subclass based
    on *ocr_lang* (BCP-47 root tag, e.g. ``"ja"``).  Language-specific
    bonuses reward dialogue text over short menu labels; see the scorer
    subclasses for details.
    """
    root = ocr_lang.split("-")[0].lower() if ocr_lang else ""
    return _LANG_SCORERS.get(root, _DEFAULT_SCORER)(text)
