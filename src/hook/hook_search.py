"""Hook-site discovery for JustReadIt.

Injects ``hook_engine.dll`` into the target process, which bulk-hooks all
function prologues in the game EXE using custom x64 trampolines.  Each
trampoline scans the call-stack frame for CJK UTF-16LE strings.  Results are
forwarded via Named Pipe and ranked by :func:`score_candidate`.

Typical workflow
----------------
1.  Create a :class:`HookSearcher` and call :meth:`~HookSearcher.start`.
2.  Let the user play the game for ~30 s so dialogue functions fire.
3.  Call :meth:`~HookSearcher.ranked_candidates` to get filtered, ranked hits.
4.  Present the list to the user; they pick the best candidate.
5.  Store it as a :class:`HookCode` in ``AppConfig.hook_code``.
6.  Call :meth:`~HookSearcher.filter_to` with the confirmed candidates;
    the live feed is then available via :meth:`~HookSearcher.drain_live_feed`.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.hook._win32 import (
    InjectionError,
    PipeError,
    close_pipe,
    connect_pipe,
    create_pipe_server,
    inject_dll,
    pack_scan_next_command,
    pack_search_config,
    read_pipe_exact,
    unpack_result_hdr,
    write_pipe,
    _RESULT_HDR_SIZE,
)

log = logging.getLogger(__name__)

_DLL_PATH = Path(__file__).parent / "hook_engine.dll"

# ---------------------------------------------------------------------------
# Batch-advance parameters
# ---------------------------------------------------------------------------
# During the scan phase Python advances batches as fast as the DLL can process
# them.  _BATCH_ADVANCE_DELAY_S is only a tiny inter-thread yield so
# _send_next_batch runs on a worker thread rather than the pipe reader thread.
# High-frequency functions are suppressed at the C level (SEND_CALL_LIMIT=150)
# within milliseconds; no artificial settle window is needed.
# Once all pdata is covered the player simply plays the game — dialogue
# functions fire and appear in ranked_candidates().
_BATCH_ADVANCE_DELAY_S: float = 0.05  # inter-thread yield only; not a settle
_DEFAULT_BATCH_SIZE:    int   = 2000  # functions per batch — larger batches finish scanning faster

# ---------------------------------------------------------------------------
# HookCode — serialisable reference to a confirmed hook site
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCode:
    """Identifies a single hook site discovered by :class:`HookSearcher`.

    Attributes
    ----------
    module:
        DLL / EXE name as reported by ``Process.enumerateModules()``,
        e.g. ``"ユニゾンコード.exe"``.
    rva:
        Relative virtual address of the function within the module.
        Stable across process restarts (ASLR changes the base, not the RVA).
    access_pattern:
        Memory-access pattern string as emitted by ``hook_search.js``.
        Examples: ``"r0"``, ``"*(r1+0x14)"``, ``"*(s+0x28)"``.
    encoding:
        ``"utf16"`` or ``"utf8"``.
    """

    module: str
    rva: int
    access_pattern: str
    encoding: str

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_str(self) -> str:
        """Compact string for :class:`~src.config.AppConfig` storage.

        Format::

            <module>!<rva_hex>:<access_pattern>:<encoding>

        Example::

            ユニゾンコード.exe!0x1d6910:*(r0+0x14):utf16
        """
        return f"{self.module}!{self.rva:#x}:{self.access_pattern}:{self.encoding}"

    @classmethod
    def from_str(cls, s: str) -> "HookCode":
        """Parse a string produced by :meth:`to_str`.

        Accepts the legacy format where the access field was a bare digit
        (e.g. ``"...!0x1d6910:0:utf16"``) and converts it to ``"r0"``.

        Raises
        ------
        ValueError
            If the string is malformed.
        """
        try:
            module, rest = s.split("!", 1)
            rva_str, pattern, enc = rest.split(":", 2)
            # Backward-compat: single bare digit was the old arg_index.
            if pattern.isdigit():
                pattern = f"r{pattern}"
            return cls(
                module=module,
                rva=int(rva_str, 16),
                access_pattern=pattern,
                encoding=enc,
            )
        except Exception as exc:
            raise ValueError(f"Malformed HookCode string {s!r}: {exc}") from exc

    def to_hook_config_fields(self) -> tuple[int, int, int, int]:
        """Parse *access_pattern* into ``(arg_idx, deref, byte_offset, encoding_int)``.

        Used to build the C DLL ``Config`` struct for ``MODE_HOOK``.

        Supported patterns
        ------------------
        ``r0``           → (0, 0, 0, enc)
        ``r2``           → (2, 0, 0, enc)
        ``*(r0+0x14)``   → (0, 1, 0x14, enc)
        ``*(r1)``        → (1, 1, 0, enc)
        ``*(s+0x28)``    → (0xFF, 1, 0x28, enc)  stack-relative
        """
        enc_int = 0 if self.encoding == "utf16" else 1
        p = self.access_pattern.strip()

        # Normalise "*(base+off)" → "*base+off"
        if p.startswith("*(") and p.endswith(")"):
            p = "*" + p[2:-1]

        deref = 1 if p.startswith("*") else 0
        if deref:
            p = p[1:]

        base, _, offset_str = p.partition("+")
        byte_offset = int(offset_str, 16) if offset_str else 0

        if base == "s":
            arg_idx = 0xFF
        elif base.startswith("r") and base[1:].isdigit():
            arg_idx = int(base[1:])
        else:
            arg_idx = 0

        return (arg_idx, deref, byte_offset, enc_int)

# ------------------------------------------------------------------
# HookCandidate
# ------------------------------------------------------------------


@dataclass
class HookCandidate:
    """A candidate hook site returned by :class:`HookSearcher`.

    Attributes
    ----------
    module, rva, access_pattern, encoding:
        Same as :class:`HookCode`.
    text:
        A representative text string captured at this site.
    hit_count:
        Number of times this site fired with CJK text.
    score:
        Ranking score; higher is better.
    """

    module: str
    rva: int
    access_pattern: str
    encoding: str
    text: str
    hit_count: int = 0
    score: float = 0.0
    hook_va: int = 0  # absolute VA as received from the pipe; used for confirmed-set filtering

    def to_hook_code(self) -> HookCode:
        return HookCode(
            module=self.module,
            rva=self.rva,
            access_pattern=self.access_pattern,
            encoding=self.encoding,
        )

    def display_label(self) -> str:
        """Short label for UI display (sorted by RVA; no score)."""
        preview = self.text[:60].replace("\n", " ")
        return (
            f"+{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}  {preview!r}"
        )

    def display_label_scored(self) -> str:
        """Label with score prefix, used in the Recommended tab."""
        preview = self.text[:55].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}  {preview!r}"
        )


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

_PATH_RE           = re.compile(r"[A-Za-z]:[/\\]")
_PURE_ASCII_ID_RE  = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_FILE_EXT_RE       = re.compile(r"\.[a-z]{2,5}\b", re.IGNORECASE)
_HEX_BLOB_RE       = re.compile(r"^[0-9a-fA-F]{8,}$")
_QUOTED_DIALOGUE_RE = re.compile(
    r'^[\s\u3000]*[「『""].*[」』""][\s\u3000]*$', re.DOTALL
)

# ---------------------------------------------------------------------------
# Per-language candidate scorers
# ---------------------------------------------------------------------------
# Each scorer encapsulates hard-reject rules and language-specific bonuses.
# Register new languages in ``_LANG_SCORERS``; ``score_candidate`` dispatches
# to the appropriate instance based on the BCP-47 root tag.
# ---------------------------------------------------------------------------

def _shannon_entropy(text: str) -> float:
    """Character-level Shannon entropy in bits."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Unicode ranges used for cross-script rejection.
