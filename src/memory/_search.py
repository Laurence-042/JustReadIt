"""Fast byte-pattern search with optional C DLL acceleration.

The pure-Python fallback uses CPython's built-in ``bytes.find()`` which is
implemented in C via a Boyer-Moore-Horspool variant and is already quite fast.

When ``mem_scan.dll`` is present (built via ``build.ps1``), the search is
delegated to the DLL for additional speed via ``memchr`` + ``memcmp``
(typically SIMD-optimised by the MSVC CRT).

Usage::

    from src.memory._search import find_all_positions

    offsets = find_all_positions(haystack_bytes, needle_bytes)
"""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# C DLL acceleration (optional)
# ---------------------------------------------------------------------------

_dll: ctypes.CDLL | None = None
_dll_load_attempted = False
_DLL_PATH = Path(__file__).with_name("mem_scan.dll")


def _try_load_dll() -> None:
    """Attempt to load ``mem_scan.dll`` once.  A no-op after the first call."""
    global _dll, _dll_load_attempted
    if _dll_load_attempted:
        return
    _dll_load_attempted = True

    if not _DLL_PATH.exists():
        _log.debug("mem_scan.dll not found at %s, using Python fallback", _DLL_PATH)
        return

    try:
        lib = ctypes.CDLL(str(_DLL_PATH))
        lib.find_all.restype = ctypes.c_int
        lib.find_all.argtypes = [
            ctypes.c_void_p,                      # haystack
            ctypes.c_size_t,                      # haystack_len
            ctypes.c_void_p,                      # needle
            ctypes.c_size_t,                      # needle_len
            ctypes.POINTER(ctypes.c_size_t),      # out_positions
            ctypes.c_size_t,                      # max_results
        ]
        _dll = lib
        _log.info("Loaded mem_scan.dll for accelerated byte search")
    except OSError:
        _log.debug("Failed to load mem_scan.dll, using Python fallback", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_all_positions(
    haystack: bytes,
    needle: bytes,
    *,
    max_results: int = 512,
) -> list[int]:
    """Return byte offsets of every occurrence of *needle* in *haystack*.

    Uses the C DLL when available, otherwise falls back to CPython's
    ``bytes.find()``.  Matches may overlap (stride = 1).

    Parameters
    ----------
    haystack:
        Raw bytes to search (typically one memory region).
    needle:
        Byte pattern to find.
    max_results:
        Stop after this many hits to avoid runaway scanning.
    """
    if not needle or not haystack or len(needle) > len(haystack):
        return []

    _try_load_dll()

    if _dll is not None:
        out = (ctypes.c_size_t * max_results)()
        count = _dll.find_all(haystack, len(haystack), needle, len(needle), out, max_results)
        return [out[i] for i in range(count)]

    # ---- Python fallback (CPython bytes.find is C-fast) ----
    positions: list[int] = []
    start = 0
    while len(positions) < max_results:
        pos = haystack.find(needle, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    return positions
