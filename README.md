# JustReadIt

> Windows-only hover-translation tool for Light.VN-based RPG games.

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-blue.svg)]()

JustReadIt is a real-time translation overlay for Windows. Move your mouse over in-game text, and a transparent overlay instantly shows you the translation — no copy-paste, no alt-tabbing.

---

## Features

- **DXGI Desktop Duplication capture** — works on DirectX games where `BitBlt`/`PrintWindow` produce black frames
- **Windows OCR** — built-in, no external model downloads required; upscales small fonts for accuracy
- **Native text hook** — uses Frida to hook EXE function prologues and read CJK strings directly from game memory via a Named Pipe, then cross-validates with OCR output using Levenshtein matching
- **Freeze mode** — hotkey freezes a screenshot as a topmost overlay so you can hover-translate at your own pace; right-click to dismiss and return focus to the game
- **Pluggable translation backends** — clean `Translator` ABC; Cloud Translation API and OpenAI (with rolling context summary) planned
- **Perceptual-hash cache** — phash of the translation region avoids redundant OCR and translation calls for unchanged text
- **PySide6 debug window** — capture preview, OCR bounding-box overlay, hook output viewer

---

## Architecture

```
Mouse hover / Freeze hotkey
  -> DXGI Desktop Duplication capture (src/capture.py)
    -> Windows OCR small-area probe (src/ocr/windows_ocr.py)
      -> no text -> return to idle
      -> text found -> phash of translation region (src/cache.py)
        -> cache hit -> show overlay
        -> cache miss:
            -> full-screen Windows OCR -> range detection (src/ocr/range_detectors.py)
            -> Levenshtein cross-match with DLL hook output (src/correction.py)
              -> match success -> use cleaned hook text
              -> match failure -> fall back to OCR text
            -> translate (src/translators/) -> cache -> show overlay (src/overlay.py)
```

### Component map

| Component | Path | Role |
|---|---|---|
| Target process | `src/target.py` | `GameTarget` frozen dataclass — PID, HWND, window/capture rects, dxcam output index |
| Screen capture | `src/capture.py` | DXGI Desktop Duplication via dxcam |
| OCR engine | `src/ocr/windows_ocr.py` | `WindowsOcr` — upscale-then-downscale for small fonts; PIL ↔ WinRT bitmap bridge |
| Range detection | `src/ocr/range_detectors.py` | `RangeDetector` ABC + `run_detectors()` chain runner |
| Hook discovery | `src/hook/hook_search.py` | `HookSearcher` — bulk-hooks EXE function prologues, reads CJK strings from Named Pipe |
| Hook value objects | `src/hook/hook_code.py` | `HookCode` frozen dataclass; `HookCandidate` live hit accumulator |
| Candidate scorer | `src/hook/candidate_scorer.py` | `score_candidate(text, lang)` — hard-reject filters + per-language subclasses |
| Hook cleaner | `src/hook/cleaner.py` | `Cleaner` ABC rule chain — `StripControlChars`, `DeduplicateLines`, `TrimWhitespace` |
| Correction | `src/correction.py` | Levenshtein cross-match between OCR and hook results |
| Cache | `src/cache.py` | Perceptual-hash keyed translation cache |
| Translation | `src/translators/` | `Translator` ABC; Cloud Translation API + OpenAI planned |
| Config | `src/config.py` | `AppConfig` — typed `QSettings` wrapper, INI at `%APPDATA%\JustReadIt\config.ini` |
| Debug UI | `src/ui/` | PySide6 debug window + window picker |
| Overlay | `src/overlay.py` | Topmost transparent overlay, Freeze mode |

---

## Requirements

- **Windows 10 / 11** (Windows OCR and DXGI Desktop Duplication are Windows-only APIs)
- **Python 3.11+**
- The target game must be a **Light.VN-based** title (the hook search is tuned for Light.VN engine internals)

---

## Installation

```powershell
# Clone the repository
git clone https://github.com/Laurence-042/JustReadIt.git
cd JustReadIt

# Install core dependencies
pip install -e .

# Install UI extras (PySide6) for the debug window
pip install -e ".[ui]"

# Install dev tools (pytest, type stubs, etc.)
pip install -e ".[dev]"
```

---

## Usage

```powershell
# Launch the PySide6 debug / test window
python main.py --debug

# Headless mode (not yet implemented)
python main.py
```

---

## Development

```powershell
# Run the test suite
pytest
```

The codebase follows a **chain-of-responsibility** pattern for extensible rules:

- **Range detectors** (`src/ocr/range_detectors.py`): implement `RangeDetector` and append to `DEFAULT_DETECTORS`.
- **Hook cleaners** (`src/hook/cleaner.py`): implement `Cleaner` and append to the default chain.
- **Translation backends** (`src/translators/`): implement the `Translator` ABC.

All files begin with `from __future__ import annotations`. Types are pervasive (PEP 604 `X | None`, PEP 585 generics). See `.github/copilot-instructions.md` for full code-style guidelines.

---

## Acknowledgements

### Conceptual inspiration: Textractor

The text-hook subsystem (`src/hook/`) was **conceptually inspired by [Textractor](https://github.com/Artikash/Textractor)**, a widely used open-source game-text extractor.

Specifically, the general idea of bulk-hooking EXE function prologues to discover which ones emit CJK text strings at runtime originates from Textractor's hook-search approach.

**Important clarifications:**

- **No source code from Textractor was copied or incorporated.** The hook engine (`hook_engine.dll`), the Python hook-search orchestration (`src/hook/hook_search.py`), the candidate scoring logic, and the Named Pipe transport are all original implementations written independently.
- **The execution flow differs substantially:** Textractor is a standalone C++ application that injects a DLL and routes text through a GUI extension pipeline. JustReadIt runs the hook search as a subordinate phase within a Python OCR-and-translation pipeline: hook output is cross-validated against Windows OCR results via Levenshtein matching before being accepted, and the translation is rendered as a DXGI-captured overlay rather than forwarded to clipboard or a separate UI.
- Textractor is licensed under the GNU General Public License v3.0; this project is independently authored and licensed under the **Mozilla Public License 2.0**.

Full credit and thanks to [Artikash](https://github.com/Artikash) and all Textractor contributors for pioneering and open-sourcing the hook-search technique.

---

## License

This Source Code Form is subject to the terms of the **Mozilla Public License, v. 2.0**.
If a copy of the MPL was not distributed with this file, you can obtain one at https://mozilla.org/MPL/2.0/.

See [`LICENSE`](LICENSE) for the full license text.