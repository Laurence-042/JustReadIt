"""Automatic hook-site discovery for JustReadIt.

Attaches a broad-net Frida instrumentation script to the target process,
intercepts candidate functions that pass CJK text through argument registers,
and returns a ranked list of :class:`HookCandidate` instances.

Typical workflow
----------------
1.  Create a :class:`HookSearcher` and call :meth:`~HookSearcher.start`.
2.  Let the user play the game for ~30 s so text passes through the hooks.
3.  Call :meth:`~HookSearcher.ranked_candidates` to get filtered, ranked hits.
4.  Present the list to the user; they pick the best one.
5.  Store it as a :class:`HookCode` in ``AppConfig.hook_code``.
6.  Pass the :class:`HookCode` to :meth:`TextHook.attach` for future runs.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frida
import frida.core

log = logging.getLogger(__name__)

_SEARCH_SCRIPT_JS: str = (Path(__file__).parent / "hook_search.js").read_text(
    encoding="utf-8"
)

# ---------------------------------------------------------------------------
# HookCode — serialisable reference to a confirmed hook site
# ---------------------------------------------------------------------------


def _access_expr_to_js(pattern: str, read_fn: str) -> str:
    """Translate an *access_pattern* string to a Frida JS read-expression.

    Pattern syntax (mirrors what ``hook_search.js`` sends):

    =========== =============================================
    Pattern     Frida expression produced
    =========== =============================================
    ``r0``      ``args[0].readUtf16String(1024)``
    ``*r1``     ``args[1].readPointer().readUtf16String(1024)``
    ``r2+0x14`` ``args[2].add(0x14).readUtf16String(1024)``
    ``*(r3+0x14)`` ``args[3].add(0x14).readPointer().readUtf16String(1024)``
    ``*(s+0x28)`` ``this.context.rsp.add(0x28).readPointer().readUtf16String(1024)``
    =========== =============================================
    """
    p = pattern.strip()
    # Normalise "*(base+off)" → "*base+off" so the generic parser handles it.
    if p.startswith("*(") and p.endswith(")"):
        p = "*" + p[2:-1]

    deref = p.startswith("*")
    if deref:
        p = p[1:]

    base_part, _, offset_part = p.partition("+")

    if base_part.startswith("r") and base_part[1:].isdigit():
        expr = f"args[{int(base_part[1:])}]"
    elif base_part == "s":
        expr = "this.context.rsp"
    else:
        raise ValueError(f"Unrecognised base in access_pattern {pattern!r}: {base_part!r}")

    if offset_part:
        expr += f".add({offset_part})"
    if deref:
        expr += ".readPointer()"
    expr += f".{read_fn}(1024)"
    return expr


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

    def to_js(self) -> str:
        """Generate a standalone Frida script that hooks this site only."""
        read_fn = "readUtf16String" if self.encoding == "utf16" else "readUtf8String"
        read_expr = _access_expr_to_js(self.access_pattern, read_fn)
        return _ADDRESS_HOOK_TEMPLATE.format(
            module=self.module,
            rva=self.rva,
            read_expr=read_expr,
            encoding=self.encoding,
            access_pattern=self.access_pattern,
        )


# Frida script template used when a confirmed HookCode is already known.
# Placeholders: {module}, {rva} (integer), {read_expr} (full JS read expression),
# {encoding}, {access_pattern}.
_ADDRESS_HOOK_TEMPLATE = """\
'use strict';
var _lastText = '';
function _onText(str) {{
    if (!str || str.length === 0) return;
    var t = str.replace(/^\\s+|\\s+$/g, '');
    if (t.length === 0) return;
    if (t === _lastText) return;
    _lastText = t;
    send({{ type: 'text', value: t }});
}}
var _mod = Process.getModuleByName('{module}');
var _addr = _mod.base.add({rva:d});
Interceptor.attach(_addr, {{
    onEnter: function(args) {{
        try {{
            var s = {read_expr};
            if (s) _onText(s);
        }} catch(e) {{}}
    }}
}});
send({{ type: 'diag', value: 'address-hook attached: {module}+{rva:#x} {access_pattern} ({encoding})' }});
"""

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

    def to_hook_code(self) -> HookCode:
        return HookCode(
            module=self.module,
            rva=self.rva,
            access_pattern=self.access_pattern,
            encoding=self.encoding,
        )

    def display_label(self) -> str:
        """Short label for UI display."""
        preview = self.text[:60].replace("\n", " ")
        return (
            f"{self.module}  +{self.rva:#x}  {self.access_pattern}"
            f"  hits={self.hit_count}  {preview!r}"
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


def _shannon_entropy(text: str) -> float:
    """Character-level Shannon entropy in bits."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def score_candidate(text: str) -> float:
    """Return a quality score ≥ 0.  Score 0 means 'discard'."""
    if not text:
        return 0.0

    # --- hard rejects ------------------------------------------------------

    # Binary / high-entropy garbage
    if _shannon_entropy(text) > 5.5:
        return 0.0

    # File / resource paths
    if _PATH_RE.search(text):
        return 0.0

    # Pure ASCII identifiers and filenames (likely internal IDs)
    stripped = text.strip()
    if _PURE_ASCII_ID_RE.match(stripped):
        return 0.0
    if _FILE_EXT_RE.search(stripped) and len(stripped) < 60:
        return 0.0

    # Hex blobs
    if _HEX_BLOB_RE.match(stripped):
        return 0.0

    # Require at least some CJK content
    cjk_chars = sum(
        1 for c in text
        if "\u3000" <= c <= "\u9FFF" or "\uFF00" <= c <= "\uFFEF"
    )
    if cjk_chars == 0:
        return 0.0

    # --- scoring -----------------------------------------------------------

    score = float(len(text))

    # Reward high CJK density (dialogue vs. mixed noise)
    cjk_ratio = cjk_chars / max(len(text), 1)
    score *= 1.0 + cjk_ratio

    # Strong bonus for double-quoted or bracket-quoted dialogue
    if _QUOTED_DIALOGUE_RE.match(text):
        score *= 2.5

    return score


