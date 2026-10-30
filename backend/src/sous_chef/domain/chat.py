"""Send contracts and an injected execution seam; no production fake or provider fallback."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self

from pydantic import UUID4, Field, StringConstraints, model_validator

from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.context import RequestContext
from sous_chef.domain.conversations import ConversationModel, ConversationTurn
from sous_chef.domain.profiles import ExplicitPreferences
from sous_chef.domain.recipes import StructuredRecipe

MessageText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)
]
Reference = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
TurnStatus = Literal["pending", "completed", "failed", "outcomeUnknown"]
Clarification = Literal["request", "ingredients", "time_budget", "constraints"]
CLARIFICATIONS: dict[Clarification, str] = {
    "request": "Which dish would you like to make?",
    "ingredients": "Which ingredients would you like to use?",
    "time_budget": "What cooking time budget should I use?",
    "constraints": "Which ingredient or request constraint would you like to change?",
}


class SendMessage(ConversationModel):
    model_config = {"populate_by_name": False}

    content: MessageText = Field(repr=False)
    retry_of_turn_id: UUID4 | None = None


class ValidatedReply(ConversationModel):
    """Untrusted executor data; ChatService owns all final public text."""

    model_config = {"revalidate_instances": "always"}

    content: MessageText = Field(repr=False)
    kind: Literal["recipe", "clarification", "unable"] = "clarification"
    clarification: Clarification = "request"
    entity_id: Reference
    generated_recipe_id: UUID4 | None = None
    provider_response_id: Reference | None = Field(default=None, repr=False)
    recipe: StructuredRecipe | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def recipe_reference(self) -> Self:
        if (self.recipe is None) != (self.generated_recipe_id is None):
            raise ValueError("A generated recipe reference requires its full structured candidate.")
        if (self.kind == "recipe") != (self.recipe is not None):
            raise ValueError("Only a recipe reply can carry a structured candidate.")
        return self


@dataclass(frozen=True)
class ChatExecution:
    user: AuthenticatedUser
    conversation_id: UUID4
    turn_id: UUID4
    operation_id: UUID4
    context: RequestContext
    deadline: float

    @property
    def content(self) -> str:
        return self.context.current_request

    @property
    def explicit_preferences(self) -> ExplicitPreferences:
        return self.context.explicit_preferences


class ChatExecutor(Protocol):
    async def execute(self, request: ChatExecution) -> ValidatedReply: ...


class ExecutionFailed(RuntimeError):
    """Executor confirms that no successful response needs reconciliation."""


class SendConflict(ValueError):
    """Another operation owns the conversation, or this turn is not retryable."""


class OperationView(ConversationModel):
    operation_id: UUID4
    conversation_id: UUID4
    turn_id: UUID4
    status: TurnStatus
    failure_code: (
        Literal["execution_failed", "invalid_assistant_response", "execution_outcome_unknown"]
        | None
    ) = None
    turn: ConversationTurn


class OperationEnvelope(ConversationModel):
    data: OperationView


class TurnEnvelope(ConversationModel):
    data: ConversationTurn
