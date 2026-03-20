# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SQLite schema initialisation for the game knowledge base."""
from __future__ import annotations

import sqlite3

# ── DDL ──────────────────────────────────────────────────────────────────────

_CREATE_TERMS = """
CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT    NOT NULL DEFAULT 'term',
    original    TEXT    NOT NULL,
    reading     TEXT    NOT NULL DEFAULT '',
    translation TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    embedding   BLOB,                          -- numpy float32, may be NULL
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_original ON terms(original);
"""

# FTS5 content table — mirrors the terms table columns used for search.
# 'content=terms' makes it a content table: row data stored in terms,
# FTS index stores only the search tokens.
_CREATE_TERMS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS terms_fts USING fts5(
    original, reading, translation, description, category,
    content='terms',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# Triggers to keep the FTS index in sync with the terms table.
_TERMS_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS terms_ai AFTER INSERT ON terms BEGIN
    INSERT INTO terms_fts(rowid, original, reading, translation, description, category)
    VALUES (new.id, new.original, new.reading, new.translation,
            new.description, new.category);
END;
CREATE TRIGGER IF NOT EXISTS terms_ad AFTER DELETE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, original, reading, translation,
                          description, category)
    VALUES ('delete', old.id, old.original, old.reading, old.translation,
            old.description, old.category);
END;
CREATE TRIGGER IF NOT EXISTS terms_au AFTER UPDATE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, original, reading, translation,
                          description, category)
    VALUES ('delete', old.id, old.original, old.reading, old.translation,
            old.description, old.category);
    INSERT INTO terms_fts(rowid, original, reading, translation, description, category)
    VALUES (new.id, new.original, new.reading, new.translation,
            new.description, new.category);
END;
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    summary     TEXT    NOT NULL,
    turn_index  INTEGER NOT NULL DEFAULT -1,
    embedding   BLOB,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_EVENTS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    summary,
    content='events',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_EVENTS_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary)
    VALUES ('delete', old.id, old.summary);
    INSERT INTO events_fts(rowid, summary) VALUES (new.id, new.summary);
END;
"""

# ── Public helper ─────────────────────────────────────────────────────────────

def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, FTS virtual tables and sync triggers if absent."""
    conn.executescript(
        _CREATE_TERMS
        + _CREATE_TERMS_FTS
        + _TERMS_FTS_TRIGGERS
        + _CREATE_EVENTS
        + _CREATE_EVENTS_FTS
        + _EVENTS_FTS_TRIGGERS
    )
    conn.commit()
