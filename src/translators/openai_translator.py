# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OpenAI translation backend with rolling summary context agent.

Suitable for dialogue, plot text and long-form content where understanding
previous exchanges improves translation quality.

**Context management** — two-stage rolling window:

1. *Recent history*: the last ``context_window`` (source, translation) pairs
   are appended verbatim to each request so the model understands recent
   exchanges.
2. *Summary agent*: once ``summary_trigger`` total pairs have accumulated,
   the oldest chunk is condensed into a single summary paragraph via a
   dedicated summarisation call.  The summary is injected into the system
   prompt of all subsequent requests instead of the verbose raw history,
   keeping token usage bounded.

Requirements::

    pip install openai

Usage::

    from src.translators.openai_translator import OpenAITranslator

    translator = OpenAITranslator(
        api_key="sk-...",
        system_prompt="You are translating a Japanese fantasy RPG into English.",
        context_window=8,
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

class OpenAITranslator(Translator):
    """Translation backend backed by OpenAI Chat Completions API.

    Args:
        api_key: OpenAI API key (``sk-...``).
        model: Chat model name (default ``"gpt-4o-mini"``).
        system_prompt: User-supplied system prompt prepended to every request.
            Describe the game world, character names, writing style, etc.
        context_window: Number of most-recent (source, translation) pairs
            included verbatim in each request (default 10).
        summary_trigger: When the total history length reaches this value,
            the oldest half is condensed into a summary paragraph (default 20).
        base_url: Override OpenAI-compatible endpoint URL (e.g. local
            proxy or Azure OpenAI deployment).
        timeout: Per-request timeout in seconds (default 30).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-4o-mini",
        system_prompt: str = "",
        context_window: int = 10,
        summary_trigger: int = 20,
        base_url: str | None = None,
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
        self._summary_trigger = summary_trigger
        self._timeout = timeout

        # Rolling history
        self._history: list[_HistoryEntry] = []
        # Condensed summary of older history (replaces raw old entries)
        self._summary: str = ""

    # ── Public API ────────────────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._system_prompt = value

    def clear_context(self) -> None:
        """Discard all accumulated history and summary."""
        self._history.clear()
        self._summary = ""

    # ── Translator ────────────────────────────────────────────────────

    def translate(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "en",
    ) -> str:
        """Translate *text* with rolling context via OpenAI Chat Completions.

        Args:
            text: Source text to translate.
            source_lang: BCP-47 source language code (e.g. ``"ja"``).
            target_lang: BCP-47 target language code (e.g. ``"en"``, ``"zh-CN"``).

        Returns:
            Translated string.

        Raises:
            RuntimeError: If the OpenAI API call fails.
        """
        if not text.strip():
            return text

        src = source_lang
        tgt = target_lang
        messages = self._build_messages(text, src, tgt)
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.3,
                timeout=self._timeout,
            )
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
                raise AuthError(f"OpenAI authentication failed: {exc}") from exc
            if "429" in msg or "rate" in msg.lower():
                raise RateLimitError(f"OpenAI rate limited: {exc}") from exc
            raise TranslationError(f"OpenAI API request failed: {exc}") from exc

        translation: str = response.choices[0].message.content or ""
        translation = translation.strip()

        # Record in history, then trigger summarisation if needed
        self._history.append(_HistoryEntry(source=text, translation=translation))
        if len(self._history) >= self._summary_trigger:
            self._maybe_summarise()

        return translation

    # ── Internal ──────────────────────────────────────────────────────

    def _build_messages(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> list[dict]:
        """Construct the messages list for the chat completions request."""
        system_content = self._build_system_content(source_lang, target_lang)
        messages: list[dict] = [{"role": "system", "content": system_content}]

        # Inject recent history as alternating user/assistant turns
        recent = self._history[-self._context_window:]
        for entry in recent:
            messages.append({"role": "user", "content": entry.source})
            messages.append({"role": "assistant", "content": entry.translation})

        messages.append({"role": "user", "content": text})
        return messages

    def _build_system_content(self, source_lang: str, target_lang: str) -> str:
        """Compose the full system prompt including base prompt and summary."""
        parts: list[str] = []

        # Use stored prompt if set, otherwise fall back to the default template.
        raw_base = self._system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
        # Format {source_lang} / {target_lang} placeholders in the base prompt.
        base = raw_base.format(source_lang=source_lang, target_lang=target_lang)
        parts.append(base)

        if self._summary:
            parts.append(
                "\n\n=== Story context so far (summary) ===\n" + self._summary
            )

        if self._system_prompt.strip():
            # When the user supplied a custom prompt, append the explicit
            # translation instruction so the model always knows its task.
            parts.append(
                f"\n\nTranslate the following text from {source_lang} to {target_lang}. "
                f"Output ONLY the translated text."
            )

        return "\n".join(parts)

    def _maybe_summarise(self) -> None:
        """Summarise the oldest chunk of history to keep context bounded.

        The oldest ``summary_trigger // 2`` entries are condensed into a
        few sentences via a lightweight summarisation call, then removed
        from the raw history list.
        """
        chunk_size = max(1, self._summary_trigger // 2)
        chunk = self._history[:chunk_size]
        self._history = self._history[chunk_size:]

        dialogue = "\n".join(
            f"[{i + 1}] {e.source} → {e.translation}"
            for i, e in enumerate(chunk)
        )
        prompt = (
            "The following are sequential lines from a visual novel, "
            "with their translations.  Write a concise 2-4 sentence summary "
            "of the story events and key information introduced, "
            "to be used as context for future translations.\n\n"
            + dialogue
        )
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful summarisation assistant.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                timeout=self._timeout,
            )
            new_summary = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            warnings.warn(
                f"Context summarisation failed, history chunk discarded: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return

        # Prepend previous summary so we don't lose older context
        if self._summary:
            self._summary = self._summary + "\n" + new_summary
        else:
            self._summary = new_summary
