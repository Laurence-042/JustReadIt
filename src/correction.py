# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cross-match between Windows OCR text and process memory scan results.

Uses needle-anchored alignment to find and extract the best-matching
memory-resident string segment for each OCR result, providing cleaner
text for downstream translation.

Algorithm overview:

1. Use the needle (a short CJK substring known to appear in both OCR and
   memory) as an anchor. Locate the needle in each candidate to compute a
   rough start/end boundary that aligns the memory string with the OCR text.
2. Score the aligned segment against the OCR text, treating ``{{template}}``
   variables as wildcards.
3. Pick the highest-scoring candidate/occurrence.
4. Refine the left boundary: walk left from the rough start, including
   symbols/punctuation until a ``clean text`` zone begins (no nearby symbols)
   or a terminator/opening-bracket is reached. This captures ``\u2026\u2026``
   or ``\u3010A\u2026\u2026`` that OCR drops while stopping at garbled bytes.
5. Refine the right boundary: walk right from the rough end, including
   everything (text and symbols) until a terminator or closing bracket is
   reached.

When no candidate reaches the similarity threshold, the caller should fall
back to the original OCR text.

Usage::

    from src.memory import MemoryScanner, pick_needles
    from src.correction import best_match

    ocr_text = "\u30c6\u30b9\u30c8\u6587\u5b57\u5217"
    with MemoryScanner(pid=target.pid) as ms:
        for needle in pick_needles(ocr_text):
            results = ms.scan(needle)
            if results:
                break

    clean = best_match(ocr_text, [r.text for r in results], needle)
    final = clean if clean is not None else ocr_text
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
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
# Character classification helpers
# ---------------------------------------------------------------------------


def _is_symbol_char(ch: str) -> bool:
    """Return ``True`` for punctuation and symbol Unicode characters.

    Returns ``False`` for CJK/Latin letters, digits, spaces, and controls.
    Categories covered: ``P*`` (punctuation) and ``S*`` (symbol).
    """
    cat = unicodedata.category(ch)
    return cat[0] in ("P", "S")


# Characters that terminate boundary walks unconditionally.
_TERMINATORS: frozenset[str] = frozenset('\n\r\t|')

# Opening delimiters: include and stop when walking left.
_LEFT_OPEN_SET: frozenset[str] = frozenset(
    '【（「『《〈〔〖[(\'\"\u201c\u201e\u3010\uff08\u300c\u300a\u300e\u3018'
)

# Closing delimiters: include and stop when walking right; stop (don't include) when walking left.
_CLOSE_SET: frozenset[str] = frozenset(
    '】）」』》〉〕〗])\'\"\u201d\u2019\u3011\uff09\u300d\u300b\u300f\u3019'
)


# ---------------------------------------------------------------------------
# Needle search
# ---------------------------------------------------------------------------

_FUZZY_NEEDLE_GATE = 70  # minimum fuzz.ratio for a fuzzy needle match


def _find_all_needle_positions(text: str, needle: str) -> list[int]:
    """Return all start positions of *needle* in *text* (exact match)."""
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _find_needle_pos(text: str, needle: str) -> int | None:
    """Find *needle* in *text*: exact first, then fuzzy sliding-window.

    Returns the start index of the best match, or ``None`` if no match
    exceeds ``_FUZZY_NEEDLE_GATE``.
    """
    idx = text.find(needle)
    if idx >= 0:
        return idx

    n = len(needle)
    if n == 0 or len(text) < n:
        return None

    best_score = 0.0
    best_pos: int | None = None
    for i in range(len(text) - n + 1):
        window = text[i : i + n]
        score = fuzz.ratio(needle, window)
        if score > best_score:
            best_score = score
            best_pos = i

    if best_score >= _FUZZY_NEEDLE_GATE:
        return best_pos
    return None


# ---------------------------------------------------------------------------
# Boundary refinement
# ---------------------------------------------------------------------------


