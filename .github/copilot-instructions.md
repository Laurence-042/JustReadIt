# JustReadIt — Copilot Instructions

## Project Overview

A hover-translation tool for a Light.VN-based RPG game. Combines local OCR with pluggable translation backends to render accurate, context-aware translations as an overlay on screen.

## Architecture

```
DXGI screen capture
  → Windows OCR (bounding box + text)
    → phash cache lookup
      → hit: show overlay
      → miss: full-screen Windows OCR → range detection → Levenshtein match with Frida hook → translate → cache → show overlay
```

Key components and their locations (to be created under `src/`):

| Component | Path | Role |
|---|---|---|
| Screen capture | `src/capture.py` | DXGI Desktop Duplication only — never `BitBlt`/`PrintWindow` (black frames on DirectX) |
| OCR layer | `src/ocr/` | Windows OCR only — bounding box + text recognition |
| Range detection | `src/ocr/range_detectors.py` | Extensible rule chain: `List[RangeDetector]` |
| Hook + cleaner | `src/hook/` | Frida-based text hook + `List[Cleaner]` rule chain |
| Correction | `src/correction.py` | Levenshtein cross-match between OCR and hook results |
| Cache | `src/cache.py` | phash of translated-region screenshot as key |
| Translation | `src/translators/` | `Translator` ABC; two built-in plugins |
| Debug UI | `src/ui/` | PySide6 debug window; `main.py --debug` to launch |
| Overlay | `src/overlay.py` | Topmost transparent window, handles Freeze mode |

## Code Conventions

- **Extensibility via rule chains**: range detectors and hook cleaners implement a common ABC and are composed as `List[RangeDetector]` / `List[Cleaner]`. Built-in rules: paragraph, table-row, single-box (range); strip control chars, deduplicate, trim (cleaner). New rules are appended to the list.
- **Translation plugin interface**: `Translator` ABC in `src/translators/base.py`. Cloud Translation and OpenAI are the two built-ins. OpenAI plugin maintains a rolling summary agent for context.
- **GPU/CPU degradation**: Windows OCR is the sole OCR engine — no GPU dependency.
- **Freeze mode focus handoff**: use `AllowSetForegroundWindow(pid)` to return focus to the game process — direct cross-process `SetForegroundWindow` is blocked by Windows.
- **phash caching**: always key on the perceptual hash of the *translated region screenshot*, not the full screen.

## Build & Test

Project not yet initialised. When scaffolding:

```bash
# create project
uv init   # or: python -m venv .venv && pip install -e ".[dev]"

# install deps
pip install frida winsdk imagehash rapidfuzz pywin32 Pillow

# run tests
pytest tests/
```

Use `pyproject.toml` for dependency management (see TODO in `doc/story.md`).

## Key Constraints

- Screen capture **must** use DXGI Desktop Duplication API (or DWM shared surface).
- Hook is implemented with **Frida** (not Textractor/LunaTranslator — both GPL-3.0).
- License is **MPL-2.0** (file-level weak copyleft). Frida uses wxWindows Licence 3.1 — when distributing binaries, source or build toolchain must be provided.
- Windows-only project; use `ctypes`/`pywin32` for Win32 API calls.

## Reference

Full design rationale: [`doc/story.md`](../doc/story.md)
