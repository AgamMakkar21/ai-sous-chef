"""Invocation contracts shared by the backend and its New Foundry adapter."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from sous_chef.domain.agent_identity import AgentRole


class FoundryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    role: AgentRole
    prompt: str = Field(min_length=1, max_length=16000, pattern=r"\S", repr=False)
    operation_id: UUID = Field(default_factory=uuid4)
    max_output_tokens: int = Field(default=2048, ge=16, le=8192)


class FoundryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    role: AgentRole
    operation_id: UUID
    response_id: str = Field(min_length=1, max_length=256, repr=False)
    text: str = Field(min_length=1, max_length=131072, pattern=r"\S", repr=False)


class FoundryFailure(StrEnum):
    AUTHENTICATION = "foundry_authentication"
    THROTTLED = "foundry_throttled"
    TIMEOUT = "foundry_timeout"
    INVALID_RESULT = "foundry_invalid_result"
    UNAVAILABLE = "foundry_unavailable"
    NO_RESULTS = "foundry_no_results"
    REJECTED = "foundry_rejected"


class FoundryGatewayError(RuntimeError):
    """Safe failure metadata, never the service's message or request content."""

    def __init__(self, code: FoundryFailure, request: FoundryRequest) -> None:
        self.code = code
        self.role = request.role
        self.operation_id = request.operation_id
        super().__init__(f"{code.value}; operation_id={request.operation_id}.")
