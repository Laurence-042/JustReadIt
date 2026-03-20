# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Runtime dependency installer for optional translator backends.

Works in both normal Python execution and **PyInstaller-frozen executables**
without requiring the user to have Python or pip installed.

How it works
------------
* **Development / venv mode** (``sys.frozen`` absent):
  Delegates to ``subprocess [sys.executable, -m, pip, install]`` — the
  standard pip workflow, installs into the active environment.

* **Frozen exe mode** (``sys.frozen = True``, set by PyInstaller):
  ``sys.executable`` is the bundle itself, not ``python.exe``.  pip is
  bundled as a hidden import and called via its internal API.  Packages are
  installed to a per-user writable directory

      %APPDATA%\\JustReadIt\\lib

  which is prepended to ``sys.path`` so the newly installed package is
  importable immediately — no restart required.

PyInstaller spec note
---------------------
To bundle pip, add to your ``.spec``::

    hiddenimports=[
        "pip",
        "pip._internal",
        "pip._internal.cli.main",
    ]

If pip is not bundled, a ``RuntimeWarning`` is emitted and the installer
attempts a subprocess fallback (which will likely fail in frozen mode).
"""
from __future__ import annotations

import importlib
import os
import sys
import warnings
from typing import Callable

# ---------------------------------------------------------------------------
# User library directory (frozen mode install target)
# ---------------------------------------------------------------------------

_USER_LIB: str = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "JustReadIt",
    "lib",
)


def _inject_user_lib() -> None:
    """Add *_USER_LIB* to ``sys.path`` if it exists and isn't already there."""
    if os.path.isdir(_USER_LIB) and _USER_LIB not in sys.path:
        sys.path.insert(0, _USER_LIB)


# Inject once at import time so any packages already installed there are
# immediately importable without an explicit ensure_package() call.
_inject_user_lib()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

Progress = Callable[[str], None]


def ensure_package(
    pip_name: str,
    import_name: str,
    *,
    progress: Progress | None = None,
) -> None:
    """Ensure *pip_name* is importable, auto-installing if necessary.

    Args:
        pip_name: Package specifier passed to ``pip install``
            (e.g. ``"openai>=1.0"`` or ``"google-cloud-translate>=3.0"``).
        import_name: Top-level module name used to verify the package is
            importable after installation (e.g. ``"openai"``).
        progress: Optional callable that receives human-readable status
            strings during installation — useful for updating a UI label.

    Raises:
        RuntimeError: If installation fails or the package still cannot be
            imported after a successful pip invocation.
    """
    # Fast path: already importable.
    try:
        importlib.import_module(import_name)
        return
    except ImportError:
        pass

    if progress:
        progress(f"Installing {pip_name} …")

    if getattr(sys, "frozen", False):
        _install_frozen(pip_name, progress=progress)
    else:
        _install_subprocess(pip_name, progress=progress)

    # Re-inject in case the install created the user lib directory for the
    # first time (frozen mode).
    _inject_user_lib()

    # Invalidate the import cache so the freshly installed package is visible.
    importlib.invalidate_caches()

    # For namespace packages (e.g. google, google.cloud) Python caches a stale
    # __path__ in sys.modules after the first failed import.  Remove all
    # ancestor entries so they are rediscovered with the newly installed paths.
    parts = import_name.split(".")
    for i in range(len(parts)):
        key = ".".join(parts[: i + 1])
        sys.modules.pop(key, None)

    # Verify the package is now importable.
    try:
        importlib.import_module(import_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Package '{pip_name}' was installed but '{import_name}' could not "
            f"be imported.  Please restart the application and try again."
        ) from exc

    if progress:
        progress(f"'{pip_name}' installed successfully.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _install_subprocess(pip_name: str, *, progress: Progress | None) -> None:
    """Non-frozen install via ``subprocess`` + ``sys.executable``."""
    import subprocess

    cmd = [sys.executable, "-m", "pip", "install", "--quiet", pip_name]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Python executable not found at '{sys.executable}'.  "
            f"Cannot run pip."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"pip install '{pip_name}' timed out after 180 s."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"pip install '{pip_name}' failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )


def _install_frozen(pip_name: str, *, progress: Progress | None) -> None:
    """Frozen-exe install via pip's internal API into *_USER_LIB*.

    Requires pip to be included as a hidden import in the PyInstaller spec.
    Falls back to subprocess (which may fail) if pip is not bundled.
    """
    os.makedirs(_USER_LIB, exist_ok=True)

    try:
        from pip._internal.cli.main import main as _pip_main  # type: ignore[import-untyped]
    except ImportError:
        warnings.warn(
            "pip is not bundled in this executable — attempting system Python "
            "fallback for package installation.  This may fail.\n"
            "Add pip to hiddenimports in your PyInstaller .spec to fix this.",
            RuntimeWarning,
            stacklevel=4,
        )
        _install_subprocess(pip_name, progress=progress)
        return

    exit_code: int = _pip_main(
        ["install", "--target", _USER_LIB, "--quiet", pip_name]
    )
    if exit_code != 0:
        raise RuntimeError(
            f"pip install '{pip_name}' to '{_USER_LIB}' failed "
            f"(exit code {exit_code})."
        )
