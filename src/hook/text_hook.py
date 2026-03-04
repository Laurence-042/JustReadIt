"""Frida-based text hook for Light.VN games.

Attaches to the target process via Frida and intercepts Win32 text output
functions to capture rendered text strings.  Captured text is stored in
per-region sets for later cross-matching with OCR results.

Hooked functions (resolved at runtime from the target process):

    gdi32 : TextOutW, ExtTextOutW
    user32: DrawTextW, DrawTextExW
    dwrite: IDWriteFactory::CreateTextLayout

The Frida instrumentation script is kept in the sibling file
``hook_script.js`` and loaded at import time.  Edit that file to change
hook targets without touching Python code.

The script communicates with the Python side via Frida's
``send()`` / ``on('message', …)`` mechanism.

Usage::

    from src.hook.text_hook import TextHook

    hook = TextHook(pid=12345)
    hook.attach()
    # … game renders text …
    print(hook.texts)   # ['こんにちは', '選択肢1', …]
    hook.detach()

Or as a context manager::

    with TextHook(pid=12345) as hook:
        ...
        print(hook.texts)

Thread safety
-------------
:attr:`texts` and :meth:`clear` are guarded by a :class:`threading.Lock`.
Frida's message callback fires on an internal Frida thread, so the lock is
necessary.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import frida
import frida.core

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HookAttachError(RuntimeError):
    """Raised when Frida cannot attach to the target process."""


class HookScriptError(RuntimeError):
    """Raised when the Frida instrumentation script fails to load."""


# ---------------------------------------------------------------------------
# Frida JavaScript instrumentation  (loaded from sibling hook_script.js)
# ---------------------------------------------------------------------------

_HOOK_SCRIPT_JS: str = (Path(__file__).parent / "hook_script.js").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Python-side hook manager
# ---------------------------------------------------------------------------

# Maximum number of texts to keep before oldest entries are dropped.
_MAX_TEXTS = 4096


class TextHook:
    """Frida-based text hook that captures Win32 text output.

    Parameters
    ----------
    pid:
        Target process ID (usually ``GameTarget.pid``).

    Attributes
    ----------
    texts : list[str]
        Snapshot of captured text strings (newest last).  Thread-safe.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._session: frida.core.Session | None = None
        self._script: frida.core.Script | None = None
        self._texts: list[str] = []
        self._diag: str = ""
        self._lock = threading.Lock()

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
        """Attach Frida to the target process and install text hooks.

        Raises
        ------
        HookAttachError
            If Frida cannot attach (process not found, insufficient
            privileges, etc.).
        HookScriptError
            If the JavaScript instrumentation script fails to load.
        """
        if self._session is not None:
            return  # already attached

        try:
            self._session = frida.attach(self._pid)
        except frida.ProcessNotFoundError:
            raise HookAttachError(
                f"Frida could not attach: no process with PID {self._pid}. "
                "Make sure the game is running."
            ) from None
        except frida.PermissionDeniedError:
            raise HookAttachError(
                f"Frida could not attach to PID {self._pid}: permission denied. "
                "Try running as Administrator."
            ) from None
        except Exception as exc:
            raise HookAttachError(
                f"Frida could not attach to PID {self._pid}: {exc}"
            ) from exc

        self._session.on("detached", self._on_session_detached)

        try:
            self._script = self._session.create_script(_HOOK_SCRIPT_JS)
            self._script.on("message", self._on_message)
            self._script.load()
        except Exception as exc:
            # Clean up the session if script loading fails.
            try:
                self._session.detach()
            except Exception:
                pass
            self._session = None
            self._script = None
            raise HookScriptError(
                f"Failed to load hook script in PID {self._pid}: {exc}"
            ) from exc

        log.info("TextHook attached to PID %d", self._pid)

    def detach(self) -> None:
        """Detach Frida from the target process.

        Safe to call multiple times or when not attached.
        """
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

        log.info("TextHook detached from PID %d", self._pid)

    @property
    def attached(self) -> bool:
        """``True`` if the Frida session is active."""
        return self._session is not None

    # ------------------------------------------------------------------
    # Collected texts
    # ------------------------------------------------------------------

    @property
    def texts(self) -> list[str]:
        """Return a snapshot of all captured text strings (thread-safe)."""
        with self._lock:
            return list(self._texts)

    @property
    def diag(self) -> str:
        """Startup diagnostic from the JS instrumentation script.

        Lists which hook points were attached and which text-related modules
        are loaded in the target process.  Empty until the script has loaded.
        """
        with self._lock:
            return self._diag

    def clear(self) -> None:
        """Discard all captured text strings."""
        with self._lock:
            self._texts.clear()

    # ------------------------------------------------------------------
    # Frida callbacks (called on Frida's internal thread)
    # ------------------------------------------------------------------

    def _on_message(self, message: dict[str, Any], _data: Any) -> None:
        """Handle a message from the Frida script."""
        if message.get("type") == "send":
            payload = message.get("payload")
            if isinstance(payload, dict):
                kind = payload.get("type")
                value = payload.get("value", "")
                if kind == "text" and value:
                    with self._lock:
                        self._texts.append(value)
                        if len(self._texts) > _MAX_TEXTS:
                            self._texts = self._texts[-_MAX_TEXTS:]
                elif kind == "diag" and value:
                    with self._lock:
                        self._diag = value
                    log.info("Hook diag: %s", value)
        elif message.get("type") == "error":
            log.warning("Frida script error: %s", message.get("description", ""))

    def _on_session_detached(self, reason: str, *_: object) -> None:
        """Called when the Frida session is detached (e.g. process exit)."""
        log.warning("Frida session detached (reason: %s)", reason)
        self._session = None
        self._script = None
