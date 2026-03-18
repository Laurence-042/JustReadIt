"""Translation cache keyed by perceptual hash (phash) of the translated region.

Perceptual hashing (pHash) allows cache hits even when the translated region has
minor pixel-level differences (e.g. animated backgrounds, sub-pixel rendering
variance) while still distinguishing genuinely different text content.

Usage::

    from PIL import Image
    from src.cache import PhashCache

    cache = PhashCache()
    img = Image.open("region.png")

    result = cache.get(img)
    if result is None:
        result = translator.translate(text)
        cache.put(img, result)
"""
from __future__ import annotations

from PIL.Image import Image

import imagehash

# Maximum Hamming distance between two pHashes to be considered a cache hit.
# 64-bit pHash: 0 = identical, 64 = completely different.
# Threshold of 8 (~12.5 %) tolerates minor background animation without
# letting visually distinct frames collide.
_DEFAULT_THRESHOLD: int = 8
_HASH_SIZE: int = 8  # produces 64-bit hash (hash_size²)


class PhashCache:
    """In-memory translation cache keyed by perceptual hash of the source image.

    Args:
        threshold: Maximum Hamming distance for a cache hit (default 8).
        hash_size: Controls hash resolution; default 8 → 64-bit hash.
    """

    def __init__(
        self,
        threshold: int = _DEFAULT_THRESHOLD,
        hash_size: int = _HASH_SIZE,
    ) -> None:
        self._threshold = threshold
        self._hash_size = hash_size
        # List of (hash, translation) pairs — list kept small in practice
        # because each unique on-screen text block has its own entry.
        self._entries: list[tuple[imagehash.ImageHash, str]] = []

    # ── Public API ────────────────────────────────────────────────────

    def get(self, img: Image) -> str | None:
        """Return the cached translation for *img*, or ``None`` on a miss.

        The nearest stored hash within ``threshold`` Hamming distance is
        returned; if multiple entries are within range, the closest wins.
        """
        if not self._entries:
            return None
        h = self._phash(img)
        best_dist = self._threshold + 1
        best_text: str | None = None
        for stored_hash, text in self._entries:
            dist = h - stored_hash
            if dist < best_dist:
                best_dist = dist
                best_text = text
        return best_text if best_dist <= self._threshold else None

    def put(self, img: Image, translation: str) -> None:
        """Store *translation* keyed by the pHash of *img*.

        If an existing entry is already within ``threshold`` distance,
        its translation is updated in-place rather than adding a duplicate.
        """
        h = self._phash(img)
        for i, (stored_hash, _) in enumerate(self._entries):
            if h - stored_hash <= self._threshold:
                self._entries[i] = (stored_hash, translation)
                return
        self._entries.append((h, translation))

    def clear(self) -> None:
        """Evict all cached entries."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # ── Internal ──────────────────────────────────────────────────────

    def _phash(self, img: Image) -> imagehash.ImageHash:
        return imagehash.phash(img, hash_size=self._hash_size)