_HANGUL_RANGES: list[tuple[int, int]] = [
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x3130, 0x318F),   # Hangul Compatibility Jamo
    (0xA960, 0xA97F),   # Hangul Jamo Extended-A
    (0xAC00, 0xD7A3),   # Hangul Syllables
    (0xD7B0, 0xD7FF),   # Hangul Jamo Extended-B
]
_KANA_RANGES: list[tuple[int, int]] = [
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0x31F0, 0x31FF),   # Katakana Phonetic Extensions
    (0xFF65, 0xFF9F),   # Halfwidth Katakana
]


class _CandidateScorer:
    """Language-agnostic scorer used as the base class and default fallback.

    Subclass and override :meth:`_language_bonus` to add language-specific
    multipliers on top of the shared base logic.
    """

    # Codepoint ranges for scripts this language should *never* contain.
    # Any character in these ranges causes an immediate score-0 rejection.
    _reject_script_ranges: list[tuple[int, int]] = []

    # ── hard-reject helpers ────────────────────────────────────────────

    def _hard_reject(self, text: str) -> bool:
        """Return True if *text* should be immediately discarded."""
        # Non-printable control characters (null residue, ESC sequences …)
        # Allow only common whitespace: \t (0x09), \n (0x0A), \r (0x0D).
        if any(ord(c) < 0x20 and c not in '\t\n\r' for c in text):
            return True
        # Binary / high-entropy garbage
        if _shannon_entropy(text) > 5.5:
            return True
        # File / resource paths
        if _PATH_RE.search(text):
            return True
        stripped = text.strip()
        # Pure ASCII identifiers (internal IDs, enum names …)
        if _PURE_ASCII_ID_RE.match(stripped):
            return True
        # File extensions with short text → likely a filename token
        if _FILE_EXT_RE.search(stripped) and len(stripped) < 60:
            return True
        # Hex blobs
        if _HEX_BLOB_RE.match(stripped):
            return True
        # Cross-script rejection: a string containing characters from a
        # script foreign to the active OCR language is almost certainly
        # a false positive (e.g. Hangul on the stack in a Japanese game).
        if self._reject_script_ranges:
            for ch in text:
                cp = ord(ch)
                if any(lo <= cp <= hi for lo, hi in self._reject_script_ranges):
                    return True
        return False

    # ── base CJK check + scoring ───────────────────────────────────────

    def _base_score(self, text: str) -> float:
        """Shared base score before any language bonuses.

        Returns 0.0 if the text contains no CJK characters at all.
        """
        cjk_chars = sum(
            1 for c in text
            if "\u3000" <= c <= "\u9FFF" or "\uFF00" <= c <= "\uFFEF"
        )
        if cjk_chars == 0:
            return 0.0
        score = float(len(text))
        # Reward high CJK density (dialogue vs. mixed noise)
        cjk_ratio = cjk_chars / max(len(text), 1)
        score *= 1.0 + cjk_ratio
        # Strong bonus for bracket- or quote-delimited dialogue
        if _QUOTED_DIALOGUE_RE.match(text):
            score *= 2.5
        return score

    # ── language bonus (overridden by subclasses) ──────────────────────

    def _language_bonus(self, text: str) -> float:  # noqa: ARG002
        """Return a multiplier ≥ 1.0 for language-specific dialogue signals.

        The default implementation returns 1.0 (no bonus).
        """
        return 1.0

    # ── public entry point ──────────────────────────────────────────────

    def __call__(self, text: str) -> float:
        """Return quality score ≥ 0.  Zero means 'discard'."""
        if not text or self._hard_reject(text):
            return 0.0
        base = self._base_score(text)
        if base <= 0.0:
            return 0.0
        return base * self._language_bonus(text)