def _refine_left_boundary(
    text: str,
    rough_left: int,
    token_range: int = 2,
) -> int:
    """Walk *left* from *rough_left* to capture symbols OCR may have missed.

    Rules (evaluated for each character stepping leftward):

    * **Terminator** (``\\n``, ``\\r``, ``\\t``, ``|``, ``~``): stop, do not
      include.
    * **Closing bracket** (``\u3011``, ``)``\u3001 ``\uff09`` ...): stop, do not include —
      a closing bracket to the left means we have already passed that block.
    * **Opening bracket** (``\u3010``, ``(``, ``\uff08`` ...): include and stop —
      capture the start of the bracketed run.
    * **Symbol / punctuation** (``\u2026``, ``\uff1f``, ``\uff01``, ``\u3001``, ``\u3002`` ...): include
      and continue walking.
    * **Text character** (CJK, Latin letter, digit ...): include only if at
      least one symbol or opening-bracket exists within the next
      *token_range* positions further to the left; otherwise stop.
    """
    pos = rough_left
    while pos > 0:
        ch = text[pos - 1]

        # Hard terminators — stop without including.
        if ch in _TERMINATORS or ch == "~":
            break

        # Closing bracket on the left — we have overshot the block boundary.
        if ch in _CLOSE_SET:
            break

        # Opening bracket — include it and stop.
        if ch in _LEFT_OPEN_SET:
            pos -= 1
            break

        # Symbol / punctuation — include and keep walking.
        if _is_symbol_char(ch):
            pos -= 1
            continue

        # Text character: look further left within token_range positions.
        look_start = max(0, pos - 1 - token_range)
        has_nearby_symbol = any(
            _is_symbol_char(text[j]) or text[j] in _LEFT_OPEN_SET
            for j in range(look_start, pos - 1)
        )
        if has_nearby_symbol:
            pos -= 1  # text char is attached to a symbol run; keep going
        else:
            break  # clean text starts here; this is the real left edge

    return pos


