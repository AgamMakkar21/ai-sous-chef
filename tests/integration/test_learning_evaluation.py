import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from time import monotonic
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from openai.types.responses import Response
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.context import LearnedCandidate
from sous_chef.domain.cosmos import NewRecord, RecordKind, RecordPatch
from sous_chef.domain.extraction import ExtractionRequest, ExtractionResult, ExtractionSource
from sous_chef.domain.profiles import ExplicitPreferences
from sous_chef.services import context as context_policy
from sous_chef.services.context import ContextService
from sous_chef.services.learning import LearningService
from sous_chef.services.recipe_lifecycle import _identity
from test_cosmos_repository import SyntheticCredential, Transport, settings
from test_cosmos_repository import harness as harness
from test_learning import HISTORY, OWNER, USERS, seed_event, seed_source
from test_preference_extraction import config, profile, proposal, response_body, source

from scripts.evaluate_learning import Case, Step, load_suite
from scripts.quality_capture import Case as RecipeCase
from scripts.quality_capture import capture
from worker.extraction import _result

SUITE = load_suite()
REQUEST = "Prepare a beginner rice and carrot dinner in 30 minutes."


class ScriptedExtractor:
    def __init__(self, value: str) -> None:
        self.value = value
        self.signals: dict[UUID, str] = {}
        self.calls = 0

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls += 1
        signal = self.signals[request.current.feedback.feedback_id]
        candidate = proposal(request.current, value=self.value)
        candidate["evidence"][0]["direction"] = signal
        draft = (
            {"outcome": "no_insight", "candidates": []}
            if signal == "none"
            else {"outcome": "candidates", "candidates": [candidate]}
        )
        return _result(
            Response.model_validate(response_body(draft)), request, (request.current,), config()
        )


async def deliver(
    repository: CosmosRepository,
    case: Case,
    step: Step,
    sources: dict[str, ExtractionSource],
    delivered: set[UUID],
) -> ExtractionSource:
    if step.history not in sources:
        sources[step.history] = source(
            history=_identity(OWNER, "evaluation-history", f"{case.id}:{step.history}"), days=30
        )
    base = sources[step.history]
    feedback = base.feedback.model_copy(
        update={
            "feedback_id": _identity(
                OWNER, "recipe-feedback", f"{base.feedback.history_id}:{step.revision}"
            ),
            "feedback_revision": step.revision,
            "supersedes_feedback_id": _identity(
                OWNER, "recipe-feedback", f"{base.feedback.history_id}:{step.revision - 1}"
            )
            if step.revision > 1
            else None,
            "note": step.note,
            "observed_at": SUITE.now - timedelta(days=30 - step.day),
        }
    )
    item = base.model_copy(update={"feedback": feedback})
    if feedback.feedback_id not in delivered:
        current = await repository.read(OWNER, HISTORY, str(feedback.history_id))
        if current is None:
            await seed_source(repository, item)
        else:
            await seed_event(repository, item)
            current_revision = current.body["feedbackRevision"]
            assert isinstance(current_revision, int)
            if step.revision > current_revision:
                await repository.patch(
                    OWNER,
                    HISTORY,
                    str(feedback.history_id),
                    RecordPatch(
                        etag=current.etag,
                        fields={
                            "feedbackRevision": step.revision,
                            "note": step.note,
                            "processingStatus": "pending",
                        },
                    ),
                )
        delivered.add(feedback.feedback_id)
    return item


