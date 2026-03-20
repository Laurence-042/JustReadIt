"""Translation caches — two complementary layers.

**PhashCache** (in-memory, perceptual-hash-keyed)
    Fast first-pass cache using the perceptual hash of a screenshot region.
    A cache hit avoids any OCR or translation work.  Entries survive only
    for the current session.

**TranslationCache** (persistent, text-keyed)
    SQLite-backed cache keyed by ``(source_text, source_lang, target_lang)``.
    Survives restarts so repeated NPC dialogue lines are served instantly
    without calling the translation backend.  Operates on the corrected
    source text (after Levenshtein matching), so the key is deterministic
    regardless of minor screenshot variations.

Typical pipeline::

    phash_cache = PhashCache()
    text_cache  = TranslationCache(translations_db_path())

    # Fast path: phash hit (screenshot-level dedup)
    result = phash_cache.get(region_img)
    if result:
        show(result)
        return

    # Slow path: OCR + memory scan + correction ...
    source_text = corrected_text_or_ocr_fallback

    # Medium path: text-level dedup (same dialogue seen before)
    result = text_cache.get(source_text, source_lang, target_lang)
    if result is None:
        result = translator.translate(source_text, target_lang=target_lang)
        text_cache.put(source_text, source_lang, target_lang, result)

    phash_cache.put(region_img, result)
    show(result)
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


# ── Persistent text-level translation cache ───────────────────────────────────

import sqlite3
from pathlib import Path


class TranslationCache:
    """Persistent SQLite-backed cache keyed by ``(source_text, source_lang,
    target_lang)``.

    Survives restarts so repeated NPC dialogues are served instantly without
    calling the translation backend again.  Unlike :class:`PhashCache`, this
    operates on the *corrected source text* (after Levenshtein matching), so
    the key is deterministic regardless of minor screenshot variations.

    Args:
        db_path: Path to the SQLite file.  Created if absent; parent
            directories are created automatically.

    Example::

        from src.cache import TranslationCache
        from src.paths import translations_db_path

        cache = TranslationCache(translations_db_path())
        hit = cache.get("こんにちは", "ja", "en")
        if hit is None:
            hit = translator.translate("こんにちは", target_lang="en")
            cache.put("こんにちは", "ja", "en", hit)
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS translations (
        source_text  TEXT NOT NULL,
        source_lang  TEXT NOT NULL,
        target_lang  TEXT NOT NULL,
        translation  TEXT NOT NULL,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_text, source_lang, target_lang)
    );
    """

    def __init__(self, db_path: Path | str) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def get(
        self,
        source_text: str,
        source_lang: str,
        target_lang: str,
    ) -> str | None:
        """Return cached translation or ``None`` on a miss."""
        row = self._conn.execute(
            "SELECT translation FROM translations"
            " WHERE source_text=? AND source_lang=? AND target_lang=?",
            (source_text, source_lang, target_lang),
        ).fetchone()
        return row[0] if row else None

    def put(
        self,
        source_text: str,
        source_lang: str,
        target_lang: str,
        translation: str,
    ) -> None:
        """Upsert a translation into the cache."""
        self._conn.execute(
            """
            INSERT INTO translations
                (source_text, source_lang, target_lang, translation)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_text, source_lang, target_lang)
            DO UPDATE SET translation = excluded.translation,
                          created_at  = datetime('now')
            """,
            (source_text, source_lang, target_lang, translation),
        )
        self._conn.commit()

    def invalidate(
        self,
        source_text: str,
        source_lang: str,
        target_lang: str,
    ) -> None:
        """Remove a single entry from the cache."""
        self._conn.execute(
            "DELETE FROM translations"
            " WHERE source_text=? AND source_lang=? AND target_lang=?",
            (source_text, source_lang, target_lang),
        )
        self._conn.commit()

    def clear(self) -> None:
        """Delete all cached translations."""
        self._conn.execute("DELETE FROM translations")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __len__(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM translations"
        ).fetchone()
        return row[0] if row else 0

    def __enter__(self) -> "TranslationCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
