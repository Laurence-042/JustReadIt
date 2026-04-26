# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Centralised text normalisation utilities.

All text that enters the JustReadIt pipeline — from memory scan results,
translator output, user-authored CSV annotations, or LLM-generated knowledge
base entries — should pass through :func:`normalize_text` before being stored
or compared.  This single choke-point ensures consistency and makes future
normalisation rules easy to add in one place.

Usage::

    from src.text_utils import normalize_text

    clean = normalize_text(raw)
"""
from __future__ import annotations


def normalize_text(text: str) -> str:
    """Normalise *text* to a canonical form for storage and comparison.

    Transformations applied (in order):

    1. **Line endings** — ``\\r\\n`` and bare ``\\r`` are collapsed to ``\\n``.
       Memory-resident strings decoded from UTF-16LE on Windows often contain
       ``\\r\\n``; Windows users writing CSV annotations also produce ``\\r\\n``
       via standard text editors.

    Add further rules here as needed.  Every call site benefits automatically.

    Scope and intentional non-goals
    -------------------------------
    This function is the single, generic OCR-text normaliser.  It must
    stay **engine-agnostic** — see the "通用工具原则" note in
    ``.github/copilot-instructions.md``.

    Allowed extensions (safe candidates for future addition):

    * Full-/half-width ASCII symbol mapping (``ＡＢＣ`` → ``ABC``).
    * The *safe subset* of Unicode NFKC compatibility decomposition
      that does not collapse semantically distinct CJK characters.

    Not allowed here:

    * **Script placeholder handling** — ``%s``, ``{name}``, literal
      ``\\n`` strings, ``[r]``, ``@n`` and similar engine-specific
      escape forms must be processed in an engine profile layer, not in
      a generic normaliser.
    * **Lossy normalisation that perturbs Levenshtein distance signal**
      — punctuation synonym folding (``、`` ↔ ``,``), prolonged-sound-mark
      collapsing (``ー`` ↔ ``-``), katakana/hiragana folding, etc.
      These break OCR ↔ memory text matching by erasing real differences.
    """
    # Step 1: normalise line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    return text
