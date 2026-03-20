# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tool definitions shared between the OpenAI function-calling loop and the
MCP stdio server.

:data:`OPENAI_TOOLS` is the ``tools=`` list passed directly to
``client.chat.completions.create()``.

:func:`execute_tool` dispatches a single tool call to a
:class:`~src.knowledge.KnowledgeBase` instance and returns a JSON-serialisable
result dict.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.knowledge.knowledge_base import KnowledgeBase

# ── OpenAI function-calling schemas ──────────────────────────────────────────

OPENAI_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "record_term",
            "description": (
                "Save a vocabulary term, character name, location, or game-specific "
                "concept to the persistent knowledge base so it can be recalled in "
                "future translation requests.  Call this whenever you encounter a "
                "name or term worth remembering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "original": {
                        "type": "string",
                        "description": "The original-language text (e.g. Japanese).",
                    },
                    "translation": {
                        "type": "string",
                        "description": "Your chosen translation.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["character", "location", "item", "term"],
                        "description": "Kind of entry.  Default 'term'.",
                    },
                    "reading": {
                        "type": "string",
                        "description": (
                            "Optional romanisation or furigana to aid disambiguation."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Free-form notes: role in story, aliases, pronunciation "
                            "quirks, etc."
                        ),
                    },
                },
                "required": ["original", "translation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_event",
            "description": (
                "Save a brief summary of a story event to the knowledge base so "
                "future translations can refer to prior plot points.  Call this "
                "after translating a significant story beat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "2-4 sentence summary of what just happened, written in "
                            "the target language."
                        ),
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_terms",
            "description": (
                "Search the knowledge base for terms, names, or events relevant to "
                "a query.  Use this to look up how a name was previously translated "
                "before committing to a translation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language or original-text search query.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Maximum results to return (default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool executor ─────────────────────────────────────────────────────────────

def execute_tool(kb: "KnowledgeBase", name: str, arguments: str | dict) -> str:
    """Execute a knowledge-base tool call and return a JSON string result.

    Args:
        kb: The :class:`~src.knowledge.KnowledgeBase` instance to operate on.
        name: Tool function name (one of ``record_term``, ``record_event``,
            ``search_terms``).
        arguments: Either the raw JSON string from an OpenAI tool call, or an
            already-parsed ``dict``.

    Returns:
        A JSON string to feed back as the ``tool`` role message content.
    """
    args: dict = json.loads(arguments) if isinstance(arguments, str) else arguments

    if name == "record_term":
        kb.record_term(
            original=args["original"],
            translation=args["translation"],
            category=args.get("category", "term"),
            reading=args.get("reading", ""),
            description=args.get("description", ""),
        )
        return json.dumps({"ok": True, "original": args["original"]})

    if name == "record_event":
        kb.record_event(summary=args["summary"])
        return json.dumps({"ok": True})

    if name == "search_terms":
        hits = kb.search(args["query"], k=int(args.get("k", 5)))
        results = [
            {
                "kind": h.kind,
                "original": h.original,
                "translation": h.translation,
                "category": h.category,
                "description": h.description,
            }
            for h in hits
        ]
        return json.dumps({"results": results})

    return json.dumps({"error": f"Unknown tool: {name}"})