class _JaCandidateScorer(_CandidateScorer):
    """Japanese-specific scorer.

    Adds dialogue-indicator bonuses on top of the shared base:

    * **Particle bonus** — grammatical particles (は/の/に/も/を/と/が/で/
      へ/か/な/よ/ね/わ) appear in virtually every natural sentence but not
      in short menu labels.  3+ distinct particles → ×3.0; 1–2 → ×1.8.
    * **Sentence-end bonus** — 。？！ at the end of the string → ×1.3.
    * **Menu penalty** — very short (≤6 chars) all-CJK string with no
      particles → ×0.6 (likely a menu label, deprioritise).

    Cross-script: Hangul characters → immediate score 0.
    """

    _reject_script_ranges = _HANGUL_RANGES

    # Hiragana function particles that appear in almost all natural sentences
    # but are rare in menu labels / short UI strings.
    _PARTICLES: frozenset[str] = frozenset("はのにもをとがでへかなよねわ")
    _SENTENCE_END: frozenset[str] = frozenset("。？！")

    def _language_bonus(self, text: str) -> float:
        bonus = 1.0

        distinct_particles = sum(1 for p in self._PARTICLES if p in text)
        if distinct_particles >= 3:
            bonus *= 3.0   # strong dialogue signal
        elif distinct_particles >= 1:
            bonus *= 1.8   # mild dialogue signal

        # Sentence-final punctuation
        stripped = text.rstrip()
        if stripped and stripped[-1] in self._SENTENCE_END:
            bonus *= 1.3

        # Menu-label penalty: very short, no particles
        if distinct_particles == 0 and len(text.strip()) <= 6:
            bonus *= 0.6

        return bonus


class _ZhCandidateScorer(_CandidateScorer):
    """Chinese-specific scorer — rejects Hangul, base scoring only."""
    _reject_script_ranges = _HANGUL_RANGES


