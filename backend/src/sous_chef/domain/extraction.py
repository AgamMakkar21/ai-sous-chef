"""Private extraction proposals and verified evidence, never explicit-profile mutations."""

import hashlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID, uuid4

from pydantic import UUID4, AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from sous_chef.domain.context import ContextPreference, LearnedField, ResolvedPreference
from sous_chef.domain.conversations import ConversationModel
from sous_chef.domain.feedback import FeedbackRevision
from sous_chef.domain.profiles import ExplicitPreferences, PreferenceText

EXTRACTION_POLICY_VERSION: Final = 1
MAX_EXTRACTION_INPUT_TOKENS = 6000
MAX_EXTRACTION_OUTPUT_TOKENS = 1000
MAX_EXTRACTION_PRIOR = 5

Owner = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Confidence = Annotated[float, Field(strict=True, ge=0, le=1)]


class ExtractionModel(ConversationModel):
    model_config = ConfigDict(strict=True)


class ExtractionSource(ExtractionModel):
    user_id: Owner = Field(repr=False)
    feedback: FeedbackRevision = Field(repr=False)
    cooked_at: AwareDatetime
    title: str = Field(min_length=1, max_length=160, repr=False)
    ingredients: tuple[PreferenceText, ...] = Field(min_length=1, max_length=30, repr=False)

    @model_validator(mode="after")
    def owned_cooked_feedback(self) -> Self:
        feedback = self.feedback

        def identity(revision: int) -> UUID:
            digest = hashlib.sha256(
                f"{self.user_id}\0recipe-feedback\0{feedback.history_id}:{revision}".encode()
            ).digest()
            return UUID(bytes=digest[:16], version=4)

        previous = (
            identity(feedback.feedback_revision - 1) if feedback.feedback_revision > 1 else None
        )
        if (
            feedback.source_id != feedback.history_id
            or feedback.feedback_id != identity(feedback.feedback_revision)
            or feedback.supersedes_feedback_id != previous
            or self.cooked_at > feedback.observed_at
        ):
            raise ValueError("Extraction requires an owned, cooked feedback revision.")
        return self


class ExtractionProfile(ExtractionModel):
    user_id: Owner = Field(repr=False)
    explicit_preferences: ExplicitPreferences = Field(repr=False)
    learned_preferences: tuple[ContextPreference, ...] = Field(max_length=20, repr=False)


class ExtractionRequest(ExtractionModel):
    current: ExtractionSource = Field(repr=False)
    profile: ExtractionProfile = Field(repr=False)
    prior: tuple[ExtractionSource, ...] = Field(default=(), max_length=99, repr=False)
    now: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    operation_id: UUID4 = Field(default_factory=uuid4)

    @model_validator(mode="after")
    def one_owned_current_source_per_occasion(self) -> Self:
        owner = self.current.user_id
        if self.profile.user_id != owner:
            raise ValueError("Extraction context must belong to one owner.")
        if not self.now - timedelta(days=180) <= self.current.feedback.observed_at <= self.now:
            raise ValueError("Current extraction evidence must be inside the learning window.")
        histories: set[UUID] = set()
        occasions: set[UUID] = set()
        for source in (self.current, *self.prior):
            feedback = source.feedback
            if (
                source.user_id != owner
                or feedback.observed_at > self.now
                or feedback.history_id in histories
                or feedback.occasion_id in occasions
            ):
                raise ValueError("Extraction requires distinct, current, owned occasions.")
            histories.add(feedback.history_id)
            occasions.add(feedback.occasion_id)
        return self


class ExtractionAssertion(ExtractionModel):
    # Standard uuid schema format; exact supplied-source matching also enforces UUIDv4.
    feedback_id: UUID
    source_id: UUID
    source_revision: int = Field(ge=1)
    occasion_id: UUID
    signal_type: Literal["feedback_note"]
    direction: Literal["support", "oppose"]
    excerpt: str = Field(min_length=1, max_length=2000, pattern=r"\S", repr=False)


class PreferenceProposal(ExtractionModel):
    field: LearnedField
    value: PreferenceText = Field(repr=False)
    insight: str = Field(min_length=1, max_length=240, pattern=r"\S", repr=False)
    confidence: Confidence
    evidence: tuple[ExtractionAssertion, ...] = Field(
        min_length=1, max_length=MAX_EXTRACTION_PRIOR + 1, repr=False
    )


class ExtractionDraft(ExtractionModel):
    outcome: Literal["candidates", "no_insight"]
    candidates: tuple[PreferenceProposal, ...] = Field(max_length=10, repr=False)

    @model_validator(mode="after")
    def explicit_outcome(self) -> Self:
        if (self.outcome == "candidates") != bool(self.candidates):
            raise ValueError("Extraction outcome must match its candidates.")
        return self


class ExtractedPreference(ExtractionModel):
    preference: ResolvedPreference = Field(repr=False)
    model_confidence: Confidence
    assertions: tuple[ExtractionAssertion, ...] = Field(
        min_length=1, max_length=MAX_EXTRACTION_PRIOR + 1, repr=False
    )


class ExtractionResult(ExtractionModel):
    outcome: Literal["candidates", "no_insight"]
    candidates: tuple[ExtractedPreference, ...] = Field(max_length=10, repr=False)
    policy_version: Literal[1] = EXTRACTION_POLICY_VERSION
    operation_id: UUID4
    response_id: str = Field(min_length=1, max_length=128, repr=False)
    model_id: str
    deployment: str
    input_tokens: int = Field(ge=1, le=MAX_EXTRACTION_INPUT_TOKENS)
    output_tokens: int = Field(ge=1, le=MAX_EXTRACTION_OUTPUT_TOKENS)


class ExtractionFailure(StrEnum):
    CONFIGURATION = "extraction_configuration"
    CONTEXT_LIMIT = "extraction_context_limit"
    AUTHENTICATION = "extraction_authentication"
    THROTTLED = "extraction_throttled"
    TIMEOUT = "extraction_timeout"
    INVALID_RESULT = "extraction_invalid_result"
    UNAVAILABLE = "extraction_unavailable"
    REJECTED = "extraction_rejected"


class ExtractionError(RuntimeError):
    def __init__(self, code: ExtractionFailure) -> None:
        self.code = code
        super().__init__(f"Preference extraction failed ({code.value}).")
