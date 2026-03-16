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

from typing import Sequence

from rapidfuzz import fuzz

_DEFAULT_THRESHOLD: float = 60.0


def best_match(
    ocr_text: str,
    candidates: Sequence[str],
    threshold: float = _DEFAULT_THRESHOLD,
) -> str | None:
    """Return the *candidate* most similar to *ocr_text*, or ``None``.

    Parameters
    ----------
    ocr_text:
        Text recognised by Windows OCR (may contain errors).
    candidates:
        Clean text strings extracted from process memory via
        :class:`~src.memory.MemoryScanner`.
    threshold:
        Minimum ``rapidfuzz.fuzz.ratio()`` score (0–100) to accept.
        Below this threshold ``None`` is returned, signalling the caller
        to fall back to the OCR text.

    Returns
    -------
    str | None
        The best-matching candidate, or ``None`` if no candidate meets
        the similarity threshold.
    """
    if not candidates or not ocr_text:
        return None

    best_score = 0.0
    best_text: str | None = None

    for candidate in candidates:
        if not candidate:
            continue
        score = fuzz.ratio(ocr_text, candidate)
        if score > best_score:
            best_score = score
            best_text = candidate

    return best_text if best_score >= threshold else None