class _KoCandidateScorer(_CandidateScorer):
    """Korean-specific scorer — rejects kana, base scoring only."""
    _reject_script_ranges = _KANA_RANGES


# BCP-47 root tag → scorer instance.
_LANG_SCORERS: dict[str, _CandidateScorer] = {
    "ja": _JaCandidateScorer(),
    "zh": _ZhCandidateScorer(),
    "ko": _KoCandidateScorer(),
}
_DEFAULT_SCORER = _CandidateScorer()


def score_candidate(text: str, ocr_lang: str = "") -> float:
    """Return a quality score ≥ 0.  Score 0 means 'discard'.

    Dispatches to the appropriate :class:`_CandidateScorer` subclass based
    on *ocr_lang* (BCP-47 root tag, e.g. ``"ja"``).  Language-specific
    bonuses reward dialogue text over short menu labels; see the scorer
    subclasses for details.
    """
    root = ocr_lang.split("-")[0].lower() if ocr_lang else ""
    return _LANG_SCORERS.get(root, _DEFAULT_SCORER)(text)


# ---------------------------------------------------------------------------
# HookSearcher
# ---------------------------------------------------------------------------


# Minimum score for a candidate to appear in the "Recommended" tab.
_RECOMMEND_SCORE_THRESHOLD: float = 60.0

# How long to wait for the injected DLL to connect before assuming it is a
# stale leftover from a previous session (20 s is generous; normally < 1 s).
_CONNECT_TIMEOUT_S: float = 20.0


class HookSearchError(RuntimeError):
    """Raised when the hook-search session cannot be started."""


@dataclass
class FoundString:
    """Kept for API compatibility; not populated in the DLL-based search."""
    address: str
    encoding: str
    text: str