def _refine_right_boundary(text: str, rough_right: int) -> int:
    """Walk *right* from *rough_right* to include trailing symbols OCR missed.

    Rules:

    * **Terminator** (``\\n``, ``\\r``, ``\\t``, ``|``, ``~``): stop, do not include.
    * **Closing bracket** (``\u3011``, ``)``\u3001 ``\uff09`` ...): include and stop.
    * **Anything else** (text or symbols): include and continue.
    """
    pos = rough_right
    while pos < len(text):
        ch = text[pos]

        if ch in _TERMINATORS or ch == "~":
            break

        pos += 1  # include the character

        if ch in _CLOSE_SET:
            break  # included the closing bracket; stop

    return pos


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _template_aware_score(ocr_norm: str, cand_raw_segment: str) -> float:
    """Score *cand_raw_segment* against *ocr_norm* with ``{{...}}`` as wildcards.

    When no template variables are present delegates to ``fuzz.ratio``.
    When templates exist, splits the segment at template boundaries and
    finds each fixed part in *ocr_norm* left-to-right; the score is the
    fraction of fixed characters successfully matched, scaled to 0–100.
    """
    templates = re.findall(r'\{\{[^}\n]*\}\}', cand_raw_segment)
    cand_norm = _normalize(cand_raw_segment)

    if not templates:
        return float(fuzz.ratio(ocr_norm, cand_norm))

    # Split raw segment by template vars, normalise each fixed part.
    parts = re.split(r'\{\{[^}\n]*\}\}', cand_raw_segment)
    fixed_segments = [_normalize(p) for p in parts]
    fixed_segments = [s for s in fixed_segments if s.strip()]

    if not fixed_segments:
        return 0.0

    total_chars = sum(len(s) for s in fixed_segments)
    if total_chars == 0:
        return 0.0

    matched_chars = 0.0
    search_pos = 0
    for seg in fixed_segments:
        idx = ocr_norm.find(seg, search_pos)
        if idx >= 0:
            matched_chars += len(seg)
            search_pos = idx + len(seg)
        else:
            # Fuzzy fallback: partial_ratio finds the best alignment of seg
            # anywhere inside the remaining OCR text (equivalent to the
            # sliding-window approach but using optimised C internals).
            # search_pos advances by len(seg) as a conservative estimate so
            # subsequent segments are not double-counted.
            ratio = fuzz.partial_ratio(seg, ocr_norm[search_pos:])
            if ratio >= _DEFAULT_THRESHOLD:
                matched_chars += len(seg) * (ratio / 100.0)
                search_pos += len(seg)

    return (matched_chars / total_chars) * 100.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """Needle-aligned matching result with score/provenance details."""

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
    needle: str,
    threshold: float = _DEFAULT_THRESHOLD,
) -> MatchResult | None:
    """Return the best needle-aligned memory segment with score metadata.

    For each candidate the needle is located (exact then fuzzy) to derive
    a rough start/end boundary that aligns the memory string with the OCR
    text.  The aligned segment is scored against normalised OCR using
    template-aware scoring.  After picking the winner, left and right
    boundaries are refined to capture symbols OCR may have dropped and to
    trim leading garbled bytes.

    Returns ``None`` when no candidate's aligned segment reaches *threshold*.
    """
    if not candidates or not ocr_text or not needle:
        return None

    ocr_norm = _normalize(ocr_text)

    # Locate needle in OCR text (original then NFKC-simplified fallback).
    ocr_needle_pos = _find_needle_pos(ocr_text, needle)
    if ocr_needle_pos is None:
        needle_nfkc = unicodedata.normalize("NFKC", needle)
        ocr_nfkc = unicodedata.normalize("NFKC", ocr_text)
        ocr_needle_pos = _find_needle_pos(ocr_nfkc, needle_nfkc)
    if ocr_needle_pos is None:
        ocr_needle_pos = 0  # defensive fallback

    # Collect accepted (candidate, rough_left, rough_right, score) tuples.
    accepted: list[tuple[str, int, int, float]] = []

    for candidate in candidates:
        if not candidate:
            continue

        # Find all occurrences of needle in candidate.
        positions = _find_all_needle_positions(candidate, needle)
        if not positions:
            pos = _find_needle_pos(candidate, needle)
            if pos is None:
                continue
            positions = [pos]

        # Evaluate each occurrence; keep the one giving the highest score.
        best_score = -1.0
        best_left = 0
        best_right = 0

        for cand_needle_pos in positions:
            offset = cand_needle_pos - ocr_needle_pos
            rough_left = max(0, offset)
            rough_right = min(len(candidate), offset + len(ocr_text))

            if rough_right <= rough_left:
                continue

            segment = candidate[rough_left:rough_right]
            score = _template_aware_score(ocr_norm, segment)

            if score > best_score:
                best_score = score
                best_left = rough_left
                best_right = rough_right

        if best_score >= threshold:
            accepted.append((candidate, best_left, best_right, best_score))

    if not accepted:
        return None

    # Winner = highest score; on tie prefer longer aligned segment (more context).
    winner_cand, winner_left, winner_right, winner_score = max(
        accepted,
        key=lambda x: (x[3], x[2] - x[1]),
    )

    # Refine boundaries.
    refined_left = _refine_left_boundary(winner_cand, winner_left)
    refined_right = _refine_right_boundary(winner_cand, winner_right)

    return MatchResult(
        text=winner_cand[refined_left:refined_right],
        score=winner_score,
        phase="aligned",
        threshold=threshold,
    )


def best_match(
    ocr_text: str,
    candidates: Sequence[str],
    needle: str,
    threshold: float = _DEFAULT_THRESHOLD,
) -> str | None:
    """Compatibility wrapper returning only matched text."""
    result = best_match_with_details(ocr_text, candidates, needle, threshold)
    return result.text if result is not None else None
