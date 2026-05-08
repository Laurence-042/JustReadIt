"""Translation caches — two complementary layers.

**PhashCache** (in-memory, OCR-text-keyed)
    Fast first-pass cache keyed by the OCR region text.  A cache hit
    avoids any memory-scan or translation work.  Entries survive only
    for the current session.  Stores :class:`PipelineRecord` so that
    debug panels can replay full OCR / memory data on a cache hit.

**TranslationCache** (persistent, text-keyed)
    SQLite-backed cache keyed by ``(source_text, source_lang, target_lang)``.
    Survives restarts so repeated NPC dialogue lines are served instantly
    without calling the translation backend.  Operates on the corrected
    source text (after Levenshtein matching), so the key is deterministic
    regardless of minor screenshot variations.

:class:`PipelineRecord` is the shared DTO used by both caches and by
:class:`~src.dataset.PipelineDataset` — one structure that carries
everything produced by a single pipeline run.
"""
from __future__ import annotations

import dataclasses

# ── Pipeline result record ─────────────────────────────────────────────────────

import sqlite3
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class PipelineRecord:
    """Full result of one pipeline run.

    Shared by :class:`PhashCache` (in-memory) and
    :class:`~src.dataset.PipelineDataset` (SQLite) so that both layers use
    an identical data structure.

    Fields whose names match :class:`~src.dataset.PipelineDataset` columns
    (``region_text``, ``corrected_text``, ``translated_text``,
    ``memory_hits``, ``needle``) are written directly to the dataset.
    The remaining fields support debug-panel replay without re-running the
    full pipeline.

    Attributes
    ----------
    region_text:
        OCR-detected region text — primary phash-cache key.
    corrected_text:
        Text after Levenshtein correction against memory-scan results.
        Equals *region_text* when no match was found.  Used as the
        secondary phash-cache key and as the translation-cache key.
    translated_text:
        Final translation produced by the translation backend (or
        retrieved from the persistent translation cache).
    near_rect:
        ``(x, y, w, h)`` bounding rect of the detected text region in
        captured-image pixel space.  Used by the overlay to position
        itself relative to the game window.
    memory_hits:
        Raw text strings extracted from process memory by the scanner.
    needle:
        CJK substring used as the memory-scan needle.
    ocr_debug:
        Formatted OCR debug text (one line per word box).
    ocr_boxes:
        Word-level :class:`~src.ocr.range_detectors.BoundingBox` list.
    ocr_line_boxes:
        Line-level :class:`~src.ocr.range_detectors.BoundingBox` list.
    detector_name:
        Name of the :class:`~src.ocr.range_detectors.RangeDetector` that
        produced *region_text*.
    crop_rect:
        ``(left, top, right, bottom)`` crop used for range detection, in
        image-pixel space.  ``None`` when range detection produced no result.
    mem_text:
        Formatted memory-scan debug string (shown in the debug panel).
    from_cache:
        ``True`` when this record was served from :class:`PhashCache`
        rather than from a fresh pipeline run.  Used to skip dataset
        recording for cache hits (no new data to learn from).
    ocr_ms, range_ms, scan_ms, corr_ms, translate_ms:
        Wall-clock duration of each pipeline step in milliseconds.
    """

    region_text:    str
    corrected_text: str
    translated_text: str
    near_rect:      tuple[int, int, int, int]       = (0, 0, 0, 0)
    memory_hits:    list[str]                       = dataclasses.field(default_factory=list)
    needle:         str                             = ""
    ocr_debug:      str                             = ""
    ocr_boxes:      list                            = dataclasses.field(default_factory=list)
    ocr_line_boxes: list                            = dataclasses.field(default_factory=list)
    detector_name:  str                             = ""
    crop_rect:      tuple[int, int, int, int] | None = None
    mem_text:       str                             = ""
    from_cache:     bool                            = False
    ocr_ms:         float                           = 0.0
    range_ms:       float                           = 0.0
    scan_ms:        float                           = 0.0
    corr_ms:        float                           = 0.0
    translate_ms:   float                           = 0.0


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

    Stores :class:`PipelineRecord` objects so that a cache hit can replay
    the full OCR / memory-scan debug output without re-running the pipeline.

    Each record is indexed under *both* ``region_text`` and
    ``corrected_text`` so that a later OCR hit that resolves to the same
    corrected text (but with slightly different raw OCR) still finds a hit.

    The class name is kept for backward compatibility with existing call
    sites; perceptual hashing was removed in an earlier refactor.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PipelineRecord] = {}

    # ── Public API ────────────────────────────────────────────────────

    def get(self, source_text: str) -> PipelineRecord | None:
        """Return the cached :class:`PipelineRecord` for *source_text*, or ``None``."""
        return self._entries.get(source_text) if source_text else None

    def put(self, record: PipelineRecord) -> None:
        """Store *record* under its ``region_text`` and ``corrected_text`` keys."""
        if record.region_text:
            self._entries[record.region_text] = record
        if record.corrected_text and record.corrected_text != record.region_text:
            self._entries[record.corrected_text] = record

    def clear(self) -> None:
        """Evict all cached entries."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