class HookSearcher:
    """Hook-site discovery via native DLL injection (hook_engine.dll).

    Workflow
    --------
    1. :meth:`start` injects ``hook_engine.dll`` into the game and starts
       the bulk prologue scanner.  All threads in the game are briefly
       suspended while the patches are written.
    2. As the game runs, hooked functions fire.  Any call-stack slot that
       contains a valid CJK UTF-16LE pointer is reported back via a Named
       Pipe and accumulated as a :class:`HookCandidate`.
    3. Call :meth:`ranked_candidates` to get the current list sorted by
       score.  The user picks the best candidate and confirms.
    4. :meth:`stop` unloads the DLL (which restores all patches).

    Phase 2 (triggered by calling :meth:`watch`):
        ``MemoryAccessMonitor`` is armed on the selected addresses.
        The game render loop fires it within one frame.
        Hook candidates are reported via :attr:`ranked_candidates`.

    Parameters
    ----------
    pid:
        Target process ID.
    max_candidates:
        Stop after this many unique hook sites.
    max_strings:
        Cap on CJK strings reported in Phase 1.

    Usage::

        searcher = HookSearcher(pid=12345)
        searcher.start()
        timeot = searcher.wait_for_scan()  # waits for bulk patch to complete
        # play the game ~30 s …
        candidates = searcher.ranked_candidates()
        searcher.stop()
    """

    def __init__(
        self,
        pid: int,
        *,
        max_candidates: int = 50,
        max_hooks: int = 0,  # 0 = scan all pdata entries
        batch_size: int = _DEFAULT_BATCH_SIZE,
        ocr_lang: str = "",
    ) -> None:
        self._pid        = pid
        self._max_c      = max_candidates
        self._max_hooks  = max_hooks
        self._batch_size = batch_size
        self._ocr_lang   = ocr_lang

        self._h_pipe: int = 0
        self._stop       = threading.Event()
        self._candidates: dict[str, HookCandidate] = {}
        self._diags:      list[str]                = []
        self._scan_done  = threading.Event()
        self._lock       = threading.Lock()
        self._write_lock = threading.Lock()  # guards pipe writes (any thread)
        self._reader:     threading.Thread | None  = None
        # Batch-advance state (no settle timer — advance fires immediately)
        self._batch_exhausted: bool              = False
        self._advance_thread:  threading.Timer | None = None
        # Confirmed-address live feed (populated after filter_to())
        self._confirmed_vas: set[int] = set()
        self._live_feed:     list[str] = []
        # Process-exit / connect-timeout detection
        self._pipe_connected  = threading.Event()
        self._process_died:  bool                   = False
        self._connect_watchdog: threading.Timer | None = None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Create Named Pipe, inject hook_engine.dll, begin scanning.

        Raises
        ------
        HookSearchError
            If the DLL is missing, injection fails, or the pipe fails.
        """
        try:
            self._h_pipe = create_pipe_server(self._pid)
        except OSError as exc:
            raise HookSearchError(f"Named Pipe creation failed: {exc}") from exc

        # Inject MinHook.x64.dll first so hook_engine.dll's implicit import resolves
        # (the game's DLL search path doesn't include src/hook/ by default).
        _minhook_path = _DLL_PATH.parent / "MinHook.x64.dll"
        if _minhook_path.exists():
            try:
                inject_dll(self._pid, _minhook_path)
            except InjectionError as exc:
                close_pipe(self._h_pipe)
                self._h_pipe = 0
                raise HookSearchError(
                    f"MinHook.x64.dll pre-injection failed: {exc}"
                ) from exc

        try:
            inject_dll(self._pid, _DLL_PATH)
        except InjectionError as exc:
            close_pipe(self._h_pipe)
            self._h_pipe = 0
            raise HookSearchError(str(exc)) from exc

        self._reader = threading.Thread(
            target=self._reader_loop, name="hook-search-reader", daemon=True
        )
        self._reader.start()

        # Watchdog: if the DLL hasn't connected within _CONNECT_TIMEOUT_S the
        # game likely already has a stale DLL from a previous session whose
        # worker thread is dead.  Close the pipe so _reader_loop unblocks.
        self._connect_watchdog = threading.Timer(
            _CONNECT_TIMEOUT_S, self._on_connect_timeout
        )
        self._connect_watchdog.daemon = True
        self._connect_watchdog.start()
        log.info("HookSearcher started for PID %d", self._pid)

    def stop(self) -> None:
        """Close the pipe and stop the reader thread.  Safe to call many times."""
        self._stop.set()
        # Cancel watchdog and advance threads before closing the pipe.
        wdog = None
        with self._lock:
            t = self._advance_thread
            self._advance_thread = None
            wdog = self._connect_watchdog
            self._connect_watchdog = None
        if t is not None:
            t.cancel()
        if wdog is not None:
            wdog.cancel()
        if self._h_pipe:
            try:
                close_pipe(self._h_pipe)
            except Exception:
                pass
            self._h_pipe = 0
        # Evict the module cache for this PID so a fresh attach gets current
        # module layout (guards against PID reuse and stale ASLR bases).
        _module_cache.pop(self._pid, None)
        log.info("HookSearcher stopped")

    def wait_for_scan(self, timeout: float = 30.0) -> bool:
        """Block until the bulk-patch phase completes (or *timeout* seconds)."""
        return self._scan_done.wait(timeout)

    @property
    def process_died(self) -> bool:
        """``True`` if the game process closed unexpectedly while the searcher
        was active (pipe broken without an explicit :meth:`stop` call)."""
        return self._process_died

    @property
    def pipe_connected(self) -> bool:
        """``True`` once the injected DLL has connected to the Named Pipe."""
        return self._pipe_connected.is_set()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def ranked_candidates(self) -> list[HookCandidate]:
        """Return all candidates sorted by RVA ascending (stable, snapshot)."""
        with self._lock:
            return sorted(self._candidates.values(), key=lambda c: c.rva)

    def recommended_candidates(self) -> list[HookCandidate]:
        """Return candidates with score >= :data:`_RECOMMEND_SCORE_THRESHOLD`,
        sorted by score descending.  These are the most likely dialogue hooks."""
        with self._lock:
            cands = [
                c for c in self._candidates.values()
                if c.score >= _RECOMMEND_SCORE_THRESHOLD
            ]
        return sorted(cands, key=lambda c: -c.score)

    def diags(self) -> list[str]:
        """Return all diagnostic messages received so far."""
        with self._lock:
            return list(self._diags)

    @property
    def scan_complete(self) -> bool:
        """``True`` once the DLL has finished installing all patches."""
        return self._scan_done.is_set()

    @property
    def found_strings(self) -> list[FoundString]:
        """Not used in DLL-based search; always returns empty list."""
        return []

    def watch(self, addresses: list[str]) -> None:  # noqa: ARG002
        """No-op: watch phase not applicable with bulk-hook search."""

    # ------------------------------------------------------------------
    # Pipe reader loop (background thread)
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Connect pipe, send config, then read result messages until closed."""
        try:
            connect_pipe(self._h_pipe)
        except (PipeError, OSError) as exc:
            if not self._stop.is_set():
                # Pipe closed before connect — most likely the connect watchdog
                # fired because the DLL is a stale leftover from a previous
                # session.  The watchdog already logged the message.
                with self._lock:
                    self._diags.append(f"Pipe connect failed: {exc}")
            log.error("HookSearcher pipe connect failed: %s", exc)
            return

        # DLL connected — cancel the watchdog so it doesn't fire.
        self._pipe_connected.set()
        with self._lock:
            wdog = self._connect_watchdog
            self._connect_watchdog = None
        if wdog is not None:
            wdog.cancel()

        # Send search config to DLL
        try:
            write_pipe(self._h_pipe, pack_search_config(self._max_hooks, self._batch_size))
        except (PipeError, OSError) as exc:
            with self._lock:
                self._diags.append(f"Config write failed: {exc}")
            return

        # Read result messages until pipe closes or stop is signalled
        while not self._stop.is_set():
            hdr_bytes = read_pipe_exact(self._h_pipe, _RESULT_HDR_SIZE)
            if hdr_bytes is None:
                break

            hook_va, slot_i, encoding, text_len = unpack_result_hdr(hdr_bytes)

            if text_len == 0:
                continue
            text_bytes = read_pipe_exact(self._h_pipe, text_len)
            if text_bytes is None:
                break

            try:
                text = (
                    text_bytes.decode("utf-16-le", errors="ignore")
                    if encoding == 0
                    else text_bytes.decode("utf-8", errors="ignore")
                )
            except Exception:
                continue

            # hook_va == 0 → control message (not a text hit)
            if hook_va == 0:
                self._handle_control(text)
                continue

            self._handle_hit(hook_va, slot_i, encoding, text)
            # Culls are flushed in bulk by the batch-settle timer (_send_next_batch)
            # rather than per-hit, to avoid starving the timer thread of _write_lock.

        # Distinguish intentional stop from unexpected pipe break (game exit).
        if not self._stop.is_set():
            self._process_died = True
            with self._lock:
                self._diags.append(
                    "\u26a0 Game process closed — pipe disconnected."
                )
            log.info("HookSearcher: game process closed (pipe broken for PID %d)", self._pid)
        log.debug("HookSearcher reader loop exiting")

    def _on_connect_timeout(self) -> None:
        """Called if the DLL has not connected within ``_CONNECT_TIMEOUT_S``.

        The most likely cause: ``hook_engine.dll`` was already loaded by the
        game from a previous JustReadIt session.  ``LoadLibraryA`` increments
        the DLL's reference count without re-running ``DllMain``, so the
        worker thread that connects to the pipe was never started again.
        Closing the pipe unblocks ``ConnectNamedPipe`` in :meth:`_reader_loop`.
        """
        if self._pipe_connected.is_set():
            return   # connected in the meantime; nothing to do
        with self._lock:
            self._diags.append(
                "\u26a0 DLL did not connect within "
                f"{_CONNECT_TIMEOUT_S:.0f} s.  The game likely already has a "
                "stale hook DLL from a previous session — restart the game and "
                "try again."
            )
        log.warning(
            "HookSearcher connect timeout for PID %d — closing pipe", self._pid
        )
        self.stop()

    def _handle_control(self, text: str) -> None:
        if text.startswith("scan_done:"):
            # Format: "scan_done:N@pos"  (pos = pdata index after batch)
            try:
                payload = text.split(":", 1)[1]
                parts   = payload.split("@", 1)
                newly   = int(parts[0])
                pos     = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                newly = 0
                pos   = 0
            with self._lock:
                if newly > 0:
                    self._diags.append(
                        f"Batch complete — {newly} hook(s) installed "
                        f"(total: {sum(1 for _ in self._candidates)}, pdata @{pos})"
                    )
                else:
                    self._diags.append(
                        f"All .pdata entries exhausted at @{pos} — scan complete."
                    )
            # Mark first batch done so UI shows “play game” status
            self._scan_done.set()
            if newly > 0 and not self._stop.is_set():
                self._schedule_next_batch()
            else:
                self._batch_exhausted = True
            log.info("HookSearcher batch: %d newly, pdata_pos=%d", newly, pos)
        elif text.startswith("disabled:"):
            log.debug("DLL confirmed: %s", text)
        elif text.startswith("ERROR:"):
            with self._lock:
                self._diags.append(text)
            log.error("DLL error: %s", text)
        else:
            with self._lock:
                self._diags.append(text)

    def _schedule_next_batch(self) -> None:
        """Immediately schedule CMD_SCAN_NEXT on a worker thread (tiny yield only)."""
        if self._stop.is_set() or self._batch_exhausted:
            with self._lock:
                self._diags.append(
                    f"_schedule_next_batch skipped: stop={self._stop.is_set()} "
                    f"exhausted={self._batch_exhausted}"
                )
            return
        t = threading.Timer(_BATCH_ADVANCE_DELAY_S, self._send_next_batch)
        t.daemon = True
        with self._lock:
            old = self._advance_thread
            self._advance_thread = t
        if old is not None:
            old.cancel()
        t.start()

    def _send_next_batch(self) -> None:
        """Called by advance thread: send CMD_SCAN_NEXT."""
        with self._lock:
            self._diags.append(
                f"Advancing: stop={self._stop.is_set()} pipe={self._h_pipe}"
            )
        if self._stop.is_set() or not self._h_pipe:
            return
        with self._write_lock:
            if self._stop.is_set() or not self._h_pipe:
                with self._lock:
                    self._diags.append("CMD_SCAN_NEXT aborted (pipe closed)")
                return
            try:
                write_pipe(self._h_pipe, pack_scan_next_command(self._batch_size))
                log.info("HookSearcher: sent CMD_SCAN_NEXT batch_size=%d", self._batch_size)
            except (PipeError, OSError) as exc:
                with self._lock:
                    self._diags.append(f"CMD_SCAN_NEXT write failed: {exc}")
                log.warning("Failed to send CMD_SCAN_NEXT: %s", exc)

    def filter_to(self, candidates: list["HookCandidate"]) -> None:
        """Set the confirmed address set so the live feed receives their texts.

        All hooks continue running in the DLL.  The player can call
        ``filter_to`` again at any time to add or change confirmed candidates
        without losing data.

        After this call, texts from confirmed addresses are queued in the live
        feed and can be drained with :meth:`drain_live_feed`.

        Safe to call on the UI thread; can be called multiple times.
        """
        confirmed_vas: set[int] = {c.hook_va for c in candidates if c.hook_va}
        with self._lock:
            self._confirmed_vas = confirmed_vas
        log.info("filter_to: live feed watching %d address(es)", len(confirmed_vas))

    def drain_live_feed(self) -> list[str]:
        """Return and clear all texts queued since the last drain.

        Only populated after :meth:`filter_to` is called.  Returns an empty
        list if no confirmed addresses have fired since the last call.
        """
        with self._lock:
            texts = list(self._live_feed)
            self._live_feed.clear()
        return texts

    def _handle_hit(self, hook_va: int, slot_i: int,
                     encoding: int, text: str) -> None:
        """Build / update a HookCandidate from a single pipe result.

        High-frequency functions are suppressed and auto-disabled by the DLL
        (SEND_CALL_LIMIT).  Python only sees results that passed the C-level
        gate, so no additional frequency filtering is needed here.
        """
        s = score_candidate(text, self._ocr_lang)
        if s <= 0:
            return

        # Map slot index to HookCode access_pattern.
        #
        # Trampoline push order (from the s_tpl template in hook_engine.c):
        #   pushfq, rax, rbx, rcx, rdx, rsp, rbp, rsi, rdi,
        #   r8, r9, r10, r11, r12, r13, r14, r15  (17 values)
        #   sub rsp, 0x20  (xmm shadow space)
        #   lea rcx, [rsp+0xa8]  ← points to entry RSP (== stack param)
        #
        # stack[i] = *(entry_RSP + i*8)
        #   stack[ 0] = return address
        #   stack[-1] = rflags    stack[-2] = rax     stack[-3] = rbx
        #   stack[-4] = rcx (arg0)  stack[-5] = rdx (arg1)
        #   stack[-6] = rsp (saved)  stack[-7] = rbp
        #   stack[-8] = rsi    stack[-9] = rdi
        #   stack[-10] = r8 (arg2)  stack[-11] = r9 (arg3)
        #   stack[-12..-17] = r10-r15
        #   stack[1..N] = caller stack frame
        _reg_to_pattern: dict[int, str] = {
            -4:  "r0",   # rcx = arg0
            -5:  "r1",   # rdx = arg1
            -10: "r2",   # r8  = arg2
            -11: "r3",   # r9  = arg3
        }
        if slot_i in _reg_to_pattern:
            pattern = _reg_to_pattern[slot_i]
        elif slot_i >= 1:
            pattern = f"s+{slot_i * 8:#x}"
        else:
            return   # saved non-arg register or return addr — ignore

        enc_str = "utf16" if encoding == 0 else "utf8"

        # Module / RVA: we don't know the module from the VA alone in Python.
        # Use the VA directly; the module field is filled as best-effort.
        module = _resolve_module(hook_va, self._pid)
        if module is None:
            return  # not in any known module
        base, name = module
        rva = hook_va - base

        key = f"{rva:#x}:{pattern}"
        with self._lock:
            if key in self._candidates:
                c = self._candidates[key]
                c.hit_count += 1
                if len(text) > len(c.text):
                    c.text = text
                if not c.hook_va:
                    c.hook_va = hook_va
            else:
                self._candidates[key] = HookCandidate(
                    module=name,
                    rva=rva,
                    access_pattern=pattern,
                    encoding=enc_str,
                    text=text,
                    hit_count=1,
                    score=s,
                    hook_va=hook_va,
                )
            # If this address is in the confirmed set, add to the live feed.
            if self._confirmed_vas and hook_va in self._confirmed_vas:
                self._live_feed.append(text)
                if len(self._live_feed) > 2048:
                    self._live_feed = self._live_feed[-2048:]


