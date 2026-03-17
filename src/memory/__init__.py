"""Process memory scanner — zero-intrusion text extraction via ReadProcessMemory.

Scans the target process's committed readable memory regions for encoded text
strings matching OCR output, extracts complete null-terminated strings, and
returns clean text for downstream Levenshtein matching.

Public API
----------
.. autoclass:: MemoryScanner
.. autoclass:: ScanResult
.. autofunction:: pick_needles
"""
from __future__ import annotations

from .scanner import MemoryScanner, ScanResult, pick_needles

__all__ = [
    "MemoryScanner",
    "ScanResult",
    "pick_needles",
]
