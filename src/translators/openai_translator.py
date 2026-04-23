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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.translators._installer import ensure_package
from src.translators.base import AuthError, RateLimitError, TranslationError, Translator

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.config import AppConfig
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

# System prompt for mid-range local models (~9 B) that support tool-calling
# but have shallower context budgets — keep instructions tight.
_SYSTEM_PROMPT_9B: str = (
    "You are a Japanese-to-{target_lang} visual novel translator.\n"
    "Rules:\n"
    "- Output ONLY the translated text, no commentary.\n"
    "- Preserve punctuation and line breaks.\n"
    "- Call search_terms before translating unfamiliar names or terms.\n"
    "- Call record_term to save newly encountered character names and game-specific "
    "terms for future reference.\n"
    "- Source: {source_lang}.  Target: {target_lang}."
)

# Ultra-minimal system prompt for small models (~4 B) that cannot reliably
# execute function-calling prompts.
_SYSTEM_PROMPT_4B: str = (
    "Translate the following {source_lang} visual novel text into {target_lang}.\n"
    "Output ONLY the translation. Preserve punctuation and line breaks exactly."
)

# ---------------------------------------------------------------------------
# Preset configurations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenAIPreset:
    """A bundle of recommended settings for a class of OpenAI-compatible model.

    All string fields that are empty mean "keep the current UI value".
    ``system_prompt`` of ``None`` signals "use the built-in default".
    """

    label: str
    """Human-readable name shown in the UI dropdown."""

    description: str
    """One-line hint shown as tooltip."""

    model_placeholder: str
    """Suggested model name (used as QLineEdit placeholder, not written to config)."""

    base_url_placeholder: str
    """Suggested base URL (used as QLineEdit placeholder, not written to config)."""

    tools_enabled: bool
    """Whether KB tool-calling should be enabled."""

    context_window: int
    """Recommended recent-pair context window size."""

    summary_trigger: int
    """History length that triggers oldest-chunk summarisation (0 = off)."""

    system_prompt: str
    """System-prompt template (supports ``{source_lang}``/``{target_lang}``)."""

    disable_thinking: bool = False
    """When ``True``, prepend an empty ``<think></think>`` assistant prefill to
    suppress the model's reasoning phase.  Effective only on local endpoints
    (Ollama / LM Studio) running thinking-capable models such as
    DeepSeek-R1-Distill or QwQ.  Has no effect—and will likely cause an API
    error—on the standard OpenAI endpoint."""


#: Ordered list of ready-made presets offered in the UI.
OPENAI_PRESETS: list[OpenAIPreset] = [
    OpenAIPreset(
        label="强大在线服务（GPT-4o / Claude / Gemini …）",
        description="适合 OpenAI、OpenRouter、Google 等在线旗舰模型。全功能：工具调用+RAG+长上下文。",
        model_placeholder="gpt-4o-mini",
        base_url_placeholder="https://api.openai.com/v1  （留空使用默认）",
        tools_enabled=True,
        context_window=20,
        summary_trigger=40,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        disable_thinking=True,
    ),
    OpenAIPreset(
        label="9B 本地模型（支持工具调用，如 Qwen2.5:9b）",
        description="适合 Ollama/LM Studio 运行的 7–13 B 模型。支持工具调用但上下文较短，精简提示词。",
        model_placeholder="qwen2.5:9b",
        base_url_placeholder="http://localhost:11434/v1",
        tools_enabled=True,
        context_window=6,
        summary_trigger=15,
        system_prompt=_SYSTEM_PROMPT_9B,
        disable_thinking=True,
    ),
    OpenAIPreset(
        label="4B 及以下本地模型（不支持工具调用）",
        description="适合 Ollama/LM Studio 运行的 3–4 B 小模型。禁用工具调用，使用极简提示词。",
        model_placeholder="qwen2.5:3b",
        base_url_placeholder="http://localhost:11434/v1",
        tools_enabled=False,
        context_window=3,
        summary_trigger=0,
        system_prompt=_SYSTEM_PROMPT_4B,
        disable_thinking=True,
    ),
]

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
        disable_thinking: If ``True``, prepend an empty
            ``<think>\n</think>`` assistant message (prefill) to every
            request, suppressing the model's reasoning phase.  Useful for
            local thinking-capable models (DeepSeek-R1-Distill, QwQ, etc.)
            when run via Ollama or LM Studio.  **Do not enable for OpenAI
            endpoints** — the standard API requires the last message to be
            from the user / tool role.
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
        disable_thinking: bool = False,
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

        # Local endpoints (Ollama, LM Studio, etc.) don't need a real key.
        # Use a placeholder so the openai client doesn't raise at construction
        # time — real auth errors will surface as 401 responses from the API.
        effective_key = api_key or "no-key"
        kwargs: dict = {"api_key": effective_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = _openai.OpenAI(**kwargs)
        self._model = model
        self._system_prompt = system_prompt
        self._context_window = context_window
        self._timeout = timeout
        self._kb = knowledge_base
        self._tools_enabled = tools_enabled
        self._disable_thinking = disable_thinking

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
        # Delegate to base template method (calls _translate + normalize_text).
        translation = super().translate(text, source_lang, target_lang)

        # Update short-term context only on confirmed high-quality input.
        if translation and add_to_history:
            self._recent.append(_HistoryEntry(source=text, translation=translation))
            if len(self._recent) > self._context_window:
                self._recent = self._recent[-self._context_window:]

        return translation

    def _do_translate(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "en",
    ) -> str:
        """Core RAG + tool-loop translation (called by base template method)."""
        if not text.strip():
            return text

        # 1. RAG search
        rag_entries = self._rag_search(text)

        # 2. Build messages
        messages = self._build_messages(text, source_lang, target_lang, rag_entries)

        # 3. Tool schemas (only when KB is present and tools enabled)
        tools = self._get_tools() if (self._kb and self._tools_enabled) else None

        # 4. Tool-call loop → final translation
        return self._run_tool_loop(messages, tools)

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
        if self._disable_thinking:
            # Prefill with an empty think block so the model skips its
            # reasoning phase entirely (effective on Ollama / LM Studio;
            # do NOT use with the standard OpenAI endpoint).
            messages.append({"role": "assistant", "content": "<think>\n</think>"})
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


# ---------------------------------------------------------------------------
# Headless factory helper (used by _panel_base.BUILDER_REGISTRY)
# ---------------------------------------------------------------------------

def build_from_config(
    cfg: "AppConfig",
    *,
    progress: "Callable[[str], None] | None" = None,
    knowledge_base: object = None,
) -> OpenAICompatTranslator:
    """Construct an :class:`OpenAICompatTranslator` from *cfg*."""
    oa = cfg.translator.backends.openai
    return OpenAICompatTranslator(
        api_key=oa.api_key.strip(),
        model=oa.model.strip() or "gpt-4o-mini",
        system_prompt=oa.system_prompt,
        context_window=oa.context_window,
        base_url=oa.base_url.strip() or None,
        knowledge_base=knowledge_base,  # type: ignore[arg-type]
        tools_enabled=oa.tools_enabled,
        disable_thinking=oa.disable_thinking,
        progress=progress,
    )
