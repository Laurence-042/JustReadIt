# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cross-match between Windows OCR text and process memory scan results.

Uses **edlib semi-global alignment** to find and extract the best-matching
memory-resident string segment for each OCR result, providing cleaner
text for downstream translation.

Algorithm overview:

1. Light-normalise both OCR and candidate texts for alignment: strip VN
   engine noise (commands, templates, dialog-quote markers) and replace
   ``\\n`` with spaces so that structural differences do not prevent
   alignment.  A position map tracks where each normalised character
   came from in the original text.
2. Run ``edlib.align(ocr, candidate, mode="HW")`` — semi-global
   alignment finds the substring of the (normalised) candidate with
   minimal edit distance to the (normalised) OCR text.
3. Map the alignment's ``[i0, i1]`` back to original candidate positions
   via the position map.
4. Score the mapped segment against normalised OCR using template-aware
   scoring (``{{...}}`` treated as wildcards).
5. Pick the highest-scoring candidate.
6. Refine boundaries: walk outward from the mapped edges to capture
   symbols/brackets that OCR may have dropped (``……``, ``【``, ``」`` …).

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

import edlib
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
# Alignment normalisation (lightweight, with position map)
# ---------------------------------------------------------------------------

_NAME_TAG_RE = re.compile(r'^~【([^】\n]+)】$')


def _normalize_for_alignment(text: str) -> tuple[str, list[int]]:
    """Light normalisation for ``edlib`` alignment with position map.

    Unlike :func:`_normalize` this does **not** apply NFKC (which
    changes string lengths in hard-to-track ways).  ``edlib`` tolerates
    small character-level differences (fullwidth vs ASCII punctuation,
    different ellipsis code-points) as edit distance.

    Transformations (all maintain a ``pos_map`` from normalised index
    to original index):

    * ``\\n`` → space (lets alignment span across lines).
    * ``~【name】`` → just the name (VN character name tags).
    * ``~command…`` lines → skipped entirely.
    * ``{{template}}`` → skipped.
    * ``\\w``, ``\\n`` etc. (VN wait/control commands) → skipped.
    * ``\u201c``/``\u201d``/``"`` dialog-quote markers → skipped.
    * ``\\r``, ``\\t``, ``|`` → space.
    """
    lines = text.split('\n')
    chars: list[str] = []
    pos_map: list[int] = []
    offset = 0  # position in *text* where current line starts

    for line_idx, line in enumerate(lines):
        # Inter-line separator: the \n at (offset - 1).
        if line_idx > 0:
            chars.append(' ')
            pos_map.append(offset - 1)  # position of the \n

        # VN command line (~ジャンプ …) — skip entirely.
        if line.startswith('~') and not _NAME_TAG_RE.match(line):
            offset += len(line) + (1 if line_idx < len(lines) - 1 else 0)
            continue

        # Character name tag (~【name】) — keep only the name.
        m = _NAME_TAG_RE.match(line)
        if m:
            name_start = 2  # after ~【
            for j, ch in enumerate(m.group(1)):
                chars.append(ch)
                pos_map.append(offset + name_start + j)
            offset += len(line) + (1 if line_idx < len(lines) - 1 else 0)
            continue

        # Regular line: character-by-character.
        i = 0
        while i < len(line):
            ch = line[i]

            # Skip {{...}} templates.
            if line[i : i + 2] == '{{':
                end = line.find('}}', i + 2)
                if end >= 0:
                    i = end + 2
                    continue

            # Skip \\w \\n etc. (literal backslash + letter).
            if ch == '\\' and i + 1 < len(line) and line[i + 1].isalpha():
                i += 2
                continue

            # Replace control chars with space.
            if ch in '\r\t|':
                chars.append(' ')
                pos_map.append(offset + i)
                i += 1
                continue

            # Skip dialog-quote markers.
            if ch in '"\u201c\u201d':
                i += 1
                continue

            chars.append(ch)
            pos_map.append(offset + i)
            i += 1

        offset += len(line) + (1 if line_idx < len(lines) - 1 else 0)

    return ''.join(chars), pos_map


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
            # Special case: ~【name】 — include the ~ command prefix.
            if pos > 0 and text[pos - 1] == '~':
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
# Per-candidate alignment
# ---------------------------------------------------------------------------


