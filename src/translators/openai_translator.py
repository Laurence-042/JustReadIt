# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OpenAI-compatible API translation backend with KnowledgeBase RAG + tool use.

Works with any endpoint that implements the OpenAI Chat Completions API,
including OpenAI itself, OpenRouter, local Ollama proxies, Azure OpenAI, etc.
Point ``base_url`` at the desired endpoint; the ``api_key`` format is
provider-specific.

Context strategy
----------------
Previous versions injected rolling translation history verbatim into each
request.  This caused context pollution when OCR misread a region and
produced garbled text that was then fed back as if it were clean dialogue.

The current version uses a two-level context strategy that is pollution-safe:

1. **RAG injection (long-term, persistent)**: Before each call, the
   :class:`~src.knowledge.KnowledgeBase` is searched for terms and events
   relevant to the source text.  Only explicitly confirmed knowledge (saved
   via function calls from the model itself) reaches future requests.

2. **Recent pairs (short-term, volatile)**: A small sliding buffer of the
   last ``context_window`` (source, translation) pairs is injected as
   alternating user/assistant turns for immediate conversational continuity.
   Callers control whether a result is added to this buffer via the
   ``add_to_history`` flag on :meth:`translate` — set it to ``False`` when
   OCR confidence is low or memory-scan correction did not succeed.

3. **LLM-driven knowledge capture**: The model receives three function tools
   (``record_term``, ``record_event``, ``search_terms``) and may call them
   during translation.  The translator runs a tool-call loop until the model
   produces a final plain-text response.

Requirements::

    pip install openai
    pip install justreadit[knowledge]   # for KnowledgeBase

Usage::

    from src.translators.openai_translator import OpenAICompatTranslator
    from src.knowledge import KnowledgeBase

    kb = KnowledgeBase.open("alcia.db")
    translator = OpenAICompatTranslator(
        api_key="sk-...",
        # base_url="https://openrouter.ai/api/v1",  # any compatible endpoint
        knowledge_base=kb,
        system_prompt="You are translating a Japanese fantasy RPG into English.",
    )
    result = translator.translate("この剣は伝説の武器だ。", target_lang="en")
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.translators._installer import ensure_package
from src.translators.base import AuthError, RateLimitError, TranslationError, Translator

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.knowledge.knowledge_base import KnowledgeBase

# ---------------------------------------------------------------------------
# Default system prompt (supports {source_lang} / {target_lang} placeholders)
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT: str = (
    "You are a professional Japanese-to-{target_lang} translator specialising in "
    "visual novel games.\n"
    "Rules:\n"
    "- Output ONLY the translated text, with no commentary, notes, or explanations.\n"
    "- Preserve all punctuation, line breaks, and formatting from the source text.\n"
    "- Translate character names phonetically unless a localised name is known.\n"
    "- Keep honorifics (san, kun, chan\u2026) as-is if no natural equivalent exists.\n"
    "- Before translating a name or term, call search_terms to check if it was "
    "already recorded.\n"
    "- When you translate a new character name or game-specific term, call "
    "record_term to save it.\n"
    "- Source language: {source_lang}.  Target language: {target_lang}."
)

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class _HistoryEntry:
    source: str
    translation: str


# ---------------------------------------------------------------------------
# Translator implementation
# ---------------------------------------------------------------------------

