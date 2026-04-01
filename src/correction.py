"""Cross-match between Windows OCR text and process memory scan results.

Uses Levenshtein distance (via ``rapidfuzz``) to find the best-matching
memory-resident string for each OCR result, providing cleaner text for
downstream translation.

When no memory match exceeds the similarity threshold, the caller should
fall back to the original OCR text.

Usage::

    from src.memory import MemoryScanner, pick_needles
    from src.correction import best_match

    ocr_text = "テスト文字列"
    with MemoryScanner(pid=target.pid) as ms:
        for needle in pick_needles(ocr_text):
            results = ms.scan(needle)
            if results:
                break

    clean = best_match(ocr_text, [r.text for r in results])
    final = clean if clean is not None else ocr_text
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from rapidfuzz import fuzz

_DEFAULT_THRESHOLD: float = 60.0
_PARTIAL_THRESHOLD: float = 75.0

# ---------------------------------------------------------------------------
# Unicode normalisation — scoring only
# ---------------------------------------------------------------------------

# OCR and VN engines use different code-points for visually identical glyphs.
_NORM_TABLE = str.maketrans({
    "\u2025": "\u2026",  # ‥ TWO DOT LEADER  → … HORIZONTAL ELLIPSIS
    "\u22ef": "\u2026",  # ⋯ MIDLINE HORIZ.  → … HORIZONTAL ELLIPSIS
})


def _normalize(text: str) -> str:
    """Normalise Unicode variants and strip VN script noise for scoring.

    Applied to **both** sides before computing fuzz ratios so that
    cosmetic differences (different ellipsis code-points, Light.VN
    dialog quote markers, wait commands) do not drag scores down.
    """
    # NFKC first: fullwidth → ASCII (！？ → !?), but also decomposes
    # … (U+2026) → ... and ‥ (U+2025) → ..  — fixed in the next steps.
    text = unicodedata.normalize("NFKC", text)
    # _NORM_TABLE handles ⋯ (U+22EF) which NFKC does not decompose.
    text = text.translate(_NORM_TABLE)
    # Re-collapse 2+ dots (NFKC-decomposed ellipses) back to single ….
    text = re.sub(r"\.{2,}", "\u2026", text)
    # Collapse consecutive … into one.
    text = re.sub("\u2026{2,}", "\u2026", text)
    # Collapse ・ (U+30FB, katakana middle-dot) sequences — OCR often
    # renders the VN ellipsis glyph as ・・ instead of …… or ‥‥.
    text = re.sub("\u30FB{2,}", "\u2026", text)
    # Strip leading " \u201c \u201d (Light.VN dialog quote markers, not rendered).
    text = re.sub(r'^["\u201c\u201d]', "", text, flags=re.MULTILINE)
    # Strip literal \w \n etc. (Light.VN wait/control commands).
    text = re.sub(r"\\[a-z]", "", text)
    # Strip Light.VN {{template}} variables (e.g. {{主人公}}) — replaced
    # by runtime values in the rendered text that OCR captures.
    text = re.sub(r"\{\{[^}\n]*\}\}", "", text)
    # Unwrap ~【name】 character name tags — keep the inner name so it
    # can still match the rendered character name visible to OCR.
    text = re.sub(r"^~【([^】\n]+)】$", r"\1", text, flags=re.MULTILINE)
    # Strip VN engine command lines (e.g. ~ジャンプ, ~暗転解除).  Lines
    # starting with ~ that are NOT character name tags are commands.
    text = re.sub(r"^~(?!【).+$", "", text, flags=re.MULTILINE)
    # Strip spaces that Windows OCR inserts between punctuation marks,
    # e.g. "! ?" → "!?" or "( )" → "()".  Japanese text has no word spaces
    # so removing isolated spaces between non-alphanumeric chars is safe.
    text = re.sub(r'(?<=[^\w\s]) (?=[^\w\s])', '', text)
    return text


# ---------------------------------------------------------------------------
# Line-window matching
# ---------------------------------------------------------------------------


def _best_line_window(
    ocr_norm: str,
    candidate: str,
) -> tuple[str, float] | None:
    """Find the contiguous line window in *candidate* best matching OCR text.

    Splits *candidate* by newlines and tries all contiguous windows of
    ``max(1, ocr_lines − 2)`` to ``min(total, ocr_lines + 3)`` lines.
    Each window is normalised before scoring against *ocr_norm*.

    Returns ``(original_window_text, score)`` or ``None``.
    """
    cand_lines = candidate.replace("\r\n", "\n").split("\n")
    if len(cand_lines) <= 1:
        return None  # single-line candidates handled by full-text ratio

    ocr_line_count = len(ocr_norm.split("\n"))

    best_score = 0.0
    best_text: str | None = None

    min_win = max(1, ocr_line_count - 1)
    max_win = min(len(cand_lines), ocr_line_count + 3)

    for win_size in range(min_win, max_win + 1):
        for start in range(len(cand_lines) - win_size + 1):
            window_lines = cand_lines[start : start + win_size]
            window_text = "\n".join(window_lines)
            window_norm = _normalize(window_text)

            if not window_norm.strip():
                continue

            score = fuzz.ratio(ocr_norm, window_norm)
            if score > best_score:
                best_score = score
                best_text = window_text

    if best_text is not None:
        return best_text, best_score
    return None


@dataclass(frozen=True)
class MatchResult:
    """Levenshtein matching result with provenance details."""

    text: str
    score: float
    phase: str
    threshold: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def best_match_with_details(
    ocr_text: str,
    candidates: Sequence[str],
    threshold: float = _DEFAULT_THRESHOLD,
) -> MatchResult | None:
    """Return the best-matching text segment with score/phase metadata.

    Three-phase matching:

    1. **Full-text ratio** — fastest; works when OCR and memory text
       are roughly the same length and content.
    2. **Line-window matching** — splits each candidate into lines and
       finds the contiguous window most similar to the OCR text.
       Naturally excludes engine command lines and character-name tags.
    3. **Partial ratio fallback** — catches remaining substring
       relationships (e.g. single dialog line vs. multi-line OCR).

    Both sides are unicode-normalised before scoring (ellipsis variants,
    VN script markers) so that cosmetic differences don't drag scores
    down.

    Returns ``None`` when no strategy reaches its threshold.
    """
    if not candidates or not ocr_text:
        return None

    ocr_norm = _normalize(ocr_text)

    accepted: list[MatchResult] = []

    # Phase 1: full-text ratio with normalisation.
    best_full_score = 0.0
    best_full_text: str | None = None
    for candidate in candidates:
        if not candidate:
            continue
        cand_norm = _normalize(candidate)
        score = fuzz.ratio(ocr_norm, cand_norm)
        if score > best_full_score:
            best_full_score = score
            best_full_text = candidate

    if best_full_text is not None and best_full_score >= threshold:
        accepted.append(MatchResult(
            text=best_full_text,
            score=best_full_score,
            phase="full",
            threshold=threshold,
        ))

    # Phase 2: line-window matching — best contiguous segment.
    best_line_score = 0.0
    best_line_text: str | None = None
    for candidate in candidates:
        if not candidate:
            continue
        result = _best_line_window(ocr_norm, candidate)
        if result is None:
            continue
        window_text, window_score = result
        if window_score > best_line_score:
            best_line_score = window_score
            best_line_text = window_text

    if best_line_text is not None and best_line_score >= threshold:
        accepted.append(MatchResult(
            text=best_line_text,
            score=best_line_score,
            phase="line-window",
            threshold=threshold,
        ))

    # Phase 3: partial ratio fallback for substring relationships.
    best_partial_score = 0.0
    best_partial_text: str | None = None
    for candidate in candidates:
        if not candidate:
            continue
        cand_norm = _normalize(candidate)
        score = fuzz.partial_ratio(ocr_norm, cand_norm)
        if score > best_partial_score:
            best_partial_score = score
            best_partial_text = candidate

    if best_partial_text is not None and best_partial_score >= _PARTIAL_THRESHOLD:
        # Refine multi-line partial winners to the best matching window so
        # that a short single-line candidate cannot beat a richer multi-line
        # candidate just because partial_ratio ignores length differences.
        partial_output = best_partial_text
        # Use fuzz.ratio (not partial_ratio) as the comparable score so
        # that the final cross-phase max() is an apples-to-apples
        # comparison.  partial_ratio ignores length differences and
        # inflates scores relative to fuzz.ratio used in Phases 1 & 2.
        partial_comparable_score = best_partial_score
        win_result = _best_line_window(ocr_norm, best_partial_text)
        if win_result is not None:
            partial_output = win_result[0]
            partial_comparable_score = win_result[1]
        else:
            # Single-line or very short candidate — fall back to full-text
            # fuzz.ratio for a fair comparison.
            partial_comparable_score = fuzz.ratio(
                ocr_norm, _normalize(best_partial_text)
            )
        accepted.append(MatchResult(
            text=partial_output,
            score=partial_comparable_score,
            phase="partial",
            threshold=_PARTIAL_THRESHOLD,
        ))

    if not accepted:
        return None

    # Prefer the highest score; on tie, prefer line-window over full over partial.
    phase_priority = {"line-window": 3, "full": 2, "partial": 1}
    return max(accepted, key=lambda r: (r.score, phase_priority.get(r.phase, 0)))


def best_match(
    ocr_text: str,
    candidates: Sequence[str],
    threshold: float = _DEFAULT_THRESHOLD,
) -> str | None:
    """Compatibility wrapper returning only matched text."""
    result = best_match_with_details(ocr_text, candidates, threshold)
    return result.text if result is not None else None
