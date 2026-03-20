# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Game knowledge base — persistent store of terms, names and story events.

Provides hybrid retrieval (SQLite FTS5 BM25 + optional vector cosine
similarity, combined via Reciprocal Rank Fusion) so that an LLM translator
can recall relevant definitions without being limited by a fixed context
window.

Usage::

    from src.knowledge import KnowledgeBase

    kb = KnowledgeBase.open("game_save.db")
    kb.record_term("アルシア", "Alcia", category="character",
                   description="The protagonist's childhood friend.")
    results = kb.search("アルシア")  # → [KnowledgeEntry(...), ...]
    kb.close()
"""
from __future__ import annotations

from src.knowledge.knowledge_base import KnowledgeBase, KnowledgeEntry
from src.knowledge.tools import OPENAI_TOOLS, execute_tool

__all__ = ["KnowledgeBase", "KnowledgeEntry", "OPENAI_TOOLS", "execute_tool"]