def _align_candidate(
    ocr_align: str,
    ocr_norm: str,
    candidate: str,
    cand_align: str,
    cand_pos_map: list[int],
    max_k: int,
    threshold: float,
) -> tuple[int, int, float, bool] | None:
    """Align *candidate* against *ocr_align* and return ``(left, right, score, is_reversed)``.

    Two strategies are tried in order:

    **Normal** — ``edlib`` HW, OCR as query, candidate as target.
    Finds the substring of the candidate with minimal edit distance to
    the full OCR text.  Skipped when the length excess alone already
    exceeds *max_k* (guaranteed failure).

    **Reversed** — ``edlib`` HW, candidate as query, OCR as target.
    Used when OCR is longer than the candidate (captured UI chrome
    inflates OCR length).  Scored with ``fuzz.partial_ratio`` because
    no position map is available in this direction; the full candidate
    is returned as-is (boundaries refined by the caller).

    The 4th return value ``is_reversed`` lets the caller prefer normal-
    direction results when both directions have accepted candidates;
    the reversed path is a fallback for when OCR is heavily inflated.

    Returns ``None`` when neither strategy produces a score ≥ *threshold*.
    """
    length_excess = len(ocr_align) - len(cand_align)

    # ── Normal direction ─────────────────────────────────────────────
    if length_excess <= max_k:
        result = edlib.align(
            ocr_align, cand_align, mode="HW", task="locations", k=max_k,
        )
        if result["editDistance"] >= 0:
            best_score = -1.0
            best_left = 0
            best_right = 0
            for loc_start, loc_end_inclusive in result["locations"]:
                if loc_start >= len(cand_pos_map):
                    continue
                if loc_end_inclusive >= len(cand_pos_map):
                    loc_end_inclusive = len(cand_pos_map) - 1
                orig_left  = cand_pos_map[loc_start]
                orig_right = cand_pos_map[loc_end_inclusive] + 1
                score = _template_aware_score(ocr_norm, candidate[orig_left:orig_right])
                if score > best_score:
                    best_score = score
                    best_left  = orig_left
                    best_right = orig_right
            if best_score >= threshold:
                return best_left, best_right, best_score, False

    # ── Reversed direction (OCR longer than candidate) ───────────────
    if len(cand_align) < len(ocr_align):
        rev_k = max(1, len(cand_align) // 2)
        rev = edlib.align(
            cand_align, ocr_align, mode="HW", task="distance", k=rev_k,
        )
        if rev["editDistance"] >= 0:
            score = fuzz.partial_ratio(_normalize(candidate), ocr_norm)
            if score >= threshold:
                return 0, len(candidate), score, True

    return None


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
    """Return the best edlib-aligned memory segment with score metadata.

    Uses ``edlib`` semi-global alignment (``mode="HW"``) to find the
    substring of each candidate that best matches the OCR text.  Both
    sides are *light-normalised* first (VN noise stripped, ``\\n`` →
    space) so that structural differences do not block alignment.

    The aligned region is mapped back to the original candidate text,
    scored with :func:`_template_aware_score`, and the winner’s
    boundaries are refined to capture symbols OCR may have dropped.

    *needle* is used as a fast pre-filter: candidates that do not
    contain the needle (exact match) are skipped.

    Returns ``None`` when no candidate reaches *threshold*.
    """
    if not candidates or not ocr_text or not needle:
        return None

    ocr_norm = _normalize(ocr_text)
    ocr_align, _ = _normalize_for_alignment(ocr_text)

    if not ocr_align.strip():
        return None

    # Maximum edit distance: 2/3 of normalised OCR length.
    # Using 2/3 (not 1/2) because captured UI chrome can inflate OCR length
    # significantly beyond the clean dialog portion, raising the edit distance
    # well past 50% while still being a legitimate match.
    max_k = max(1, (len(ocr_align) * 2) // 3)

    # Collect accepted (candidate, orig_left, orig_right, score, is_reversed).
    accepted: list[tuple[str, int, int, float, bool]] = []

    for candidate in candidates:
        if not candidate:
            continue

        # Fast pre-filter: needle must appear in candidate.
        if needle not in candidate:
            continue

        cand_align, cand_pos_map = _normalize_for_alignment(candidate)
        if not cand_align.strip() or not cand_pos_map:
            continue

        match = _align_candidate(
            ocr_align, ocr_norm, candidate, cand_align, cand_pos_map,
            max_k, threshold,
        )
        if match is not None:
            accepted.append((candidate, *match))

    if not accepted:
        return None

    # Prefer normal-direction candidates over reversed-direction ones.
    # The reversed path uses partial_ratio (more lenient) and is only a
    # fallback for when OCR is inflated with UI chrome and no candidate
    # can match via normal HW alignment.
    normal = [x for x in accepted if not x[4]]
    pool = normal if normal else accepted

    # Winner = highest score; on tie prefer longer segment (more context).
    winner_cand, winner_left, winner_right, winner_score, _ = max(
        pool,
        key=lambda x: (x[3], x[2] - x[1]),
    )

    # Refine boundaries for symbols/brackets OCR may have dropped.
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
