"""Translation caches — two complementary layers.

**PhashCache** (in-memory, OCR-text-keyed)
    Fast first-pass cache keyed by the OCR region text.  A cache hit
    avoids any memory-scan or translation work.  Entries survive only
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

    # Fast path: same OCR text seen this session
    result = phash_cache.get(region_text)
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

    phash_cache.put(region_text, result)
    show(result)
"""
from __future__ import annotations


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
    """In-memory translation cache keyed by OCR region text.

    Stores ``{source_text: translation}`` pairs.  A cache hit requires an
    **exact** ``source_text`` match — no image hashing involved.  This
    avoids the entire memory-scan + translation pipeline when the same
    OCR output is seen again within the current session.

    The class name is kept for backward compatibility with existing call
    sites, but the cache no longer uses perceptual hashing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────

    def get(self, source_text: str) -> str | None:
        """Return the cached translation for *source_text*, or ``None``."""
        return self._entries.get(source_text) if source_text else None

    def put(self, source_text: str, translation: str) -> None:
        """Store *translation* keyed by *source_text*."""
        if source_text:
            self._entries[source_text] = translation

    def clear(self) -> None:
        """Evict all cached entries."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
