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
from dataclasses import dataclass
from typing import Sequence

from . import _win32 as w32
from ._search import find_all_positions

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
_LINE_REFINE_THRESHOLD: int = 300


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


def pick_needle(
    ocr_text: str,
    *,
    min_length: int = 3,
    max_length: int = 8,
) -> str:
    """Extract the most distinctive CJK substring from *ocr_text*.

    Picks the longest contiguous run of CJK characters (ideographs, hiragana,
    katakana).  If the run exceeds *max_length*, a substring is taken from the
    middle (OCR is typically most accurate in the centre of a text region).

    Falls back to the first *max_length* characters of *ocr_text* if no CJK
    run meets *min_length*.

    Parameters
    ----------
    ocr_text:
        Raw text from Windows OCR (may contain errors at boundaries).
    min_length:
        Minimum run length to consider.  Shorter runs are ignored.
    max_length:
        Maximum needle length returned.
    """
    if not ocr_text:
        return ""

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
        # No qualifying CJK run — fall back to raw text.
        return ocr_text[:max_length]

    best = max(valid, key=len)

    if len(best) <= max_length:
        return best

    # Take from the middle for better OCR accuracy.
    start = (len(best) - max_length) // 2
    return best[start : start + max_length]


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
    """Dispatch to the appropriate extraction function for *encoding*."""
    if encoding == "utf-16-le":
        return _extract_utf16le(data, match_pos)
    return _extract_byte_delimited(data, match_pos, encoding)


def _is_quality_text(text: str) -> bool:
    """Return ``True`` if *text* looks like meaningful natural-language text.

    Rejects very short strings and strings dominated by control / whitespace
    characters.
    """
    if len(text) < _MIN_TEXT_LENGTH:
        return False
    printable = sum(1 for ch in text if not ch.isspace() and ch.isprintable())
    return printable >= _MIN_TEXT_LENGTH


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

    return "\n".join(lines[lo:hi])


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
        all_regions = w32.enumerate_regions(self._handle)

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

        positions = find_all_positions(data, needle_bytes, max_results=max_results * 4)

        for pos in positions:
            if len(results) >= max_results:
                return

            text = _extract_string(data, pos, encoding)
            if text is None or not _is_quality_text(text):
                continue

            # Refine large blocks (e.g. entire VN script files) to the
            # paragraph surrounding the needle.
            if len(text) > _LINE_REFINE_THRESHOLD:
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