# ---------------------------------------------------------------------------
# Module resolver (VA → module name + base)
# ---------------------------------------------------------------------------

import ctypes as _ctypes
import ctypes.wintypes as _wt

_psapi = _ctypes.WinDLL("psapi", use_last_error=True)
_psapi.EnumProcessModules.restype  = _wt.BOOL
_psapi.EnumProcessModules.argtypes = [_wt.HANDLE, _ctypes.POINTER(_ctypes.c_void_p),
                                       _wt.DWORD, _ctypes.POINTER(_wt.DWORD)]
_psapi.GetModuleFileNameExW.restype  = _wt.DWORD
_psapi.GetModuleFileNameExW.argtypes = [_wt.HANDLE, _ctypes.c_void_p, _wt.LPWSTR, _wt.DWORD]
_psapi.GetModuleInformation.restype  = _wt.BOOL
_psapi.GetModuleInformation.argtypes = [_wt.HANDLE, _ctypes.c_void_p,
                                         _ctypes.c_void_p, _wt.DWORD]

_k32p = _ctypes.WinDLL("kernel32", use_last_error=True)
_k32p.OpenProcess.restype  = _wt.HANDLE
_k32p.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
_k32p.CloseHandle.restype  = _wt.BOOL
_k32p.CloseHandle.argtypes = [_wt.HANDLE]

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ           = 0x0010
# pid → [(base, end_exclusive, name), ...]  sorted by base
_module_cache: dict[int, list[tuple[int, int, str]]] = {}


