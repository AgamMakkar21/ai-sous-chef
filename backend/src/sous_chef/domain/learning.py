"""Bounded, deterministic preference reduction from current feedback contributions."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import UUID4, AwareDatetime, Field, model_validator

from sous_chef.domain.context import (
    LearnedCandidate,
    LearnedField,
    PreferenceEvidence,
    ResolvedPreference,
    conflicts_with_explicit,
)
from sous_chef.domain.extraction import ExtractionModel
from sous_chef.domain.profiles import ExplicitPreferences, PreferenceText

MAX_LEARNING_SOURCES = 100
MAX_LEARNED_CANDIDATES = 50


class LearningSource(ExtractionModel):
    history_id: UUID4
    occasion_id: UUID4
    feedback_id: UUID4
    feedback_revision: int = Field(ge=1)
    observed_at: AwareDatetime


class LearningWindow(ExtractionModel):
    role: Literal["learningWindow"] = "learningWindow"
    policy_version: Literal[1] = 1
    generation: int = Field(ge=1)
    sources: tuple[LearningSource, ...] = Field(max_length=MAX_LEARNING_SOURCES, repr=False)


class LearningContribution(ExtractionModel):
    insight_key: str = Field(min_length=1, max_length=100)
    insight: str = Field(min_length=1, max_length=240, repr=False)
    field: LearnedField
    value: PreferenceText = Field(repr=False)
    evidence: PreferenceEvidence = Field(repr=False)


class LearningReceipt(ExtractionModel):
    role: Literal["feedbackApplication"] = "feedbackApplication"
    policy_version: Literal[1] = 1
    source: LearningSource
    outcome: Literal["candidates", "no_insight"]
    contributions: tuple[LearningContribution, ...] = Field(max_length=10, repr=False)

    @model_validator(mode="after")
    def consistent_outcome(self) -> "LearningReceipt":
        if (self.outcome == "candidates") != bool(self.contributions) or len(
            {item.insight_key for item in self.contributions}
        ) != len(self.contributions):
            raise ValueError("A learning receipt requires a unique, consistent contribution set.")
        return self


def independent_evidence(
    items: list[LearningContribution],
) -> tuple[PreferenceEvidence, ...]:
    occasions: dict[UUID, list[PreferenceEvidence]] = defaultdict(list)
    for item in items:
        occasions[item.evidence.occasion_id].append(item.evidence)
    evidence = [
        min(entries, key=lambda entry: (str(entry.source_id), -entry.source_revision))
        for entries in occasions.values()
        if len({entry.direction for entry in entries}) == 1
    ]
    evidence.sort(key=lambda entry: (-entry.observed_at.timestamp(), str(entry.occasion_id)))
    return tuple(evidence)


def advance_window(
    previous: LearningWindow | None, source: LearningSource, *, now: datetime
) -> LearningWindow:
    sources = (
        {
            item.history_id: item
            for item in previous.sources
            if now - timedelta(days=180) <= item.observed_at <= now
        }
        if previous
        else {}
    )
    current = sources.get(source.history_id)
    if now - timedelta(days=180) <= source.observed_at <= now and (
        current is None or current.feedback_revision < source.feedback_revision
    ):
        sources[source.history_id] = source
    ranked = sorted(
        sources.values(),
        key=lambda item: (-item.observed_at.timestamp(), str(item.history_id)),
    )
    occasions = set()
    independent = []
    for item in ranked:
        if item.occasion_id not in occasions:
            occasions.add(item.occasion_id)
            independent.append(item)
    return LearningWindow(
        generation=previous.generation + 1 if previous else 1,
        sources=tuple(independent[:MAX_LEARNING_SOURCES]),
    )


def reduce_preferences(
    contributions: tuple[LearningContribution, ...],
    explicit: ExplicitPreferences,
    *,
    now: datetime,
) -> tuple[ResolvedPreference, ...]:
    grouped: dict[str, list[LearningContribution]] = defaultdict(list)
    for item in contributions:
        if now - timedelta(
            days=180
        ) <= item.evidence.observed_at <= now and not conflicts_with_explicit(
            item.field, item.value, explicit
        ):
            grouped[item.insight_key].append(item)
    resolved = []
    for key, items in grouped.items():
        # Metadata and tied contributions are independent of delivery order.
        items.sort(key=lambda item: (item.field, item.value, item.insight))
        chosen = items[0]
        evidence = independent_evidence(items)
        if not evidence:
            continue
        support = sum(entry.direction == "support" for entry in evidence)
        opposition = len(evidence) - support
        latest = max(entry.observed_at for entry in evidence)
        resolved.append(
            ResolvedPreference(
                candidate=LearnedCandidate(
                    insight_key=key,
                    insight=chosen.insight,
                    confidence=(3 + 3 * support) / (6 + 3 * support + 3 * opposition),
                    evidence_count=len(evidence),
                    supporting_cooked_count=support,
                    support_units=3 * support,
                    opposition_units=3 * opposition,
                    source="recipe_feedback",
                    last_updated=latest,
                    latest_evidence_at=latest,
                    policy_version=1,
                ),
                field=chosen.field,
                value=chosen.value,
                evidence=evidence,
            )
        )
    resolved.sort(
        key=lambda item: (
            item.candidate.confidence,
            item.candidate.latest_evidence_at.timestamp(),
            item.candidate.insight_key,
        )
    )
    return tuple(reversed(resolved[-MAX_LEARNED_CANDIDATES:]))
