# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SQLite schema for the pipeline dataset (OCR→memory→correction samples)."""
from __future__ import annotations

import sqlite3

_CREATE_SAMPLES = """
CREATE TABLE IF NOT EXISTS pipeline_samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    ocr_text            TEXT    NOT NULL DEFAULT '',
    memory_hits         TEXT    NOT NULL DEFAULT '[]',
    needle              TEXT    NOT NULL DEFAULT '',
    corrected_text      TEXT    NOT NULL DEFAULT '',
    translated_text     TEXT    NOT NULL DEFAULT '',
    label               TEXT    NOT NULL DEFAULT 'unlabeled',
    expected_correction TEXT    NOT NULL DEFAULT '',
    notes               TEXT    NOT NULL DEFAULT '',
    annotated_at        TEXT
);
"""

# label values: 'unlabeled' | 'ok' | 'bad_range' | 'bad_correction' | 'bad_memory' | 'other'

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_samples_label       ON pipeline_samples(label);
CREATE INDEX IF NOT EXISTS idx_samples_captured_at ON pipeline_samples(captured_at);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indices if absent."""
    conn.executescript(_CREATE_SAMPLES + _CREATE_IDX)
    conn.commit()
