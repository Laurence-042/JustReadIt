"""Native DLL-based text hook for Light.VN games.

Injects ``hook_engine.dll`` into the target process and uses MinHook to
install a single hook at the address stored in :class:`~src.hook.hook_search.HookCode`.
Captured text is forwarded via Named Pipe and stored in :attr:`TextHook.texts`.

Usage::

    code = HookCode.from_str(config.hook_code)
    with TextHook(pid=12345, hook_code=code) as hook:
        ...
        print(hook.texts)

Thread safety
-------------
:attr:`texts` and :meth:`clear` are guarded by :class:`threading.Lock`.
The Named Pipe reader fires on an internal thread.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from src.hook._win32 import (
    InjectionError,
    PipeError,
    _RESULT_HDR_SIZE,
    close_pipe,
    connect_pipe,
    create_pipe_server,
    get_module_base,
    inject_dll,
    pack_hook_config,
    read_pipe_exact,
    unpack_result_hdr,
)
from pathlib import Path

if TYPE_CHECKING:
    from src.hook.hook_search import HookCode

log = logging.getLogger(__name__)

_DLL_PATH = Path(__file__).parent / "hook_engine.dll"
_MAX_TEXTS = 4096

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HookAttachError(RuntimeError):
    """Raised when the hook DLL cannot be injected or the hook cannot be set."""


class HookScriptError(RuntimeError):
    """Kept for API compatibility; not raised by the native implementation."""


# ---------------------------------------------------------------------------
# TextHook
# ---------------------------------------------------------------------------


class TextHook:
    """Native text hook via ``hook_engine.dll`` + MinHook.

    Parameters
    ----------
    pid:
        Target process ID (``GameTarget.pid``).
    diagnostic:
        Accepted for API compatibility; has no effect in the native
        implementation.
    hook_code:
        A :class:`~src.hook.hook_search.HookCode` previously discovered by
        :class:`~src.hook.hook_search.HookSearcher`.  Required — attach will
        raise :exc:`HookAttachError` if this is ``None``.
    """

    def __init__(
        self,
        pid: int,
        *,
        diagnostic: bool = False,
        hook_code: "HookCode | None" = None,
    ) -> None:
        self._pid        = pid
        self._diagnostic = diagnostic
        self._hook_code  = hook_code

        self._h_pipe: int            = 0
        self._stop       = threading.Event()
        self._reader:    threading.Thread | None = None

        self._texts:  list[str] = []
        self._diags:  list[str] = []
        self._lock    = threading.Lock()

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "TextHook":
        self.attach()
        return self

    def __exit__(self, *_: object) -> None:
        self.detach()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Inject the hook DLL and start capturing text.

        Raises
        ------
        HookAttachError
            If *hook_code* is ``None``, the DLL is missing, or injection fails.
        """
        if self._h_pipe:
            return  # already attached

        if self._hook_code is None:
            raise HookAttachError(
                "TextHook requires a confirmed HookCode.  "
                "Run the hook search first to discover one."
            )

        # Resolve absolute VA from module + RVA
        hook_va = self._resolve_va()
        if hook_va is None:
            raise HookAttachError(
                f"Module '{self._hook_code.module}' not found in PID {self._pid}.  "
                "Make sure the game is running."
            )

        try:
            self._h_pipe = create_pipe_server(self._pid)
        except OSError as exc:
            raise HookAttachError(f"Named Pipe creation failed: {exc}") from exc

        try:
            inject_dll(self._pid, _DLL_PATH)
        except InjectionError as exc:
            close_pipe(self._h_pipe)
            self._h_pipe = 0
            raise HookAttachError(str(exc)) from exc

        arg_idx, deref, byte_offset, enc_int = self._hook_code.to_hook_config_fields()

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._reader_loop,
            args=(hook_va, arg_idx, deref, byte_offset, enc_int),
            name="text-hook-reader",
            daemon=True,
        )
        self._reader.start()
        log.info("TextHook attached: %s  VA=%#x", self._hook_code.to_str(), hook_va)

    def detach(self) -> None:
        """Detach the hook DLL.  Safe to call multiple times."""
        self._stop.set()
        if self._h_pipe:
            try:
                close_pipe(self._h_pipe)
            except Exception:
                pass
            self._h_pipe = 0
        log.info("TextHook detached from PID %d", self._pid)

    @property
    def attached(self) -> bool:
        """``True`` if the hook is currently active."""
        return bool(self._h_pipe)

    # ------------------------------------------------------------------
    # Captured text
    # ------------------------------------------------------------------

    @property
    def texts(self) -> list[str]:
        """Snapshot of all captured text strings (newest last)."""
        with self._lock:
            return list(self._texts)

    def clear(self) -> None:
        """Discard all captured text."""
        with self._lock:
            self._texts.clear()

    @property
    def diag(self) -> str:
        """Diagnostic messages from the DLL, joined as a single string."""
        with self._lock:
            return "\n".join(self._diags)

    @property
    def diagnostic(self) -> bool:
        """Always ``False`` in the native implementation."""
        return False

    @property
    def hook_code(self) -> "HookCode | None":
        """The :class:`~src.hook.hook_search.HookCode` used for this hook."""
        return self._hook_code

    # ------------------------------------------------------------------
    # Pipe reader loop
    # ------------------------------------------------------------------

    def _reader_loop(self, hook_va: int, arg_idx: int, deref: int,
                      byte_offset: int, enc_int: int) -> None:
        try:
            connect_pipe(self._h_pipe)
        except (PipeError, OSError) as exc:
            with self._lock:
                self._diags.append(f"Pipe connect failed: {exc}")
            log.error("TextHook pipe connect failed: %s", exc)
            return

        try:
            from src.hook._win32 import write_pipe
            write_pipe(
                self._h_pipe,
                pack_hook_config(hook_va, arg_idx, deref, byte_offset, enc_int),
            )
        except (PipeError, OSError) as exc:
            with self._lock:
                self._diags.append(f"Config write failed: {exc}")
            return

        last_text = ""

        while not self._stop.is_set():
            hdr = read_pipe_exact(self._h_pipe, _RESULT_HDR_SIZE)
            if hdr is None:
                break

            _, _, encoding, text_len = unpack_result_hdr(hdr)
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
                ).strip()
            except Exception:
                continue

            if not text or text == last_text:
                continue

            # Control messages (hook_va == 0) go to diags, not texts
            hook_va_result = int.from_bytes(hdr[:8], "little")
            if hook_va_result == 0:
                with self._lock:
                    self._diags.append(text)
                log.debug("TextHook DLL msg: %s", text)
                continue

            last_text = text
            with self._lock:
                self._texts.append(text)
                if len(self._texts) > _MAX_TEXTS:
                    self._texts = self._texts[-_MAX_TEXTS:]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_va(self) -> int | None:
        """Return the absolute VA of the hook site in the target process."""
        assert self._hook_code is not None
        base = get_module_base(self._pid, self._hook_code.module)
        if base is None:
            return None
        return base + self._hook_code.rva
