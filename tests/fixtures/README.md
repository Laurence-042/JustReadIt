# Correction samples — dataset for `best_match_with_details`

`correction_samples.csv` is a data-driven regression / annotation set for the
end-to-end OCR + memory → corrected-text pipeline (`src/correction.py::
best_match_with_details`).  Each row is one parametrised pytest case in
`tests/test_correction_dataset.py`.

The same schema is intended for **annotated exports**: when a real-world bug is
captured (OCR text, memory candidates, needle, the wrong/right output), drop
the rows into this CSV (or a sibling file) and pytest will replay them.

## Schema

| Column                | Meaning |
|-----------------------|---------|
| `id`                  | Unique pytest test id. |
| `ocr_text`            | OCR text. CSV multi-line cells (quoted) are fine. |
| `memory_hits`         | **JSON array of strings.** Use `\n`/`\\` JSON escapes inside the strings — do not put a physical newline inside the JSON literal. (`ocr_text` and `expected` may still use CSV multi-line cells; this rule is specific to `memory_hits` because it is parsed as JSON.) |
| `needle`              | The needle string. |
| `match_mode`          | `exact` / `contains_all` / `none`. |
| `expected`            | `exact` → full expected text. `contains_all` → `\|`-separated required substrings. `none` → empty. |
| `must_not_contain`    | Optional. `\|`-separated forbidden substrings (e.g. wrong-context dialogue that must not be picked). |
| `label`               | `ok` (current algorithm passes) or `bad_correction` (known failure; documented for later fix). |
| `expected_correction` | Required when `label=bad_correction`: the *ideal* output. |
| `notes`               | Free text. |

Schema is validated at load time (`tests/conftest.py::load_correction_samples`).

## How `bad_correction` works

Rows with `label=bad_correction` run under `pytest.mark.xfail(strict=True)` and
assert against `expected_correction`.

* If the algorithm still produces the wrong answer → `xfail` (silent, expected).
* If the algorithm is fixed and produces `expected_correction` → `XPASS`,
  which **fails** the suite (strict) and prompts you to:
  1. Change `label` to `ok`.
  2. Move `expected_correction` into `expected` (or split into
     `contains_all` substrings).
  3. Clear `expected_correction`.

This way known-bad samples stay in CI without silently masking fixes.

## Running against an annotated export

The default fixture path is `tests/fixtures/correction_samples.csv`.  Point
pytest at any other CSV (same schema) via:

```bash
pytest tests/test_correction_dataset.py \
    --correction-samples=path/to/exported_samples.csv
```

This is the fast path when triaging a real-world capture — export it in this
schema, drop it on disk, run.
