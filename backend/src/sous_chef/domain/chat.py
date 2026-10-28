"""Send contracts and an injected execution seam; no production fake or provider fallback."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import UUID4, Field, StringConstraints

from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.conversations import ConversationModel, ConversationTurn
from sous_chef.domain.profiles import ExplicitPreferences

MessageText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)
]
Reference = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
TurnStatus = Literal["pending", "completed", "failed", "outcomeUnknown"]


class SendMessage(ConversationModel):
    model_config = {"populate_by_name": False}

    content: MessageText = Field(repr=False)
    retry_of_turn_id: UUID4 | None = None


class ValidatedReply(ConversationModel):
    """Only final, safety-validated public text may cross this persistence boundary."""

    model_config = {"revalidate_instances": "always"}

    content: MessageText = Field(repr=False)
    entity_id: Reference
    generated_recipe_id: UUID4 | None = None
    provider_response_id: Reference | None = Field(default=None, repr=False)


@dataclass(frozen=True)
class ChatExecution:
    user: AuthenticatedUser
    conversation_id: UUID4
    turn_id: UUID4
    operation_id: UUID4
    content: str
    explicit_preferences: ExplicitPreferences
    deadline: float


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
