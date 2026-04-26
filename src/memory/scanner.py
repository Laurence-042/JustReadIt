"""Process memory scanner — finds text strings via ReadProcessMemory.

Zero-intrusion alternative to DLL injection hooks.  Scans the target process's
committed readable memory regions for encoded text strings matching OCR output,
extracts complete null-terminated strings, and returns clean text for downstream
Levenshtein matching.

Typical workflow::

    from src.memory import MemoryScanner, pick_needle

    ocr_text = "テスト文字列"
    needle = pick_needle(ocr_text)

    with MemoryScanner(pid=target.pid) as ms:
        results = ms.scan(needle)
        for r in results:
            print(r.text, r.encoding, hex(r.address))

Performance
-----------
- Typical VN committed readable memory: 200–500 MB.
- ``bytes.find()`` in CPython (C-implemented Boyer-Moore-Horspool): ~200–400 ms
  for a full naive scan.  With hot-region caching: ~10–50 ms on subsequent scans.
- Optional ``mem_scan.dll`` (``memchr`` + ``memcmp``, CRT SIMD): ~30–80 ms full,
  ~5–15 ms cached.
- User-perceived latency budget (hover trigger): ~200 ms — well within budget.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

from . import _win32 as w32
from ._search import find_all_positions
from src.text_utils import normalize_text

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported encodings (tried in this order by default)
# ---------------------------------------------------------------------------

_DEFAULT_ENCODINGS: list[str] = ["utf-16-le", "utf-8", "shift-jis"]

# Maximum region size to read in a single call (skip huge mapped files).
_MAX_REGION_BYTES: int = 100 * 1024 * 1024  # 100 MB

# Maximum characters to extract forward/backward from a match position.
_MAX_EXTRACT_CHARS: int = 4096

# Minimum extracted string length to consider a quality hit.
_MIN_TEXT_LENGTH: int = 2

# Strings longer than this are refined to nearby lines.
# VN engines load entire script files as one null-terminated block;
# line-level refinement isolates the dialog paragraph for Levenshtein matching.
#
# Now serves as a *fallback* path: the multi-boundary extraction in
# ``_extract_utf16le`` / ``_extract_byte_delimited`` already trims most blocks
# at ``\n`` / closing-quote / opening-quote boundaries, keeping extracted
# text well under this threshold.  ``_refine_to_lines`` only triggers for
# unusually large blocks the byte-level scan failed to cut.
_LINE_REFINE_THRESHOLD: int = 600

# Per-side threshold used by the multi-boundary extraction.  When the
# strong (\0) boundary is more than this many *characters* away from the
# match position, soft boundaries (\n, quotes) are preferred.
#
# 200 chars ≈ a long-ish dialog paragraph.  Anything bigger is almost
# certainly a script blob that pulled in adjacent unrelated lines.
_SOFT_BOUNDARY_THRESHOLD_CHARS: int = 200

# Right-side soft boundaries: closing quotes, brackets and the newline.
# Encountering any of these to the right of the match site marks the
# end of the current logical text unit.  They are *excluded* from the
# extracted text (consistent with the existing ``\x00`` semantics).
_RIGHT_SOFT_BOUNDARY_CODEPOINTS: tuple[str, ...] = (
    "\n",
    "」",  # JP closing kagi-kakko
    "』",  # JP closing double kagi-kakko
    "”",  # right double quotation mark (U+201D)
    "’",  # right single quotation mark (U+2019)
    "）",  # fullwidth right paren
    "】",  # right tortoise shell bracket
    '"',
    "'",
)

# Left-side strong boundaries: opening quotes / brackets.  When found
# to the left of the match site (and the slice between is "clean"
# according to ``_is_noisy_line``) they take precedence over the weaker
# ``\n`` fallback.
_LEFT_STRONG_BOUNDARY_CODEPOINTS: tuple[str, ...] = (
    "「",
    "『",
    "“",
    "‘",
    "（",
    "【",
    '"',
    "'",
)

# Newline codepoint used as a left-side fallback boundary when no
# opening quote is found within the threshold window.
_LEFT_NEWLINE_CODEPOINTS: tuple[str, ...] = ("\n",)

# Cache of encoded boundary byte-patterns, keyed by (codepoint_tuple, encoding).
# Built lazily on first use.  Codepoints that fail to encode for the
# active encoding are silently skipped.
_BOUNDARY_PATTERN_CACHE: dict[tuple[tuple[str, ...], str], list[bytes]] = {}

# How long (seconds) to reuse a cached region list before re-enumerating.
_REGIONS_CACHE_TTL_S: float = 5.0


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanResult:
    """A text string found in the target process memory.

    Attributes
    ----------
    text:
        Full null-terminated string extracted at the match site.
    encoding:
        Encoding used to decode the string (``"utf-16-le"``, ``"utf-8"``,
        or ``"shift-jis"``).
    address:
        Virtual address of the matched needle within the target process.
    region_base:
        Base address of the containing memory region.
    """

    text: str
    encoding: str
    address: int
    region_base: int


# ---------------------------------------------------------------------------
# Needle picker
# ---------------------------------------------------------------------------

def _is_cjk(char: str) -> bool:
    """Return ``True`` if *char* is a CJK ideograph, hiragana, or katakana."""
    cp = ord(char)
    return (
        0x3040 <= cp <= 0x309F      # Hiragana
        or 0x30A0 <= cp <= 0x30FF   # Katakana
        or 0x4E00 <= cp <= 0x9FFF   # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF   # CJK Extension A
        or 0xF900 <= cp <= 0xFAFF   # CJK Compatibility Ideographs
    )


def pick_needles(
    ocr_text: str,
    *,
    min_length: int = 3,
    needle_length: int = 4,
    max_needles: int = 3,
) -> list[str]:
    """Extract multiple short CJK substrings from *ocr_text* for memory search.

    Returns up to *max_needles* substrings of *needle_length* characters from
    the longest contiguous CJK run.  Shorter needles are less likely to contain
    OCR errors, and trying several provides redundancy — if one needle has an
    error, the others may still produce hits.

    Needle priority: **centre** first (OCR is most accurate in the middle of a
    text region), then **start** and **end** of the run.

    Falls back to the first *needle_length* characters of *ocr_text* if no CJK
    run meets *min_length*.

    Parameters
    ----------
    ocr_text:
        Raw text from Windows OCR (may contain errors at boundaries).
    min_length:
        Minimum CJK run length to consider.  Shorter runs are ignored.
    needle_length:
        Length of each needle substring.
    max_needles:
        Maximum number of needles to return.
    """
    if not ocr_text:
        return []

    # Collect all contiguous CJK runs.
    runs: list[str] = []
    current: list[str] = []
    for ch in ocr_text:
        if _is_cjk(ch):
            current.append(ch)
        else:
            if current:
                runs.append("".join(current))
                current = []
    if current:
        runs.append("".join(current))

    # Filter by min_length, pick the longest.
    valid = [r for r in runs if len(r) >= min_length]
    if not valid:
        return [ocr_text[:needle_length]]

    best = max(valid, key=len)

    if len(best) <= needle_length:
        return [best]

    n = len(best)
    center_start = (n - needle_length) // 2

    # Run too short for two non-overlapping needles — return centre only.
    if n < needle_length * 2:
        return [best[center_start : center_start + needle_length]]

    # Generate up to max_needles: centre, start, end — deduplicated.
    positions = [center_start, 0, n - needle_length]
    seen: set[str] = set()
    result: list[str] = []
    for pos in positions:
        substr = best[pos : pos + needle_length]
        if substr not in seen:
            seen.add(substr)
            result.append(substr)
        if len(result) >= max_needles:
            break
    return result


# ---------------------------------------------------------------------------
# String extraction helpers
# ---------------------------------------------------------------------------

def _get_boundary_patterns(
    codepoints: tuple[str, ...],
    encoding: str,
) -> list[bytes]:
    """Return the byte-pattern list for *codepoints* under *encoding*.

    Cached per ``(codepoints, encoding)``.  Codepoints that are not
    representable in the encoding (e.g. fullwidth quotes in plain
    ASCII) are silently skipped — they simply won't participate in
    boundary matching for that encoding.
    """
    key = (codepoints, encoding)
    cached = _BOUNDARY_PATTERN_CACHE.get(key)
    if cached is not None:
        return cached

    codec = "cp932" if encoding == "shift-jis" else encoding
    patterns: list[bytes] = []
    for cp in codepoints:
        try:
            patterns.append(cp.encode(codec))
        except (UnicodeEncodeError, LookupError):
            continue
    _BOUNDARY_PATTERN_CACHE[key] = patterns
    return patterns


def _find_aligned_right_boundary(
    data: bytes,
    start: int,
    end: int,
    patterns: list[bytes],
    alignment: int,
) -> int | None:
    """Return offset of the earliest aligned pattern hit in ``[start, end)``.

    For UTF-16LE the *alignment* is 2 (each codepoint occupies 2 bytes),
    so a matching byte sequence at an odd offset is rejected.  For
    byte-encodings *alignment* is 1.
    """
    best: int | None = None
    for pat in patterns:
        if not pat:
            continue
        pos = start
        while pos < end:
            idx = data.find(pat, pos, end)
            if idx < 0:
                break
            if alignment == 1 or (idx - start) % alignment == 0:
                if best is None or idx < best:
                    best = idx
                break
            pos = idx + 1
    return best


def _find_aligned_left_boundary(
    data: bytes,
    start: int,
    end: int,
    patterns: list[bytes],
    alignment: int,
) -> tuple[int, int] | None:
    """Return ``(offset, pattern_length)`` of the latest aligned pattern hit.

    Returns the rightmost (i.e. closest to ``end``) aligned hit across
    all *patterns*, or ``None`` if no pattern matches in ``[start, end)``.
    """
    best: tuple[int, int] | None = None
    for pat in patterns:
        if not pat:
            continue
        pos = end
        while pos > start:
            idx = data.rfind(pat, start, pos)
            if idx < 0:
                break
            if alignment == 1 or (idx - start) % alignment == 0:
                if best is None or idx > best[0]:
                    best = (idx, len(pat))
                break
            pos = idx
    return best


def _byte_threshold(encoding: str) -> int:
    """Return the soft-boundary distance threshold in *bytes* for *encoding*.

    The threshold is conceptually a character count but is converted to
    bytes here because all boundary scanning operates on raw bytes.
    UTF-16LE uses 2 bytes/char.  UTF-8 / Shift-JIS are bounded above by
    3 bytes/char (CJK worst case for UTF-8); using this conservative
    upper bound means we engage the soft-boundary path slightly earlier
    for byte encodings, which is harmless.
    """
    if encoding == "utf-16-le":
        return _SOFT_BOUNDARY_THRESHOLD_CHARS * 2
    return _SOFT_BOUNDARY_THRESHOLD_CHARS * 3


def _refine_extract_window(
    data: bytes,
    start_null: int,
    end_strong: int,
    match_pos: int,
    encoding: str,
    alignment: int,
) -> tuple[int, int]:
    """Refine ``[start_null, end_strong]`` using soft / strong soft-boundaries.

    Implements the multi-boundary candidate strategy:

    * Right side — when the ``\\0`` boundary is far away, prefer the
      nearest of ``\\n`` / closing quote / closing bracket.
    * Left side — when the ``\\0`` boundary is far away, prefer an
      opening quote (only if the gap to ``match_pos`` is "clean") and
      otherwise the nearest preceding ``\\n``.

    See module docstring constants ``_RIGHT_SOFT_BOUNDARY_CODEPOINTS``
    and ``_LEFT_STRONG_BOUNDARY_CODEPOINTS`` for the codepoint sets.
    """
    threshold_bytes = _byte_threshold(encoding)
    codec = "cp932" if encoding == "shift-jis" else encoding

    # ----- Right boundary -----------------------------------------------
    end = end_strong
    if end_strong - match_pos > threshold_bytes:
        right_patterns = _get_boundary_patterns(
            _RIGHT_SOFT_BOUNDARY_CODEPOINTS, encoding,
        )
        end_soft = _find_aligned_right_boundary(
            data, match_pos, end_strong, right_patterns, alignment,
        )
        if end_soft is not None:
            end = min(end_strong, end_soft)

    # ----- Left boundary ------------------------------------------------
    start = start_null
    if match_pos - start_null > threshold_bytes:
        lower_bound = max(start_null, match_pos - threshold_bytes)
        # Re-align lower_bound to the same parity as match_pos for UTF-16LE
        # so that aligned-search ``(idx - start) % alignment == 0`` checks
        # produce results aligned with codepoint boundaries.
        if alignment > 1:
            offset = (match_pos - lower_bound) % alignment
            if offset != 0:
                lower_bound += offset

        chosen: int | None = None

        # Strong open-quote candidate.
        quote_patterns = _get_boundary_patterns(
            _LEFT_STRONG_BOUNDARY_CODEPOINTS, encoding,
        )
        q = _find_aligned_left_boundary(
            data, lower_bound, match_pos, quote_patterns, alignment,
        )
        if q is not None:
            q_pos, q_len = q
            candidate_start = q_pos + q_len
            try:
                candidate_text = data[candidate_start:match_pos].decode(codec)
            except (UnicodeDecodeError, ValueError):
                candidate_text = None
            if candidate_text is not None and not _is_noisy_line(candidate_text):
                chosen = candidate_start

        # Weak newline fallback.
        if chosen is None:
            newline_patterns = _get_boundary_patterns(
                _LEFT_NEWLINE_CODEPOINTS, encoding,
            )
            n = _find_aligned_left_boundary(
                data, lower_bound, match_pos, newline_patterns, alignment,
            )
            if n is not None:
                n_pos, n_len = n
                chosen = n_pos + n_len

        if chosen is not None:
            start = chosen

    return start, end


# ---------------------------------------------------------------------------
# String extraction helpers
# ---------------------------------------------------------------------------

def _extract_utf16le(
    data: bytes,
    match_pos: int,
    max_chars: int = _MAX_EXTRACT_CHARS,
) -> str | None:
    """Extract a null-terminated UTF-16LE string containing *match_pos*.

    Returns ``None`` if the position is odd-aligned (invalid for UTF-16LE)
    or decoding fails.

    The window is first computed using the strong ``\\x00\\x00`` boundary
    on both sides and then refined via :func:`_refine_extract_window` so
    that VN script blobs (entire files in one null-terminated block) are
    sliced down to the dialog paragraph surrounding the match.
    """
    if match_pos % 2 != 0:
        return None

    max_bytes = max_chars * 2

    # Scan backward for \x00\x00 (aligned to 2 bytes) or buffer start.
    start = match_pos
    lower = max(0, match_pos - max_bytes)
    while start > lower + 1:
        if data[start - 2] == 0 and data[start - 1] == 0:
            break
        start -= 2

    # Scan forward for \x00\x00 (aligned to 2 bytes) or buffer end.
    end = match_pos
    upper = min(len(data), match_pos + max_bytes)
    while end + 1 < upper:
        if data[end] == 0 and data[end + 1] == 0:
            break
        end += 2

    # Multi-boundary refinement (no-op for short blocks).
    start, end = _refine_extract_window(
        data, start, end, match_pos, "utf-16-le", alignment=2,
    )

    if end <= start:
        return None

    try:
        return data[start:end].decode("utf-16-le")
    except (UnicodeDecodeError, ValueError):
        return None


def _extract_byte_delimited(
    data: bytes,
    match_pos: int,
    encoding: str,
    max_chars: int = _MAX_EXTRACT_CHARS,
) -> str | None:
    """Extract a null-terminated string (single ``\\x00`` delimiter).

    Works for UTF-8 and Shift-JIS (both use single-byte null terminator).

    The window is first computed using the strong ``\\x00`` boundary on
    both sides and then refined via :func:`_refine_extract_window` to
    trim VN-engine script blobs down to the local dialog paragraph.
    """
    max_bytes = max_chars * 4  # UTF-8 worst case: 4 bytes per char

    # Scan backward for \x00 or buffer start.
    start = match_pos
    lower = max(0, match_pos - max_bytes)
    while start > lower:
        if data[start - 1] == 0:
            break
        start -= 1

    # Scan forward for \x00 or buffer end.
    end = match_pos
    upper = min(len(data), match_pos + max_bytes)
    while end < upper:
        if data[end] == 0:
            break
        end += 1

    # Multi-boundary refinement (no-op for short blocks).
    start, end = _refine_extract_window(
        data, start, end, match_pos, encoding, alignment=1,
    )

    if end <= start:
        return None

    codec = "cp932" if encoding == "shift-jis" else encoding
    try:
        return data[start:end].decode(codec)
    except (UnicodeDecodeError, ValueError):
        return None


def _extract_string(
    data: bytes,
    match_pos: int,
    encoding: str,
) -> str | None:
    """Dispatch to the appropriate extraction function for *encoding*.

    The returned text is always normalised to ``\\n`` line endings so that
    downstream code (correction, caching, dataset) never sees ``\\r\\n``.
    """
    if encoding == "utf-16-le":
        text = _extract_utf16le(data, match_pos)
    else:
        text = _extract_byte_delimited(data, match_pos, encoding)
    if text is not None:
        text = normalize_text(text)
    return text


def _is_quality_text(text: str) -> bool:
    """Return ``True`` if *text* looks like meaningful natural-language text.

    Rejects very short strings and strings dominated by control / whitespace
    characters.
    """
    if len(text) < _MIN_TEXT_LENGTH:
        return False
    printable = sum(1 for ch in text if not ch.isspace() and ch.isprintable())
    return printable >= _MIN_TEXT_LENGTH


def _is_japanese_or_common_text_char(char: str) -> bool:
    """Return ``True`` for chars commonly seen in JP VN dialog text."""
    cp = ord(char)

    if char.isspace():
        return True

    if _is_cjk(char):
        return True

    if 0x0020 <= cp <= 0x007E:  # Basic ASCII printable
        return True

    if 0x3000 <= cp <= 0x303F:  # CJK symbols and punctuation
        return True

    if 0xFF00 <= cp <= 0xFFEF:  # Fullwidth forms
        return True

    if cp in {0x2025, 0x2026, 0x201C, 0x201D}:  # ‥ … “ ”
        return True

    return False


def _is_noisy_line(line: str) -> bool:
    """Heuristic: line contains many chars unlikely for JP dialog text."""
    non_space = [ch for ch in line if not ch.isspace()]
    if not non_space:
        return False

    noisy_count = sum(1 for ch in non_space if not _is_japanese_or_common_text_char(ch))
    return noisy_count >= 2 and (noisy_count / len(non_space)) >= 0.15


def _refine_to_lines(
    text: str,
    needle: str,
    *,
    context_lines: int = 3,
) -> str:
    """Narrow a long text block to lines near *needle*.

    VN engines (e.g. Light.VN) load entire script files into memory as
    one null-terminated string.  This function splits by ``\\r\\n`` /
    ``\\n``, locates the line containing *needle*, and returns a small
    window of adjacent lines — producing a paragraph-sized candidate
    instead of the full script.
    """
    lines = text.replace("\r\n", "\n").split("\n")

    # Find which line contains the needle.
    center: int | None = None
    for i, line in enumerate(lines):
        if needle in line:
            center = i
            break

    if center is None:
        return text  # defensive — should not happen

    lo = max(0, center - context_lines)
    hi = min(len(lines), center + context_lines + 1)

    # Trim empty / whitespace-only lines from boundaries.
    while lo < center and not lines[lo].strip():
        lo += 1
    while hi > center + 1 and not lines[hi - 1].strip():
        hi -= 1

    selected = lines[lo:hi]

    # Drop noisy surrounding lines (keep center line even if noisy).
    center_rel = center - lo
    if len(selected) > 1:
        filtered: list[str] = []
        for i, line in enumerate(selected):
            if i == center_rel:
                filtered.append(line)
                continue
            if _is_noisy_line(line):
                continue
            filtered.append(line)
        if filtered:
            selected = filtered

    return "\n".join(selected)


# ---------------------------------------------------------------------------
# Encoder helper
# ---------------------------------------------------------------------------

def _try_encode(text: str, encoding: str) -> bytes | None:
    """Encode *text* with *encoding*, returning ``None`` on failure."""
    codec = "cp932" if encoding == "shift-jis" else encoding
    try:
        return text.encode(codec)
    except (UnicodeEncodeError, LookupError):
        return None


# ---------------------------------------------------------------------------
# MemoryScanner
# ---------------------------------------------------------------------------

class MemoryScanner:
    """Scan a target process's memory for text strings.

    Opens the process with ``PROCESS_VM_READ | PROCESS_QUERY_INFORMATION``
    (read-only, zero intrusion).  Implements the context-manager protocol
    for automatic handle cleanup.

    Parameters
    ----------
    pid:
        Target process ID.

    Example
    -------
    ::

        with MemoryScanner(pid=game_target.pid) as ms:
            results = ms.scan("テスト")
            for r in results:
                print(r.text, r.encoding)
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._handle: int = w32.open_process_readonly(pid)
        self._learned_encoding: str | None = None
        # Regions where CJK text was previously found — scanned first.
        self._hot_regions: list[tuple[int, int]] = []
        self._hot_region_set: set[tuple[int, int]] = set()
        # Cached result of enumerate_regions() — rebuilt every _REGIONS_CACHE_TTL_S seconds.
        self._cached_regions: list[tuple[int, int, int, int]] | None = None
        self._regions_cached_at: float = 0.0

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> "MemoryScanner":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the process handle."""
        if self._handle:
            w32.close_handle(self._handle)
            self._handle = 0

    # -- Properties --------------------------------------------------------

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def learned_encoding(self) -> str | None:
        """Encoding established by a previous successful scan, or ``None``."""
        return self._learned_encoding

    @learned_encoding.setter
    def learned_encoding(self, value: str | None) -> None:
        self._learned_encoding = value

    # -- Region cache ------------------------------------------------------

    def _get_regions(self) -> list[tuple[int, int, int, int]]:
        """Return cached readable regions, re-enumerating after the TTL expires."""
        now = time.monotonic()
        if (
            self._cached_regions is None
            or (now - self._regions_cached_at) > _REGIONS_CACHE_TTL_S
        ):
            self._cached_regions = w32.enumerate_regions(self._handle)
            self._regions_cached_at = now
        return self._cached_regions

    def invalidate_regions_cache(self) -> None:
        """Force re-enumeration of memory regions on the next scan."""
        self._cached_regions = None

    # -- Main API ----------------------------------------------------------

    def scan(
        self,
        needle: str,
        *,
        encodings: Sequence[str] | None = None,
        max_results: int = 10,
        max_region_bytes: int = _MAX_REGION_BYTES,
    ) -> list[ScanResult]:
        """Scan the target process memory for *needle*.

        Encodes *needle* in each candidate encoding, searches all committed
        readable memory regions, extracts null-terminated strings around each
        hit, and returns deduplicated results.

        Parameters
        ----------
        needle:
            Text to search for (typically a CJK substring from OCR output;
            see :func:`pick_needle`).
        encodings:
            Encodings to try, in priority order.  Defaults to
            ``["utf-16-le", "utf-8", "shift-jis"]``.  If a previous scan
            established :attr:`learned_encoding`, it is tried first.
        max_results:
            Stop after collecting this many unique text hits.
        max_region_bytes:
            Skip memory regions larger than this (avoids huge mapped files).

        Returns
        -------
        list[ScanResult]
            Deduplicated matches sorted by address.
        """
        if not self._handle:
            raise RuntimeError("MemoryScanner is closed")

        enc_order = self._encoding_order(encodings)
        all_regions = self._get_regions()

        results: list[ScanResult] = []
        seen_texts: set[str] = set()

        for enc in enc_order:
            needle_bytes = _try_encode(needle, enc)
            if needle_bytes is None:
                continue

            # Phase 1: scan hot regions first (likely to hit).
            for base, size in self._hot_regions:
                if len(results) >= max_results:
                    break
                self._scan_one_region(
                    base, size, needle_bytes, needle, enc,
                    results, seen_texts, max_results,
                )

            if len(results) >= max_results:
                self._learned_encoding = enc
                return results

            # Phase 2: scan all remaining regions.
            for base, size, _protect, _rtype in all_regions:
                if len(results) >= max_results:
                    break
                if (base, size) in self._hot_region_set:
                    continue  # already scanned in phase 1
                if size > max_region_bytes:
                    continue
                self._scan_one_region(
                    base, size, needle_bytes, needle, enc,
                    results, seen_texts, max_results,
                )

            if results:
                self._learned_encoding = enc
                return results

        return results

    # -- Internal helpers --------------------------------------------------

    def _encoding_order(
        self,
        explicit: Sequence[str] | None,
    ) -> list[str]:
        """Build the encoding priority list."""
        if explicit is not None:
            return list(explicit)
        base = list(_DEFAULT_ENCODINGS)
        if self._learned_encoding and self._learned_encoding in base:
            base.remove(self._learned_encoding)
            base.insert(0, self._learned_encoding)
        return base

    def _search_region_data(
        self,
        data: bytes,
        base: int,
        size: int,
        needle_bytes: bytes,
        needle_text: str,
        encoding: str,
        results: list[ScanResult],
        seen_texts: set[str],
        max_results: int,
    ) -> None:
        """Search already-read region *data* for *needle_bytes* and append hits to *results*."""
        positions = find_all_positions(data, needle_bytes, max_results=max_results * 4)

        for pos in positions:
            if len(results) >= max_results:
                return

            text = _extract_string(data, pos, encoding)
            if text is None or not _is_quality_text(text):
                continue

            # Refine large blocks (e.g. entire VN script files) and also
            # refine multiline candidates containing the needle. This removes
            # nearby binary noise lines while keeping relevant dialog context.
            if len(text) > _LINE_REFINE_THRESHOLD or ("\n" in text and needle_text in text):
                text = _refine_to_lines(text, needle_text)
                if not _is_quality_text(text):
                    continue

            if text in seen_texts:
                continue

            seen_texts.add(text)
            results.append(ScanResult(
                text=text,
                encoding=encoding,
                address=base + pos,
                region_base=base,
            ))

            # Remember this region as hot for future scans.
            key = (base, size)
            if key not in self._hot_region_set:
                self._hot_regions.append(key)
                self._hot_region_set.add(key)

    def _scan_one_region(
        self,
        base: int,
        size: int,
        needle_bytes: bytes,
        needle_text: str,
        encoding: str,
        results: list[ScanResult],
        seen_texts: set[str],
        max_results: int,
    ) -> None:
        """Read one memory region and search for *needle_bytes*."""
        data = w32.read_region(self._handle, base, size)
        if data is None:
            return
        self._search_region_data(
            data, base, size, needle_bytes, needle_text, encoding,
            results, seen_texts, max_results,
        )

    def scan_any(
        self,
        needles: list[str],
        *,
        encodings: Sequence[str] | None = None,
        max_results: int = 10,
        max_region_bytes: int = _MAX_REGION_BYTES,
    ) -> tuple[str, list[ScanResult]]:
        """Return results for the first needle in *needles* that produces hits.

        Reads each memory region **once** and searches for all needles
        simultaneously, avoiding the redundant ``ReadProcessMemory`` calls that
        sequential :meth:`scan` invocations would incur when multiple needles are
        tried.

        Parameters
        ----------
        needles:
            Candidate substrings in priority order (highest priority first).
            Produced by :func:`pick_needles`.
        encodings:
            Encoding priority list.  Defaults to learned encoding first, then
            ``["utf-16-le", "utf-8", "shift-jis"]``.
        max_results:
            Stop collecting for a given needle after this many unique hits.
        max_region_bytes:
            Skip regions larger than this.

        Returns
        -------
        tuple[str, list[ScanResult]]
            ``(matched_needle, results)`` — empty string and empty list when
            nothing is found.
        """
        if not needles:
            return ("", [])
        if not self._handle:
            raise RuntimeError("MemoryScanner is closed")

        enc_order = self._encoding_order(encodings)
        all_regions = self._get_regions()

        for enc in enc_order:
            # Precompute encoded needle bytes, preserving caller's priority order.
            encoded: list[tuple[str, bytes]] = []
            for n in needles:
                nb = _try_encode(n, enc)
                if nb is not None:
                    encoded.append((n, nb))
            if not encoded:
                continue

            # Per-needle result accumulators.
            per_results: dict[str, list[ScanResult]] = {n: [] for n, _ in encoded}
            per_seen: dict[str, set[str]] = {n: set() for n, _ in encoded}

            def _hit_all_needles(data: bytes, b: int, sz: int) -> None:
                for nt, nb in encoded:
                    rl = per_results[nt]
                    if len(rl) < max_results:
                        self._search_region_data(
                            data, b, sz, nb, nt, enc, rl, per_seen[nt], max_results,
                        )

            # Phase 1: hot regions first.
            for b, sz in self._hot_regions:
                data = w32.read_region(self._handle, b, sz)
                if data is not None:
                    _hit_all_needles(data, b, sz)

            # Return on the first (highest-priority) needle with hits.
            for nt, _ in encoded:
                if per_results[nt]:
                    self._learned_encoding = enc
                    return (nt, per_results[nt])

            # Phase 2: remaining regions — single read per region for all needles.
            for b, sz, _protect, _rtype in all_regions:
                if (b, sz) in self._hot_region_set:
                    continue
                if sz > max_region_bytes:
                    continue
                data = w32.read_region(self._handle, b, sz)
                if data is not None:
                    _hit_all_needles(data, b, sz)

            for nt, _ in encoded:
                if per_results[nt]:
                    self._learned_encoding = enc
                    return (nt, per_results[nt])

        return ("", [])
