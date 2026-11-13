import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.evaluate_efficiency import compare


@pytest.fixture
def evidence() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "evaluations" / "model-efficiency-baseline-v1.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


def test_recorded_comparison_retains_inputs_and_quality(evidence: dict[str, Any]) -> None:
    assert compare(evidence["before"], evidence["after"]) == evidence["comparison"]


@pytest.mark.parametrize("key", ["semantic_input_sha256", "backend_context_sha256"])
def test_changed_required_context_cannot_claim_savings(evidence: dict[str, Any], key: str) -> None:
    after = deepcopy(evidence["after"])
    after["contexts"]["pantry-3"][key] = "changed"
    with pytest.raises(ValueError, match="Required context changed"):
        compare(evidence["before"], after)


def test_failed_quality_or_changed_model_calls_cannot_claim_savings(
    evidence: dict[str, Any],
) -> None:
    after = deepcopy(evidence["after"])
    after["deterministic_passed"] = False
    with pytest.raises(ValueError, match="quality checks must pass"):
        compare(evidence["before"], after)
    after = deepcopy(evidence["after"])
    after["replay"][0]["model_calls"] += 1
    with pytest.raises(ValueError, match="model-call counts changed"):
        compare(evidence["before"], after)
