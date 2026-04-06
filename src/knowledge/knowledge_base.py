# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Game knowledge base with hybrid BM25 + vector retrieval.

Architecture
------------
* **Storage**: A single SQLite file holds ``terms`` and ``events`` tables,
  plus FTS5 virtual tables for BM25 text search.
* **BM25**: SQLite FTS5's built-in ``bm25()`` ranking function — no extra
  library needed.
* **Vector search**: Embeddings stored as raw ``float32`` BLOBs in SQLite.
  Cosine similarity computed in NumPy over the (small) in-memory array.
  Embeddings are generated on write via an optional injected callable.
* **Hybrid ranking**: Reciprocal Rank Fusion (RRF) combines BM25 and vector
  ranked lists.  Falls back to BM25-only when no embedding function is
  configured.

The embedding callable has signature::

    def embed(texts: list[str]) -> list[list[float]]: ...

This makes it easy to plug in the OpenAI embeddings API or any
``sentence-transformers`` model without hardcoding a dependency.
"""
from __future__ import annotations

import sqlite3
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.knowledge._db import init_schema

if TYPE_CHECKING:
    from collections.abc import Callable

# ── Constants ─────────────────────────────────────────────────────────────────

# RRF rank-smoothing constant (standard choice).
_RRF_K: int = 60
# Default number of results returned by search().
_DEFAULT_K: int = 8


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KnowledgeEntry:
    """A single knowledge-base hit returned by :meth:`KnowledgeBase.search`.

    Can represent either a *term* (character name, location, vocabulary) or
    a *story event* (plot summary paragraph).
    """

    kind: str           # "term" or "event"
    original: str       # original text (empty for events)
    translation: str    # translated form (empty for events)
    category: str       # term category or "event"
    description: str    # free-form extra context
    score: float        # RRF relevance score — higher is more relevant


# ── Main class ────────────────────────────────────────────────────────────────

class KnowledgeBase:
    """Persistent game knowledge base with hybrid BM25 + vector retrieval.

    Args:
        db_path: Path to the SQLite database file.  Created if absent.
        embed_fn: Optional embedding function
            ``(texts: list[str]) -> list[list[float]]``.  When provided,
            embeddings are stored on write and used for vector search.
            When absent, only BM25 text search is performed.

    Example::

        import openai
        client = openai.OpenAI(api_key="sk-...")

        def embed(texts):
            res = client.embeddings.create(
                model="text-embedding-3-small", input=texts
            )
            return [e.embedding for e in res.data]

        kb = KnowledgeBase.open("alcia_game.db", embed_fn=embed)
        kb.record_term("アルシア", "Alcia", category="character",
                       description="The protagonist's childhood friend.")
        for entry in kb.search("アルシア が 言った"):
            print(entry.original, "→", entry.translation)
        kb.close()
    """

    def __init__(
        self,
        db_path: Path | str,
        embed_fn: "Callable[[list[str]], list[list[float]]] | None" = None,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        init_schema(self._conn)
        self._embed_fn = embed_fn

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        db_path: Path | str,
        embed_fn: "Callable[[list[str]], list[list[float]]] | None" = None,
    ) -> "KnowledgeBase":
        """Open (or create) a knowledge base at *db_path*."""
        return cls(db_path, embed_fn=embed_fn)

    # ── Write API ─────────────────────────────────────────────────────────────

    def record_term(
        self,
        original: str,
        translation: str,
        *,
        category: str = "term",
        reading: str = "",
        description: str = "",
    ) -> None:
        """Insert or update a vocabulary term.

        Args:
            original: Original-language text (e.g. ``"アルシア"``).
            translation: Translated form (e.g. ``"Alcia"``).
            category: One of ``"character"``, ``"location"``, ``"item"``,
                ``"term"`` (default), or any custom label.
            reading: Pronunciation hint, e.g. romanisation or furigana.
            description: Free-form notes (role in story, aliases, etc.).
        """
        original = original.strip()
        if not original:
            raise ValueError("original must be non-empty")

        blob = self._make_embedding_blob(
            f"{original} {reading} {translation} {description}"
        )

        self._conn.execute(
            """
            INSERT INTO terms (category, original, reading, translation,
                               description, embedding, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(original) DO UPDATE SET
                category    = excluded.category,
                reading     = excluded.reading,
                translation = excluded.translation,
                description = excluded.description,
                embedding   = excluded.embedding,
                updated_at  = datetime('now')
            """,
            (category, original, reading, translation, description, blob),
        )
        self._conn.commit()

    def record_event(self, summary: str, *, turn_index: int = -1) -> None:
        """Append a story-event summary paragraph.

        Args:
            summary: A concise description of what just happened in the story.
            turn_index: Optional conversation turn counter for ordering.
        """
        summary = summary.strip()
        if not summary:
            raise ValueError("summary must be non-empty")

        blob = self._make_embedding_blob(summary)
        self._conn.execute(
            "INSERT INTO events (summary, turn_index, embedding) VALUES (?, ?, ?)",
            (summary, turn_index, blob),
        )
        self._conn.commit()

    # ── Read API ──────────────────────────────────────────────────────────────

    def search(self, query: str, k: int = _DEFAULT_K) -> list[KnowledgeEntry]:
        """Retrieve the most relevant entries for *query*.

        Performs BM25 text search via SQLite FTS5.  When an embedding
        function is configured, vector cosine similarity is also computed
        and the two ranked lists are merged with Reciprocal Rank Fusion.

        Args:
            query: Natural-language or raw game-text search query.
            k: Maximum number of results to return.

        Returns:
            Up to *k* :class:`KnowledgeEntry` instances, sorted by
            descending relevance.
        """
        query = query.strip()
        if not query:
            return []

        # BM25 ranked lists
        term_bm25 = self._bm25_terms(query, limit=k * 4)
        event_bm25 = self._bm25_events(query, limit=k * 4)

        entries: dict[tuple, KnowledgeEntry] = {}

        # Collect BM25 entries keyed by (kind, original/summary)
        bm25_ranked: list[tuple] = []
        for row in term_bm25:
            key = ("term", row["original"])
            entry = KnowledgeEntry(
                kind="term",
                original=row["original"],
                translation=row["translation"],
                category=row["category"],
                description=row["description"],
                score=0.0,
            )
            entries[key] = entry
            bm25_ranked.append(key)
        for row in event_bm25:
            key = ("event", row["summary"])
            entry = KnowledgeEntry(
                kind="event",
                original="",
                translation="",
                category="event",
                description=row["summary"],
                score=0.0,
            )
            entries[key] = entry
            bm25_ranked.append(key)

        # Optional vector ranked list
        vec_ranked: list[tuple] = []
        if self._embed_fn is not None:
            try:
                vec_ranked = self._vector_search(query, k=k * 4)
            except Exception as exc:
                warnings.warn(
                    f"KnowledgeBase vector search failed, using BM25 only: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

        # Populate vector hits that may not already be in BM25 results
        for key in vec_ranked:
            if key not in entries:
                entry = self._load_entry(key)
                if entry is not None:
                    entries[key] = entry

        # Reciprocal Rank Fusion
        rrf_scores: dict[tuple, float] = {key: 0.0 for key in entries}
        for rank, key in enumerate(bm25_ranked):
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, key in enumerate(vec_ranked):
            if key not in rrf_scores:
                rrf_scores[key] = 0.0
            rrf_scores[key] += 1.0 / (_RRF_K + rank + 1)

        # Sort by score, return top k
        sorted_keys = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:k]  # type: ignore[arg-type]
        result: list[KnowledgeEntry] = []
        for key in sorted_keys:
            if rrf_scores[key] <= 0.0:
                continue
            entry = entries.get(key)
            if entry is None:
                continue
            # Attach final score
            result.append(
                KnowledgeEntry(
                    kind=entry.kind,
                    original=entry.original,
                    translation=entry.translation,
                    category=entry.category,
                    description=entry.description,
                    score=rrf_scores[key],
                )
            )
        return result

    def get_all_terms(self) -> list[KnowledgeEntry]:
        """Return every term in the knowledge base (no ranking)."""
        rows = self._conn.execute(
            "SELECT category, original, reading, translation, description FROM terms"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [
            KnowledgeEntry(
                kind="term",
                original=r["original"],
                translation=r["translation"],
                category=r["category"],
                description=r["description"],
                score=1.0,
            )
            for r in rows
        ]

    def get_recent_events(self, limit: int = 10) -> list[KnowledgeEntry]:
        """Return the most recently recorded story events."""
        rows = self._conn.execute(
            "SELECT summary FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            KnowledgeEntry(
                kind="event",
                original="",
                translation="",
                category="event",
                description=r["summary"],
                score=1.0,
            )
            for r in rows
        ]

    def get_all_events_rows(self) -> list[tuple[int, str]]:
        """Return all events as ``(id, summary)`` tuples, newest first.

        Intended for admin/management UIs that need to identify rows by ID
        for deletion.
        """
        rows = self._conn.execute(
            "SELECT id, summary FROM events ORDER BY id DESC"
        ).fetchall()
        return [(r["id"], r["summary"]) for r in rows]

    # ── Delete API ────────────────────────────────────────────────────────────

    def delete_term(self, original: str) -> bool:
        """Delete the term whose ``original`` matches exactly.

        Returns ``True`` if a row was deleted, ``False`` if not found.
        """
        cur = self._conn.execute(
            "DELETE FROM terms WHERE original = ?", (original,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_event(self, event_id: int) -> bool:
        """Delete the event with *event_id*.

        Returns ``True`` if a row was deleted, ``False`` if not found.
        """
        cur = self._conn.execute(
            "DELETE FROM events WHERE id = ?", (event_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "KnowledgeBase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _bm25_terms(self, query: str, limit: int) -> list[sqlite3.Row]:
        """BM25 ranked term search via FTS5.

        SQLite FTS5 ``bm25()`` returns negative values; ORDER BY ascending
        gives the most relevant rows first.
        """
        try:
            # Sanitise query: FTS5 uses a mini query language; strip special
            # characters to avoid syntax errors on raw game text.
            safe_q = _sanitise_fts_query(query)
            return self._conn.execute(
                """
                SELECT t.category, t.original, t.reading, t.translation,
                       t.description
                FROM terms_fts f
                JOIN terms t ON t.id = f.rowid
                WHERE terms_fts MATCH ?
                ORDER BY bm25(terms_fts) ASC
                LIMIT ?
                """,
                (safe_q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _bm25_events(self, query: str, limit: int) -> list[sqlite3.Row]:
        try:
            safe_q = _sanitise_fts_query(query)
            return self._conn.execute(
                """
                SELECT e.summary
                FROM events_fts f
                JOIN events e ON e.id = f.rowid
                WHERE events_fts MATCH ?
                ORDER BY bm25(events_fts) ASC
                LIMIT ?
                """,
                (safe_q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _vector_search(self, query: str, k: int) -> list[tuple]:
        """Return a ranked list of (kind, key) pairs via cosine similarity."""
        import numpy as np  # optional, only used when embed_fn is set

        q_vec = np.array(self._embed_fn([query])[0], dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0.0:
            return []
        q_unit = q_vec / q_norm

        scores: list[tuple[float, tuple]] = []

        for row in self._conn.execute(
            "SELECT original, translation, category, description, embedding FROM terms"
            " WHERE embedding IS NOT NULL"
        ).fetchall():
            emb = _blob_to_vec(row["embedding"])
            if emb is None:
                continue
            sim = float(q_unit @ (emb / (np.linalg.norm(emb) + 1e-9)))
            scores.append((sim, ("term", row["original"])))

        for row in self._conn.execute(
            "SELECT summary, embedding FROM events WHERE embedding IS NOT NULL"
        ).fetchall():
            emb = _blob_to_vec(row["embedding"])
            if emb is None:
                continue
            sim = float(q_unit @ (emb / (np.linalg.norm(emb) + 1e-9)))
            scores.append((sim, ("event", row["summary"])))

        scores.sort(reverse=True)
        return [key for _, key in scores[:k]]

    def _make_embedding_blob(self, text: str) -> bytes | None:
        if self._embed_fn is None:
            return None
        try:
            import numpy as np
            vecs = self._embed_fn([text])
            arr = np.array(vecs[0], dtype=np.float32)
            return arr.tobytes()
        except Exception as exc:
            warnings.warn(
                f"KnowledgeBase: embedding generation failed, "
                f"storing entry without embedding: {exc}",
                RuntimeWarning,
                stacklevel=3,
            )
            return None

    def _load_entry(self, key: tuple) -> KnowledgeEntry | None:
        kind, pk = key
        if kind == "term":
            row = self._conn.execute(
                "SELECT category, original, translation, description FROM terms"
                " WHERE original = ?",
                (pk,),
            ).fetchone()
            if row is None:
                return None
            return KnowledgeEntry(
                kind="term",
                original=row["original"],
                translation=row["translation"],
                category=row["category"],
                description=row["description"],
                score=0.0,
            )
        if kind == "event":
            return KnowledgeEntry(
                kind="event",
                original="",
                translation="",
                category="event",
                description=pk,
                score=0.0,
            )
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────

def _sanitise_fts_query(text: str) -> str:
    """Convert arbitrary text into a safe FTS5 simple token-OR query.

    FTS5 interprets special characters (``" : ^ * ( )`` …) as query
    operators.  We strip them and join individual words with OR so the
    search degrades gracefully on raw OCR output.
    """
    import re
    tokens = re.findall(r'\w+', text, re.UNICODE)
    if not tokens:
        return '""'  # FTS5 no-op
    return " OR ".join(tokens)


def _blob_to_vec(blob: bytes) -> "object | None":
    """Deserialise a float32 BLOB back to a numpy array."""
    try:
        import numpy as np
        return np.frombuffer(blob, dtype=np.float32)
    except Exception:
        return None
