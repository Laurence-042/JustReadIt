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
6.  Pass the :class:`HookCode` to :class:`~src.hook.text_hook.TextHook`.
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
    pack_search_config,
    read_pipe_exact,
    unpack_result_hdr,
    write_pipe,
    _RESULT_HDR_SIZE,
)

log = logging.getLogger(__name__)

_DLL_PATH = Path(__file__).parent / "hook_engine.dll"

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
        max_hooks: int = 60_000,
    ) -> None:
        self._pid        = pid
        self._max_c      = max_candidates
        self._max_hooks  = max_hooks

        self._h_pipe: int = 0
        self._stop       = threading.Event()
        self._candidates: dict[str, HookCandidate] = {}
        self._diags:      list[str]                = []
        self._scan_done  = threading.Event()
        self._lock       = threading.Lock()
        self._reader:     threading.Thread | None  = None

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
        log.info("HookSearcher started for PID %d", self._pid)

    def stop(self) -> None:
        """Close the pipe and stop the reader thread.  Safe to call many times."""
        self._stop.set()
        if self._h_pipe:
            try:
                close_pipe(self._h_pipe)
            except Exception:
                pass
            self._h_pipe = 0
        log.info("HookSearcher stopped")

    def wait_for_scan(self, timeout: float = 30.0) -> bool:
        """Block until the bulk-patch phase completes (or *timeout* seconds)."""
        return self._scan_done.wait(timeout)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def ranked_candidates(self) -> list[HookCandidate]:
        """Return candidates sorted best-first (snapshot)."""
        with self._lock:
            candidates = list(self._candidates.values())
        for c in candidates:
            c.score = score_candidate(c.text) * (1.0 + 0.1 * c.hit_count)
        return sorted(candidates, key=lambda c: -c.score)

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
            with self._lock:
                self._diags.append(f"Pipe connect failed: {exc}")
            log.error("HookSearcher pipe connect failed: %s", exc)
            return

        # Send search config to DLL
        try:
            write_pipe(self._h_pipe, pack_search_config(self._max_hooks))
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

        log.debug("HookSearcher reader loop exiting")

    def _handle_control(self, text: str) -> None:
        if text.startswith("scan_done:"):
            try:
                count = int(text.split(":", 1)[1])
            except ValueError:
                count = 0
            with self._lock:
                self._diags.append(f"Bulk patch complete — {count} function(s) hooked")
            self._scan_done.set()
            log.info("HookSearcher scan done: %d hooks installed", count)
        elif text.startswith("ERROR:"):
            with self._lock:
                self._diags.append(text)
            log.error("DLL error: %s", text)
        else:
            with self._lock:
                self._diags.append(text)

    def _handle_hit(self, hook_va: int, slot_i: int,
                     encoding: int, text: str) -> None:
        """Build / update a HookCandidate from a single pipe result.

        slot_i values come from the trampoline's ``i`` counter, which reflects
        the position of the value in the saved-register + caller-stack window::

            -16 = rax    -15 = rbx    -14 = rcx (arg0)  -13 = rdx (arg1)
            -12 = rsp    -11 = rbp    -10 = rsi  -9 = rdi
             -8 = r8 (arg2)   -7 = r9 (arg3)   -6...-1 = r10-r15
              0 = return addr   1..N = caller stack slots
        """
        s = score_candidate(text)
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
            else:
                self._candidates[key] = HookCandidate(
                    module=name,
                    rva=rva,
                    access_pattern=pattern,
                    encoding=enc_str,
                    text=text,
                    hit_count=1,
                    score=s,
                )


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
