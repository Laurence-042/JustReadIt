# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a cup of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pipeline dataset — records OCR→memory→correction samples for algorithm QA.

Usage::

    with PipelineDataset.open(path) as ds:
        ds.record(ocr_text="...", memory_hits=["…"], needle="…",
                  corrected_text="…", translated_text="…")
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ._db import init_schema

# Valid label values
LABELS: tuple[str, ...] = ("unlabeled", "ok", "bad_range", "bad_correction", "bad_memory", "other")

LABEL_DISPLAY: dict[str, str] = {
    "unlabeled":      "未标注",
    "ok":             "✓ 正确",
    "bad_range":      "✗ 范围错误",
    "bad_correction": "✗ 纠错错误",
    "bad_memory":     "✗ 内存匹配错误",
    "other":          "✗ 其他",
}


@dataclass
class SampleRow:
    """One row from ``pipeline_samples``."""
    id: int
    captured_at: str
    ocr_text: str
    memory_hits: list[str]
    needle: str
    corrected_text: str
    translated_text: str
    label: str
    expected_correction: str
    notes: str
    annotated_at: str | None


class PipelineDataset:
    """SQLite-backed store for pipeline samples.

    Prefer using as a context manager so the connection is closed cleanly::

        with PipelineDataset.open(path) as ds:
            ...
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def open(cls, path: Path | str) -> "PipelineDataset":
        """Open (or create) the dataset DB at *path*."""
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        return cls(conn)

    # ── Context-manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "PipelineDataset":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Write ─────────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        ocr_text: str,
        memory_hits: Sequence[str],
        needle: str,
        corrected_text: str,
        translated_text: str,
    ) -> int:
        """Insert a new sample and return its row id."""
        cur = self._conn.execute(
            """
            INSERT INTO pipeline_samples
                (ocr_text, memory_hits, needle, corrected_text, translated_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ocr_text,
                json.dumps(list(memory_hits), ensure_ascii=False),
                needle,
                corrected_text,
                translated_text,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def annotate(
        self,
        sample_id: int,
        *,
        label: str,
        expected_correction: str = "",
        notes: str = "",
    ) -> None:
        """Set annotation fields for a sample."""
        if label not in LABELS:
            raise ValueError(f"Invalid label {label!r}; choose from {LABELS}")
        self._conn.execute(
            """
            UPDATE pipeline_samples
            SET label = ?, expected_correction = ?, notes = ?,
                annotated_at = datetime('now')
            WHERE id = ?
            """,
            (label, expected_correction, notes, sample_id),
        )
        self._conn.commit()

    def delete(self, sample_id: int) -> None:
        """Delete a sample by id."""
        self._conn.execute(
            "DELETE FROM pipeline_samples WHERE id = ?", (sample_id,)
        )
        self._conn.commit()

    # ── Read ──────────────────────────────────────────────────────────────────

    def count(self, label_filter: str = "") -> int:
        """Total sample count, optionally filtered by label."""
        if label_filter:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM pipeline_samples WHERE label = ?",
                (label_filter,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM pipeline_samples"
            ).fetchone()
        return row[0]

    def list_samples(
        self,
        limit: int = 200,
        offset: int = 0,
        label_filter: str = "",
    ) -> list[SampleRow]:
        """Return samples ordered by captured_at descending."""
        sql = """
            SELECT id, captured_at, ocr_text, memory_hits, needle,
                   corrected_text, translated_text,
                   label, expected_correction, notes, annotated_at
            FROM pipeline_samples
        """
        params: list[object] = []
        if label_filter:
            sql += " WHERE label = ?"
            params.append(label_filter)
        sql += " ORDER BY captured_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SampleRow(
                id=r["id"],
                captured_at=r["captured_at"],
                ocr_text=r["ocr_text"],
                memory_hits=json.loads(r["memory_hits"] or "[]"),
                needle=r["needle"],
                corrected_text=r["corrected_text"],
                translated_text=r["translated_text"],
                label=r["label"],
                expected_correction=r["expected_correction"],
                notes=r["notes"],
                annotated_at=r["annotated_at"],
            )
            for r in rows
        ]

    def get(self, sample_id: int) -> SampleRow | None:
        """Fetch a single sample by id, or None."""
        row = self._conn.execute(
            """
            SELECT id, captured_at, ocr_text, memory_hits, needle,
                   corrected_text, translated_text,
                   label, expected_correction, notes, annotated_at
            FROM pipeline_samples WHERE id = ?
            """,
            (sample_id,),
        ).fetchone()
        if row is None:
            return None
        return SampleRow(
            id=row["id"],
            captured_at=row["captured_at"],
            ocr_text=row["ocr_text"],
            memory_hits=json.loads(row["memory_hits"] or "[]"),
            needle=row["needle"],
            corrected_text=row["corrected_text"],
            translated_text=row["translated_text"],
            label=row["label"],
            expected_correction=row["expected_correction"],
            notes=row["notes"],
            annotated_at=row["annotated_at"],
        )
