"""Cross-match between Windows OCR text and process memory scan results.

Uses Levenshtein distance (via ``rapidfuzz``) to find the best-matching
memory-resident string for each OCR result, providing cleaner text for
downstream translation.

When no memory match exceeds the similarity threshold, the caller should
fall back to the original OCR text.

Usage::

    from src.memory import MemoryScanner, pick_needle
    from src.correction import best_match

    ocr_text = "テスト文字列"
    with MemoryScanner(pid=target.pid) as ms:
        results = ms.scan(pick_needle(ocr_text))

    clean = best_match(ocr_text, [r.text for r in results])
    final = clean if clean is not None else ocr_text
"""
from __future__ import annotations

import re
from typing import Sequence

from rapidfuzz import fuzz

_DEFAULT_THRESHOLD: float = 60.0

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
    text = text.translate(_NORM_TABLE)
    # Collapse runs of … into one.
    text = re.sub("\u2026{2,}", "\u2026", text)
    # Strip leading " " " (Light.VN dialog quote markers, not rendered).
    text = re.sub(r'^["\u201c\u201d]', "", text, flags=re.MULTILINE)
    # Strip literal \w \n etc. (Light.VN wait/control commands).
    text = re.sub(r"\\[a-z]", "", text)
    return text


# ---------------------------------------------------------------------------
# Line-window matching
# ---------------------------------------------------------------------------


def _best_line_window(
    ocr_norm: str,
    candidate: str,
    threshold: float,
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

    min_win = max(1, ocr_line_count - 2)
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

    if best_score >= threshold and best_text is not None:
        return best_text, best_score
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def best_match(
    ocr_text: str,
    candidates: Sequence[str],
    threshold: float = _DEFAULT_THRESHOLD,
) -> str | None:
    """Return the best-matching text *segment* from *candidates*, or ``None``.

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

    Returns the matched *segment* (possibly a subset of lines from the
    original candidate), not necessarily the whole candidate string.
    """
    if not candidates or not ocr_text:
        return None

    ocr_norm = _normalize(ocr_text)

    best_score = 0.0
    best_text: str | None = None

    # Phase 1: full-text ratio with normalisation.
    for candidate in candidates:
        if not candidate:
            continue
        cand_norm = _normalize(candidate)
        score = fuzz.ratio(ocr_norm, cand_norm)
        if score > best_score:
            best_score = score
            best_text = candidate

    if best_score >= threshold:
        return best_text

    # Phase 2: line-window matching — best contiguous segment.
    for candidate in candidates:
        if not candidate:
            continue
        result = _best_line_window(ocr_norm, candidate, threshold)
        if result is not None:
            window_text, window_score = result
            if window_score > best_score:
                best_score = window_score
                best_text = window_text

    if best_score >= threshold:
        return best_text

    # Phase 3: partial ratio fallback for substring relationships.
    _PARTIAL_THRESHOLD = 75.0
    best_partial = 0.0
    best_partial_text: str | None = None

    for candidate in candidates:
        if not candidate:
            continue
        cand_norm = _normalize(candidate)
        score = fuzz.partial_ratio(ocr_norm, cand_norm)
        if score > best_partial:
            best_partial = score
            best_partial_text = candidate

    return best_partial_text if best_partial >= _PARTIAL_THRESHOLD else None