def _resolve_module(va: int, pid: int) -> tuple[int, str] | None:
    """Return ``(base, module_name)`` for a VA in the given process.

    Results are cached per PID.  Returns ``None`` if the VA is not inside
    any loaded module.
    """
    if pid not in _module_cache:
        entries: list[tuple[int, int, str]] = []
        h = _k32p.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
                               False, pid)
        if not h:
            _module_cache[pid] = entries
            return None
        try:
            needed = _wt.DWORD(0)
            _psapi.EnumProcessModules(h, None, 0, _ctypes.byref(needed))
            count = needed.value // _ctypes.sizeof(_ctypes.c_void_p)
            mods  = (_ctypes.c_void_p * count)()
            _psapi.EnumProcessModules(h, mods, needed, _ctypes.byref(needed))

            class _MI(_ctypes.Structure):
                _fields_ = [("lpBaseOfDll", _ctypes.c_void_p),
                            ("SizeOfImage",  _wt.DWORD),
                            ("EntryPoint",   _ctypes.c_void_p)]

            buf = _ctypes.create_unicode_buffer(260)
            for mod_val in mods:
                if not mod_val:
                    continue
                mi = _MI()
                if _psapi.GetModuleInformation(h, mod_val, _ctypes.byref(mi),
                                               _ctypes.sizeof(mi)):
                    base = mi.lpBaseOfDll or 0
                    end  = base + mi.SizeOfImage  # exclusive upper bound
                    _psapi.GetModuleFileNameExW(h, mod_val, buf, 260)
                    name = Path(buf.value).name
                    entries.append((base, end, name))
        finally:
            _k32p.CloseHandle(h)
        entries.sort()  # sort by base for determinism
        _module_cache[pid] = entries

    for base, end, name in _module_cache[pid]:
        if base <= va < end:
            return (base, name)
    return None