@pytest.mark.parametrize("case", SUITE.cases, ids=lambda case: case.id)
def test_longitudinal_case(
    case: Case,
    harness: tuple[Transport, SyntheticCredential],
    record_property: Callable[[str, object], None],
) -> None:
    async def run() -> dict[str, Any]:
        extractor = ScriptedExtractor(case.value)
        sources: dict[str, ExtractionSource] = {}
        delivered: set[UUID] = set()
        checkpoints: list[dict[str, Any]] = []
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await repository.create(
                OWNER,
                NewRecord(
                    kind=RecordKind.PROFILE,
                    fields={
                        "onboardingCompleted": True,
                        "explicitPreferences": profile().explicit_preferences.model_dump(
                            mode="json", by_alias=True
                        ),
                        "groceries": [],
                        "learnedPreferences": [],
                    },
                ),
            )
            for index, step in enumerate(case.steps):
                item = await deliver(repository, case, step, sources, delivered)
                extractor.signals[item.feedback.feedback_id] = step.signal
                # Restart the service on every delivery; deduplication must be durable.
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=SUITE.now
                )
                current = await repository.read(OWNER, USERS, OWNER.user_id)
                assert current is not None
                if index == len(case.steps) - 1 and case.final_explicit:
                    explicit_body = current.body["explicitPreferences"]
                    assert isinstance(explicit_body, dict)
                    explicit = ExplicitPreferences.model_validate(
                        explicit_body | case.final_explicit
                    ).model_dump(mode="json", by_alias=True)
                    current = await repository.patch(
                        OWNER,
                        USERS,
                        OWNER.user_id,
                        RecordPatch(etag=current.etag, fields={"explicitPreferences": explicit}),
                    )
                with patch("sous_chef.services.context.datetime") as clock:
                    clock.now.return_value = SUITE.now
                    context = await ContextService().build(
                        repository,
                        OWNER,
                        uuid4(),
                        current,
                        REQUEST,
                        deadline=monotonic() + 10,
                    )
                candidate_body = current.body["learnedPreferences"]
                assert isinstance(candidate_body, list)
                candidates = [LearnedCandidate.model_validate(item) for item in candidate_body]
                actual = candidates[0] if candidates else None
                counts_correct = (
                    len(candidates) == int(step.confidence is not None)
                    and (actual.evidence_count if actual else 0) == step.support + step.opposition
                    and (actual.supporting_cooked_count if actual else 0) == step.support
                    and (actual.support_units if actual else 0) == 3 * step.support
                    and (actual.opposition_units if actual else 0) == 3 * step.opposition
                    and (
                        actual.confidence == pytest.approx(step.confidence)
                        if actual
                        else step.confidence is None
                    )
                )
                checkpoints.append(
                    {
                        "count_correct": counts_correct,
                        "inclusion_correct": bool(context.learned_preferences) == step.included,
                        "confidence": actual.confidence if actual else None,
                        "evidence_count": actual.evidence_count if actual else 0,
                        "included": bool(context.learned_preferences),
                    }
                )
            service = LearningService(repository)
            _, window = await service._window(OWNER)
            current_evidence = await service._current(OWNER, window, now=SUITE.now)
            histories = {item.feedback.history_id: name for name, item in sources.items()}
            evidence = [
                f"{histories[contribution.evidence.source_id]}:"
                f"{contribution.evidence.source_revision}:{contribution.evidence.direction}"
                for _, receipt in current_evidence
                for contribution in receipt.contributions
            ]
        explicit_only = context.model_copy(update={"learned_preferences": ()})
        recipe_case = RecipeCase(
            id=case.id, request=REQUEST, expected=("candidate",), web="forbidden"
        )
        arms: dict[str, Any] = {}
        for name, matched in (("explicit_only", explicit_only), ("learned_enabled", context)):
            observation = await capture(recipe_case, matched, None)
            recipe = observation.result.recipe if observation.result else None
            contains = (
                any(ingredient.name.casefold() == case.value for ingredient in recipe.ingredients)
                if recipe
                else None
            )
            arms[name] = {
                "context": matched.model_dump(mode="json", by_alias=True),
                "recipe_title": recipe.title if recipe else None,
                "contains_target": contains,
                "aligned": contains == case.heldout_likes
                if contains is not None and case.heldout_likes is not None
                else None,
                "error": observation.error_code,
            }
        confidence = checkpoints[-1]["confidence"]
        return {
            "case_id": case.id,
            "correct_checkpoints": sum(row["count_correct"] for row in checkpoints),
            "correct_inclusion": sum(row["inclusion_correct"] for row in checkpoints),
            "checkpoints": checkpoints,
            "evidence": {
                "correct": len(set(evidence) & set(case.expected_evidence)),
                "emitted": len(evidence),
                "expected": len(case.expected_evidence),
                "observed": sorted(evidence),
            },
            "extractions": extractor.calls,
            "included": bool(context.learned_preferences),
            "precedence": not context.learned_preferences
            and all(arm["contains_target"] is False for arm in arms.values())
            if case.final_explicit
            else None,
            "brier": (confidence - int(case.heldout_likes)) ** 2
            if confidence is not None and case.heldout_likes is not None
            else None,
            "arms": arms,
        }

    result = asyncio.run(run())
    record_property("learning_observation", json.dumps(result))
    assert result["correct_checkpoints"] == result["correct_inclusion"] == len(case.steps)
    assert result["evidence"]["observed"] == sorted(case.expected_evidence)
    assert result["extractions"] == case.extractions
    assert result["precedence"] is not False
    before, after = result["arms"]["explicit_only"], result["arms"]["learned_enabled"]
    assert (
        before["context"] | {"learnedPreferences": after["context"]["learnedPreferences"]}
        == after["context"]
    )
    assert all(arm["recipe_title"] is not None and arm["error"] is None for arm in (before, after))


@pytest.mark.parametrize(
    ("case_id", "setting", "bypass"),
    [
        ("single-citrus", "MIN_LEARNED_COOKED_SUPPORT", 1),
        ("contradictory-feedback", "MIN_LEARNED_CONFIDENCE", 0.0),
    ],
)
def test_experiment_detects_weakened_production_inclusion_policy(
    case_id: str,
    setting: str,
    bypass: float,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_policy, setting, bypass)
    case = next(case for case in SUITE.cases if case.id == case_id)
    with pytest.raises(AssertionError):
        test_longitudinal_case(case, harness, lambda name, value: None)