# ---------------------------------------------------------------------------
# HookSearcher
# ---------------------------------------------------------------------------


class HookSearchError(RuntimeError):
    """Raised when the hook-search session cannot be started."""


@dataclass
class FoundString:
    """A CJK string discovered in game process memory during Phase 1."""
    address: str   # hex string as sent by JS
    encoding: str  # 'utf16' or 'utf8'
    text: str


class HookSearcher:
    """Hook-site discovery via Frida (auto-scan + read-monitor strategy).

    Phase 1 (starts immediately on :meth:`start`):
        JS scans rw- memory for UTF-16LE CJK strings.  Results are
        available via :attr:`found_strings` once :attr:`scan_complete`.

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
        searcher.wait_for_scan()
        # show searcher.found_strings to user, they pick
        searcher.watch([s.address for s in selected])
        # render loop fires -> candidates arrive
        candidates = searcher.ranked_candidates()
        searcher.stop()
    """

    def __init__(
        self,
        pid: int,
        *,
        max_candidates: int = 50,
        max_strings: int = 200,
    ) -> None:
        self._pid         = pid
        self._max_c       = max_candidates
        self._max_strings = max_strings
        self._session: frida.core.Session | None = None
        self._script: frida.core.Script | None   = None

        self._candidates: dict[str, HookCandidate] = {}
        self._diags: list[str] = []
        self._found_strings: list[FoundString] = []
        self._scan_done = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Attach Frida and load the search script.

        Raises
        ------
        HookSearchError
            On Frida attach / script load failure.
        """
        try:
            self._session = frida.attach(self._pid)
        except Exception as exc:
            raise HookSearchError(
                f"Could not attach Frida to PID {self._pid}: {exc}"
            ) from exc

        # Inject config variables before the scan script body
        config_prefix = (
            f"var config = {{"
            f"  maxCandidates: {self._max_c},"
            f"  maxStrings: {self._max_strings}"
            f"}};"
        )
        try:
            self._script = self._session.create_script(
                config_prefix + _SEARCH_SCRIPT_JS
            )
            self._script.on("message", self._on_message)
            self._script.load()
        except Exception as exc:
            self._detach_silently()
            raise HookSearchError(
                f"Failed to load hook-search script: {exc}"
            ) from exc

        log.info("HookSearcher started for PID %d", self._pid)

    def stop(self) -> None:
        """Detach Frida.  Safe to call multiple times."""
        self._detach_silently()
        log.info("HookSearcher stopped")

    def wait_for_scan(self, timeout: float = 10.0) -> bool:
        """Block until the initial code scan completes (or *timeout* seconds).

        Returns ``True`` if scan completed within the timeout.
        """
        return self._scan_done.wait(timeout)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def ranked_candidates(self) -> list[HookCandidate]:
        """Return candidates sorted best-first.

        The list is a snapshot; call again after more game interaction for
        updated scores.
        """
        with self._lock:
            candidates = list(self._candidates.values())

        # Re-score with hit_count bonus then sort
        for c in candidates:
            c.score = score_candidate(c.text) * (1.0 + 0.1 * c.hit_count)

        return sorted(candidates, key=lambda c: -c.score)

    def diags(self) -> list[str]:
        """Return all diagnostic messages received so far."""
        with self._lock:
            return list(self._diags)

    @property
    def scan_complete(self) -> bool:
        """``True`` once the Phase 1 memory scan has finished."""
        return self._scan_done.is_set()

    @property
    def found_strings(self) -> list[FoundString]:
        """CJK strings found in process memory during Phase 1."""
        with self._lock:
            return list(self._found_strings)

    def watch(self, addresses: list[str]) -> None:
        """Start Phase 2: arm MemoryAccessMonitor on *addresses*.

        Parameters
        ----------
        addresses:
            Hex-string addresses as returned by :attr:`found_strings`.
            Pass the ones whose text is currently on screen.
        """
        if self._script is None:
            raise HookSearchError("Cannot watch: script not loaded.")
        self._script.post({"type": "watch", "addresses": addresses})
        log.info("Posted watch for %d address(es)", len(addresses))

    # ------------------------------------------------------------------
    # Frida callbacks
    # ------------------------------------------------------------------

    def _on_message(self, message: dict[str, Any], _data: Any) -> None:
        if message.get("type") != "send":
            if message.get("type") == "error":
                log.warning("Frida search error: %s", message.get("description", ""))
            return

        payload = message.get("payload") or {}
        kind = payload.get("type")

        if kind == "candidate":
            self._handle_candidate(payload)
        elif kind == "diag":
            value = payload.get("value", "")
            with self._lock:
                self._diags.append(value)
            log.debug("Search diag: %s", value)
        elif kind == "string_found":
            addr = str(payload.get("address", ""))
            enc  = str(payload.get("encoding", "utf16"))
            text = str(payload.get("text", ""))
            with self._lock:
                self._found_strings.append(FoundString(addr, enc, text))
            log.debug("String found at %s: %.30r", addr, text)
        elif kind == "scan_done":
            count = int(payload.get("count", 0))
            with self._lock:
                self._diags.append(f"Scan complete — {count} CJK string(s) found")
            self._scan_done.set()
            log.info("Phase 1 scan complete: %d strings", count)

    def _handle_candidate(self, payload: dict[str, Any]) -> None:
        module   = str(payload.get("module", ""))
        rva_str  = str(payload.get("rva", "0x0"))
        pattern  = str(payload.get("pattern", "r0"))
        encoding = str(payload.get("encoding", "utf16"))
        text     = str(payload.get("text", ""))

        if not text:
            return

        try:
            rva = int(rva_str, 16)
        except ValueError:
            return

        s = score_candidate(text)
        if s <= 0:
            return  # pre-filter low-quality hits

        key = f"{rva:#x}:{pattern}"
        with self._lock:
            if key in self._candidates:
                c = self._candidates[key]
                c.hit_count += 1
                # Keep the longest representative text seen at this site
                if len(text) > len(c.text):
                    c.text = text
            else:
                self._candidates[key] = HookCandidate(
                    module=module,
                    rva=rva,
                    access_pattern=pattern,
                    encoding=encoding,
                    text=text,
                    hit_count=1,
                    score=s,
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _detach_silently(self) -> None:
        if self._script is not None:
            try:
                self._script.unload()
            except Exception:
                pass
            self._script = None
        if self._session is not None:
            try:
                self._session.detach()
            except Exception:
                pass
            self._session = None
