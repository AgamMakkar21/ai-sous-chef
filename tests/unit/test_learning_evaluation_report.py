from copy import deepcopy
from typing import Any

import pytest

from scripts.evaluate_learning import load_suite, report


def observations() -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.id,
            "passed": True,
            "evidence": {
                "correct": len(case.expected_evidence),
                "emitted": len(case.expected_evidence),
            },
            "correct_checkpoints": len(case.steps),
            "correct_inclusion": len(case.steps),
            "precedence": True if case.final_explicit else None,
            "included": case.steps[-1].included,
            "brier": 0.25 if case.heldout_likes is not None else None,
            "arms": {
                arm: {"aligned": True if case.heldout_likes is not None else None}
                for arm in ("explicit_only", "learned_enabled")
            },
        }
        for case in load_suite().cases
    ]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "foreign", "failed"])
def test_incomplete_or_failed_experiment_cannot_pass(failure: str) -> None:
    rows = observations()
    if failure == "missing":
        rows.pop()
    elif failure == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif failure == "foreign":
        rows[-1]["case_id"] = "foreign"
    else:
        rows[0]["passed"] = False
    result = report(load_suite(), rows)
    assert not result["deterministic_passed"]
    assert not result["live_quality_measured"]
    assert result["sample"]["sequences"] == 10
    assert result["count_correctness"]["required"] == 25


def test_missing_alignment_and_calibration_stay_unknown_not_zero_or_dropped() -> None:
    rows = observations()
    rows[0]["arms"]["learned_enabled"]["aligned"] = None
    rows[0]["brier"] = None
    result = report(load_suite(), rows)
    alignment = result["recommendation_alignment"]["learned_enabled"]
    assert alignment == {"aligned": 5, "scored": 5, "required": 6, "rate": None}
    assert result["recommendation_alignment"]["explicit_only"]["rate"] == 1
    assert result["confidence_calibration"]["learned_enabled_brier"] is None
    assert result["confidence_calibration"]["scored"] == 5
    assert result["confidence_calibration"]["required"] == 6
    assert result["confidence_calibration"]["explicit_only_brier"] is None