class OpenAICompatTranslator(Translator):
    """Translation backend for any OpenAI-compatible Chat Completions API.

    Tested with: OpenAI, OpenRouter, Ollama (OpenAI proxy mode), Azure OpenAI.
    Leave ``base_url`` as ``None`` for the default OpenAI endpoint
    (``https://api.openai.com/v1``).

    Args:
        api_key: Provider API key.  Format is provider-specific
            (``sk-...`` for OpenAI, Bearer token for OpenRouter, etc.).
        model: Model name as accepted by the provider (default ``"gpt-4o-mini"``).
        system_prompt: User-supplied system prompt prepended to every request.
            Describe the game world, character names, writing style, etc.
        context_window: Maximum number of recent (source, translation) pairs
            kept as short-term conversational context (default 3).  Unlike the
            old rolling-window approach, pairs are only added when
            ``add_to_history=True`` is passed to :meth:`translate`, preventing
            OCR garbage from polluting future requests.
        base_url: OpenAI-compatible endpoint URL.  Examples::

                https://openrouter.ai/api/v1          # OpenRouter
                http://localhost:11434/v1              # Ollama
                https://<resource>.openai.azure.com/  # Azure OpenAI

        knowledge_base: Optional :class:`~src.knowledge.KnowledgeBase`.
            When provided:

            * Relevant terms and events are retrieved via hybrid RAG search
              and injected into the system prompt before each call.
            * The model receives ``record_term``, ``record_event`` and
              ``search_terms`` function tools and a tool-call loop handles
              multi-step responses.

        tools_enabled: If ``True`` (default) and *knowledge_base* is set,
            include KB tools in every API call.  Set to ``False`` to use RAG
            injection only (no function calling), useful for models with
            poor function-calling support.
        timeout: Per-request timeout in seconds (default 30).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-4o-mini",
        system_prompt: str = "",
        context_window: int = 3,
        base_url: str | None = None,
        knowledge_base: "KnowledgeBase | None" = None,
        tools_enabled: bool = True,
        timeout: float = 30.0,
        progress: "Callable[[str], None] | None" = None,
    ) -> None:
        ensure_package("openai>=1.0", "openai", progress=progress)
        try:
            import openai as _openai  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "openai is not installed.  Run: pip install openai"
            ) from exc

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = _openai.OpenAI(**kwargs)
        self._model = model
        self._system_prompt = system_prompt
        self._context_window = context_window
        self._timeout = timeout
        self._kb = knowledge_base
        self._tools_enabled = tools_enabled

        # Short-term volatile context — only grows when add_to_history=True.
        self._recent: list[_HistoryEntry] = []

    # ── Public API ────────────────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._system_prompt = value

    def clear_context(self) -> None:
        """Discard all accumulated short-term conversational context."""
        self._recent.clear()

    # ── Translator ────────────────────────────────────────────────────

    def translate(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "en",
        *,
        add_to_history: bool = True,
    ) -> str:
        """Translate *text* using RAG context and optional KB tool calling.

        Args:
            text: Source text to translate.
            source_lang: BCP-47 source language code (e.g. ``"ja"``).
            target_lang: BCP-47 target language code (e.g. ``"en"``,
                ``"zh-CN"``).
            add_to_history: When ``True`` (default), the (source, translation)
                pair is appended to the short-term recent-pairs buffer.  Pass
                ``False`` when OCR confidence is low or the memory-scan
                correction did not succeed, so garbled text does not pollute
                subsequent requests.

        Returns:
            Translated string.

        Raises:
            :class:`~src.translators.base.AuthError`: Authentication failure.
            :class:`~src.translators.base.RateLimitError`: Rate limit hit.
            :class:`~src.translators.base.TranslationError`: Other API error.
        """
        if not text.strip():
            return text

        # 1. RAG search
        rag_entries = self._rag_search(text)

        # 2. Build messages
        messages = self._build_messages(text, source_lang, target_lang, rag_entries)

        # 3. Tool schemas (only when KB is present and tools enabled)
        tools = self._get_tools() if (self._kb and self._tools_enabled) else None

        # 4. Tool-call loop → final translation
        translation = self._run_tool_loop(messages, tools)

        # 5. Update short-term context only on confirmed high-quality input
        if add_to_history:
            self._recent.append(_HistoryEntry(source=text, translation=translation))
            if len(self._recent) > self._context_window:
                self._recent = self._recent[-self._context_window:]

        return translation

    # ── Internal ──────────────────────────────────────────────────────

    def _run_tool_loop(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> str:
        """Drive the tool-call loop until the model returns plain text."""
        create_kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.3,
            "timeout": self._timeout,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "auto"

        _MAX_TOOL_ROUNDS = 8
        for _round in range(_MAX_TOOL_ROUNDS):
            try:
                response = self._client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                msg = str(exc)
                if "401" in msg or "authentication" in msg.lower():
                    raise AuthError(
                        f"OpenAI-compatible API authentication failed: {exc}"
                    ) from exc
                if "429" in msg or "rate" in msg.lower():
                    raise RateLimitError(
                        f"OpenAI-compatible API rate limited: {exc}"
                    ) from exc
                raise TranslationError(
                    f"OpenAI-compatible API request failed: {exc}"
                ) from exc

            choice = response.choices[0]
            msg_obj = choice.message

            # No tool calls → final translation
            if not msg_obj.tool_calls:
                return (msg_obj.content or "").strip()

            # Append assistant message (with tool_calls) to conversation
            messages.append(msg_obj.model_dump(exclude_unset=True))

            # Execute each tool call and append tool-role results
            for tc in msg_obj.tool_calls:
                result = self._dispatch_tool(tc.function.name, tc.function.arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            create_kwargs["messages"] = messages

        # Loop cap exceeded — request final answer without tools
        warnings.warn(
            "OpenAICompatTranslator: tool-call loop cap reached, "
            "requesting final answer without tools.",
            RuntimeWarning,
            stacklevel=3,
        )
        create_kwargs.pop("tools", None)
        create_kwargs.pop("tool_choice", None)
        try:
            response = self._client.chat.completions.create(**create_kwargs)
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            raise TranslationError(
                f"OpenAI-compatible API request failed: {exc}"
            ) from exc

    def _dispatch_tool(self, name: str, arguments: str) -> str:
        if self._kb is None:
            import json
            return json.dumps({"error": "No knowledge base configured."})
        from src.knowledge.tools import execute_tool
        try:
            return execute_tool(self._kb, name, arguments)
        except Exception as exc:
            import json
            warnings.warn(
                f"KB tool {name!r} raised: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return json.dumps({"error": str(exc)})

    def _rag_search(self, text: str) -> list:
        if self._kb is None:
            return []
        try:
            return self._kb.search(text, k=6)
        except Exception as exc:
            warnings.warn(
                f"KnowledgeBase RAG search failed: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return []

    def _build_messages(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        rag_entries: list,
    ) -> list[dict]:
        system_content = self._build_system_content(
            source_lang, target_lang, rag_entries
        )
        messages: list[dict] = [{"role": "system", "content": system_content}]
        for entry in self._recent:
            messages.append({"role": "user", "content": entry.source})
            messages.append({"role": "assistant", "content": entry.translation})
        messages.append({"role": "user", "content": text})
        return messages

    def _build_system_content(
        self,
        source_lang: str,
        target_lang: str,
        rag_entries: list,
    ) -> str:
        raw_base = self._system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
        base = raw_base.format(source_lang=source_lang, target_lang=target_lang)
        parts: list[str] = [base]

        if rag_entries:
            lines: list[str] = []
            for entry in rag_entries:
                if entry.kind == "term":
                    line = (
                        f"- [{entry.category}] {entry.original}"
                        f" → {entry.translation}"
                    )
                    if entry.description:
                        line += f" ({entry.description})"
                    lines.append(line)
                else:
                    lines.append(f"- [story] {entry.description}")
            if lines:
                parts.append(
                    "\n\n=== Relevant knowledge from previous sessions ===\n"
                    + "\n".join(lines)
                )

        return "\n".join(parts)

    def _get_tools(self) -> list[dict]:
        from src.knowledge.tools import OPENAI_TOOLS
        return OPENAI_TOOLS


# Backward-compatible alias
OpenAITranslator = OpenAICompatTranslator
