"""ASC-25 internal coordination contracts, not public recipe/persistence schemas."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryRequest
from sous_chef.domain.recipes import StructuredRecipe


@dataclass(frozen=True)
class OrchestratorPolicy:
    version: int = 2
    max_model_calls: int = 4
    max_specialist_calls: int = 2
    max_revisions: int = 1
    max_tool_calls: int = 8
    max_web_calls: int = 1
    web_deadline_seconds: float = 10
    max_tool_result_tokens: int = 4_000
    deadline_seconds: int = 60
    max_input_tokens: int = 16_000
    total_input_tokens: int = 32_000
    total_output_tokens: int = 6_000
    output_tokens_per_call: int = 1_500


POLICY = OrchestratorPolicy()
ShortText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]


class CoordinationModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class TokenUsage(CoordinationModel):
    input_tokens: int = Field(ge=1)
    output_tokens: int = Field(ge=0)


class SpecialistArguments(CoordinationModel):
    requirements: ShortText = Field(repr=False)
    time_budget_minutes: int = Field(ge=1, le=240)
    use_web: bool
    web_query: (
        Annotated[
            str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240)
        ]
        | None
    ) = Field(default=None, repr=False)

    @model_validator(mode="after")
    def conditional_query(self) -> Self:
        if self.use_web != (self.web_query is not None):
            raise ValueError("Only a justified web request supplies a public search query.")
        return self


class OpaqueReasoning(CoordinationModel):
    id: str = Field(min_length=1, max_length=256, repr=False)
    encrypted_content: str = Field(min_length=1, max_length=32768, repr=False)


class SpecialistCall(CoordinationModel):
    call_id: str = Field(min_length=1, max_length=256, repr=False)
    arguments: SpecialistArguments = Field(repr=False)
    reasoning: tuple[OpaqueReasoning, ...] = Field(default=(), max_length=8, repr=False)


class OrchestratorAnswer(CoordinationModel):
    status: Literal["clarification", "unable", "accept"]
    explanation: ShortText = Field(repr=False)


class CoordinationStep(CoordinationModel):
    usage: TokenUsage
    call: SpecialistCall | None = Field(default=None, repr=False)
    answer: OrchestratorAnswer | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def one_action(self) -> Self:
        if (self.call is None) == (self.answer is None):
            raise ValueError("A coordination step requires exactly one action.")
        return self


class CoordinationPrompt(FoundryRequest):
    # ASC-23's token-bounded context may exceed the original gateway's character ceiling.
    prompt: str = Field(min_length=1, max_length=131072, pattern=r"\S", repr=False)


class CoordinationRequest(CoordinationModel):
    request: CoordinationPrompt = Field(repr=False)
    max_input_tokens: int = Field(default=POLICY.max_input_tokens, ge=1, le=POLICY.max_input_tokens)
    previous_call: SpecialistCall | None = Field(default=None, repr=False)
    tool_output: str | None = Field(default=None, max_length=16000, repr=False)

    @model_validator(mode="after")
    def paired_output(self) -> Self:
        if (self.previous_call is None) != (self.tool_output is None):
            raise ValueError("A function result requires its original call.")
        return self


class CandidateIngredient(CoordinationModel):
    line_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100, pattern=r"\S", repr=False)


class CandidateInstruction(CoordinationModel):
    text: str = Field(min_length=1, max_length=500, pattern=r"\S", repr=False)
    ingredient_line_ids: tuple[str, ...] = Field(max_length=30)


class RecipeCandidate(CoordinationModel):
    """Specialist's bounded fit/safety projection; never a saved or approved recipe."""

    title: str = Field(min_length=1, max_length=160, pattern=r"\S", repr=False)
    total_minutes: int = Field(ge=1, le=1440)
    ingredients: tuple[CandidateIngredient, ...] = Field(min_length=1, max_length=30, repr=False)
    instructions: tuple[CandidateInstruction, ...] = Field(min_length=1, max_length=30, repr=False)
    fit_reasons: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=240, pattern=r"\S")], ...
    ] = Field(min_length=1, max_length=10, repr=False)

    @model_validator(mode="after")
    def ingredient_references(self) -> Self:
        ids = {line.line_id for line in self.ingredients}
        if len(ids) != len(self.ingredients) or any(
            not set(step.ingredient_line_ids) <= ids for step in self.instructions
        ):
            raise ValueError("Ingredient IDs must be unique and instruction references valid.")
        return self


class SpecialistResult(CoordinationModel):
    candidate: RecipeCandidate = Field(repr=False)
    usage: TokenUsage
    recipe: StructuredRecipe | None = Field(default=None, repr=False)


class OrchestrationResult(CoordinationModel):
    status: Literal["clarification", "unable", "candidate"]
    explanation: ShortText = Field(repr=False)
    candidate: RecipeCandidate | None = Field(default=None, repr=False)
    recipe: StructuredRecipe | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def candidate_state(self) -> Self:
        if (self.status == "candidate") != (self.candidate is not None):
            raise ValueError("Only a candidate result contains a recipe projection.")
        if self.recipe is not None and self.status != "candidate":
            raise ValueError("Only a candidate result contains a structured recipe.")
        return self


class OrchestratorFailure(StrEnum):
    INVALID_RESULT = "orchestrator_invalid_result"
    BUDGET_EXHAUSTED = "orchestrator_budget_exhausted"
    TIMEOUT = "orchestrator_timeout"
    SPECIALIST_FAILED = "orchestrator_specialist_failed"
    CANDIDATE_REJECTED = "orchestrator_candidate_rejected"


class OrchestratorError(RuntimeError):
    def __init__(self, code: OrchestratorFailure, operation_id: UUID) -> None:
        self.code = code
        self.operation_id = operation_id
        super().__init__(f"{code.value}; operation_id={operation_id}.")


class Coordinator(Protocol):
    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep: ...


class SpecialistBudget(Protocol):
    def model_allowance(self) -> tuple[int, int]:
        """Check token headroom for a reserved call after any nested invocation."""
        ...

    def model(self) -> None:
        """Reserve any additional model invocation before executing it."""
        ...

    def usage(self, usage: TokenUsage) -> None:
        """Account for an additional invocation without double-counting returned usage."""
        ...

    def tool(self, operation: str) -> None:
        """Reserve an allowlisted tool call before executing it, including retries."""
        ...


class RecipeSpecialist(Protocol):
    async def generate(
        self,
        context: RequestContext,
        arguments: SpecialistArguments,
        *,
        operation_id: UUID,
        budget: SpecialistBudget,
        max_input_tokens: int,
        max_output_tokens: int,
        deadline: float,
    ) -> SpecialistResult:
        """Use the pinned managed specialist, with no autonomous/unaccounted nested calls.

        Share the supplied budget with every tool adapter; return provider-counted usage
        including reasoning. Raise an explicit failure, never a successful substitute.
        Live user-chat composition is ASC-38; this seam also supports ASC-26's
        structured specialist with explicitly supplied grounding tool adapters.
        """
        ...
