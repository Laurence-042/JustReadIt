# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared pytest fixtures and CLI options for the test suite.

Currently exposes the ``--correction-samples`` option used by
``tests/test_correction_dataset.py`` to drive end-to-end tests of
``best_match_with_details`` from a CSV dataset.  The default dataset
lives at ``tests/fixtures/correction_samples.csv``; users can point at
an alternative file (for example a freshly exported annotation set)
via ``pytest --correction-samples=path/to/file.csv``.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pytest


_VALID_MATCH_MODES = {"exact", "contains_all", "none"}
_VALID_LABELS = {"ok", "bad_correction"}

_DEFAULT_SAMPLES = Path(__file__).parent / "fixtures" / "correction_samples.csv"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--correction-samples",
        action="store",
        default=str(_DEFAULT_SAMPLES),
        help=(
            "Path to a correction-samples CSV (same schema as "
            "tests/fixtures/correction_samples.csv). Use this to run the "
            "dataset-driven correction tests against an annotated export."
        ),
    )


@dataclass(frozen=True)
class CorrectionSample:
    """A single row of the correction dataset."""

    id: str
    ocr_text: str
    memory_hits: tuple[str, ...]
    needle: str
    match_mode: str           # "exact" | "contains_all" | "none"
    expected: str             # full text (exact) | "|"-joined substrings | ""
    must_not_contain: str     # "|"-joined forbidden substrings, may be ""
    label: str                # "ok" | "bad_correction"
    expected_correction: str  # ideal output for label=bad_correction, may be ""
    notes: str

    @property
    def expected_substrings(self) -> list[str]:
        if not self.expected:
            return []
        return [s for s in self.expected.split("|") if s]

    @property
    def forbidden_substrings(self) -> list[str]:
        if not self.must_not_contain:
            return []
        return [s for s in self.must_not_contain.split("|") if s]


def _validate(sample: CorrectionSample) -> None:
    """Raise ``ValueError`` if a row violates the schema."""
    if not sample.id:
        raise ValueError("row missing id")
    if sample.match_mode not in _VALID_MATCH_MODES:
        raise ValueError(
            f"{sample.id}: match_mode={sample.match_mode!r} not in "
            f"{sorted(_VALID_MATCH_MODES)}"
        )
    if sample.label not in _VALID_LABELS:
        raise ValueError(
            f"{sample.id}: label={sample.label!r} not in {sorted(_VALID_LABELS)}"
        )
    if sample.match_mode == "none":
        if sample.expected:
            raise ValueError(
                f"{sample.id}: match_mode=none must have empty expected"
            )
        if sample.must_not_contain:
            raise ValueError(
                f"{sample.id}: match_mode=none must have empty must_not_contain"
            )
    else:
        if not sample.expected:
            raise ValueError(
                f"{sample.id}: match_mode={sample.match_mode} requires expected"
            )
    if sample.label == "bad_correction" and not sample.expected_correction:
        raise ValueError(
            f"{sample.id}: label=bad_correction requires expected_correction"
        )


def load_correction_samples(path: str | Path) -> list[CorrectionSample]:
    """Parse the correction-samples CSV and validate every row."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"correction samples file not found: {p}")

    samples: list[CorrectionSample] = []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {
            "id", "ocr_text", "memory_hits", "needle", "match_mode",
            "expected", "must_not_contain", "label", "expected_correction",
            "notes",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{p}: missing required columns: {sorted(missing)}"
            )
        for row in reader:
            raw_hits = row["memory_hits"] or "[]"
            try:
                hits = json.loads(raw_hits)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"row {row.get('id')!r}: memory_hits is not valid JSON: {exc}"
                ) from exc
            if not isinstance(hits, list) or not all(
                isinstance(h, str) for h in hits
            ):
                raise ValueError(
                    f"row {row.get('id')!r}: memory_hits must be a JSON array "
                    f"of strings"
                )
            sample = CorrectionSample(
                id=row["id"],
                ocr_text=row["ocr_text"] or "",
                memory_hits=tuple(hits),
                needle=row["needle"] or "",
                match_mode=row["match_mode"],
                expected=row["expected"] or "",
                must_not_contain=row["must_not_contain"] or "",
                label=row["label"],
                expected_correction=row["expected_correction"] or "",
                notes=row["notes"] or "",
            )
            _validate(sample)
            samples.append(sample)

    ids = [s.id for s in samples]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise ValueError(f"{p}: duplicate ids: {sorted(dupes)}")
    return samples


@pytest.fixture(scope="session")
def correction_samples_path(pytestconfig: pytest.Config) -> Path:
    return Path(pytestconfig.getoption("--correction-samples"))


@pytest.fixture(scope="session")
def correction_samples(correction_samples_path: Path) -> list[CorrectionSample]:
    return load_correction_samples(correction_samples_path)


def samples_for_collection() -> Iterable[CorrectionSample]:
    """Load samples at collection time so pytest can parametrize by id.

    Reads ``--correction-samples`` from ``sys.argv`` if present, else the
    default fixture file. Schema/parse errors fall back to an empty list so
    collection itself does not crash; the dataset test will then fail loudly.
    """
    import sys

    path: Path = _DEFAULT_SAMPLES
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--correction-samples" and i + 1 < len(argv):
            path = Path(argv[i + 1])
            break
        if arg.startswith("--correction-samples="):
            path = Path(arg.split("=", 1)[1])
            break
    try:
        return load_correction_samples(path)
    except (FileNotFoundError, ValueError):  # pragma: no cover - surfaced by test
        return []
