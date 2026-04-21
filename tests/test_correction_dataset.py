# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data-driven end-to-end tests for ``best_match_with_details``.

Each row of ``tests/fixtures/correction_samples.csv`` is parametrized as a
single test.  Rows labelled ``bad_correction`` are marked ``xfail(strict=True)``
so that fixing the algorithm flips them to XPASS and forces a manual update of
the dataset (change ``label`` to ``ok`` and copy ``expected_correction`` into
``expected``).

To run against an annotated export instead of the bundled fixture::

    pytest tests/test_correction_dataset.py \
        --correction-samples=path/to/exported_samples.csv
"""
from __future__ import annotations

import pytest

from src.correction import best_match_with_details

from tests.conftest import CorrectionSample, samples_for_collection


def _params() -> list[pytest.param]:
    return [pytest.param(s, id=s.id) for s in samples_for_collection()]


def _assert_match(result_text: str, sample: CorrectionSample) -> None:
    """Apply expected / must_not_contain checks against ``result_text``."""
    if sample.match_mode == "exact":
        assert result_text == sample.expected, (
            f"[{sample.id}] expected exact match\n"
            f"  expected: {sample.expected!r}\n"
            f"  actual:   {result_text!r}"
        )
    elif sample.match_mode == "contains_all":
        for sub in sample.expected_substrings:
            assert sub in result_text, (
                f"[{sample.id}] missing substring {sub!r} in {result_text!r}"
            )
    for forbidden in sample.forbidden_substrings:
        assert forbidden not in result_text, (
            f"[{sample.id}] forbidden substring {forbidden!r} found in "
            f"{result_text!r}"
        )


@pytest.mark.parametrize("sample", _params())
def test_correction_sample(sample: CorrectionSample) -> None:
    """End-to-end OCR + memory → corrected text, driven by CSV dataset."""
    result = best_match_with_details(
        sample.ocr_text,
        list(sample.memory_hits),
        sample.needle,
    )

    if sample.match_mode == "none":
        actual = result.text if result is not None else None
        assert result is None, f"[{sample.id}] expected None, got {actual!r}"
        return

    assert result is not None, f"[{sample.id}] expected a match, got None"
    _assert_match(result.text, sample)


def test_dataset_loads(correction_samples: list[CorrectionSample]) -> None:
    """Sanity check: the CSV file parses and contains rows."""
    assert correction_samples, "correction_samples.csv produced zero rows"
    # Every id is unique (also enforced by the loader).
    ids = [s.id for s in correction_samples]
    assert len(ids) == len(set(ids))
