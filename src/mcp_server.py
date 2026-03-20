# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""JustReadIt MCP stdio server — exposes the game knowledge base to external
MCP clients such as Claude Desktop, Cursor, or VS Code.

The server wraps the same :class:`~src.knowledge.KnowledgeBase` instance that
the in-process OpenAI translator uses, so any term or event written by the LLM
during translation is immediately visible to external clients (and vice-versa).

Tools exposed
-------------
* ``record_term``   — persist a character name, location or vocabulary item.
* ``record_event``  — persist a story-event summary paragraph.
* ``search_terms``  — hybrid BM25 + vector search across all stored knowledge.

Usage
-----
Run as a stdio server (add to Claude Desktop ``mcpServers`` config)::

    {
      "justreadit": {
        "command": "python",
        "args": ["-m", "src.mcp_server"],
        "cwd": "C:/Users/.../JustReadIt"
      }
    }

Override the database path::

    python -m src.mcp_server --db C:/path/to/my_game.db

Requirements::

    pip install mcp
"""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _exc:
    raise SystemExit(
        "mcp package not installed.  Run:\n"
        "    pip install mcp\n"
        "or:\n"
        "    pip install justreadit[knowledge]"
    ) from _exc

from src.knowledge.knowledge_base import KnowledgeBase
from src.paths import knowledge_db_path

# ── Default DB path ───────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    return knowledge_db_path()


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="justreadit-mcp",
        description="JustReadIt MCP stdio server for the game knowledge base.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the SQLite knowledge-base file "
             f"(default: {_default_db_path()})",
    )
    return p.parse_args()


# ── Server factory ────────────────────────────────────────────────────────────

def build_server(db_path: Path) -> FastMCP:
    """Construct and return a :class:`FastMCP` instance bound to *db_path*.

    Separated from ``main()`` so the server can be imported and tested
    without launching it.
    """
    kb = KnowledgeBase.open(db_path)

    mcp = FastMCP(
        "JustReadIt Knowledge Base",
        instructions=(
            "You have access to the JustReadIt game knowledge base. "
            "Use 'search_terms' to look up how names/terms were previously "
            "translated before translating them yourself. "
            "Use 'record_term' when you determine the correct translation of a "
            "character name, location or game-specific term. "
            "Use 'record_event' after translating a significant story beat."
        ),
    )

    @mcp.tool()
    def record_term(
        original: str,
        translation: str,
        category: str = "term",
        reading: str = "",
        description: str = "",
    ) -> str:
        """Persist a vocabulary term, character name, location or item.

        Args:
            original: Original-language text (e.g. Japanese: 「アルシア」).
            translation: Your chosen translation (e.g. "Alcia").
            category: One of 'character', 'location', 'item', 'term'.
            reading: Optional romanisation or furigana.
            description: Free-form notes about the term's role or context.

        Returns:
            Confirmation message.
        """
        kb.record_term(
            original=original,
            translation=translation,
            category=category,
            reading=reading,
            description=description,
        )
        return f"Recorded: {original!r} → {translation!r} [{category}]"

    @mcp.tool()
    def record_event(summary: str) -> str:
        """Persist a brief summary of a story event that just occurred.

        Call this after translating a significant scene, battle, reveal or
        character interaction so future translations can reference it.

        Args:
            summary: 2-4 sentence description of what happened, in the target
                language.

        Returns:
            Confirmation message.
        """
        kb.record_event(summary=summary)
        return "Event recorded."

    @mcp.tool()
    def search_terms(query: str, k: int = 5) -> list[dict]:
        """Search the knowledge base for relevant terms and story events.

        Performs hybrid BM25 + vector search.  Use this to look up how a name
        or concept was previously translated before committing to a new
        translation.

        Args:
            query: Natural-language or original-text search string.
            k: Maximum number of results (default 5).

        Returns:
            List of matching entries with kind, original, translation,
            category and description fields.
        """
        hits = kb.search(query, k=k)
        return [
            {
                "kind": h.kind,
                "original": h.original,
                "translation": h.translation,
                "category": h.category,
                "description": h.description,
                "score": round(h.score, 4),
            }
            for h in hits
        ]

    return mcp


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    db_path = args.db or _default_db_path()
    server = build_server(db_path)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
