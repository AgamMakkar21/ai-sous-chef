from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import UUID4

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.problems import problem_response
from sous_chef.domain.chat import (
    ChatExecutor,
    OperationEnvelope,
    SendMessage,
    TurnEnvelope,
)
from sous_chef.domain.conversations import (
    ConversationEnvelope,
    ConversationSummary,
    ConversationTurn,
    CreateConversation,
    HistoryPage,
    HistoryParameters,
)
from sous_chef.services.chat import ChatService
from sous_chef.services.conversations import ConversationService
from sous_chef.settings import Settings


def _query(request: Request) -> None:
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])


def create_conversation_router(
    settings: Settings, executor: ChatExecutor | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.post("", status_code=201)
    async def create(
        payload: CreateConversation,
        user: CurrentUser,
        response: Response,
        request: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
        ],
    ) -> ConversationEnvelope:
        if request.query_params or len(request.headers.getlist("idempotency-key")) != 1:
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("request",)}])
        summary, etag = await ConversationService(settings).create(user, payload, idempotency_key)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-store"
        response.headers["Location"] = f"/api/v1/conversations/{summary.conversation_id}/turns"
        return ConversationEnvelope(data=summary)

    @router.get("")
    async def list_conversations(
        user: CurrentUser,
        parameters: Annotated[HistoryParameters, Query()],
        request: Request,
        response: Response,
    ) -> HistoryPage[ConversationSummary]:
        _query(request)
        response.headers["Cache-Control"] = "no-store"
        return await ConversationService(settings).list(user, parameters)

    @router.get("/{conversation_id}/turns")
    async def list_turns(
        conversation_id: UUID4,
        user: CurrentUser,
        parameters: Annotated[HistoryParameters, Query()],
        request: Request,
        response: Response,
    ) -> HistoryPage[ConversationTurn]:
        _query(request)
        response.headers["Cache-Control"] = "no-store"
        return await ConversationService(settings).turns(user, conversation_id, parameters)

    @router.post("/{conversation_id}/turns", response_model=TurnEnvelope | OperationEnvelope)
    async def send_turn(
        conversation_id: UUID4,
        payload: SendMessage,
        user: CurrentUser,
        response: Response,
        request: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
        ],
    ) -> TurnEnvelope | OperationEnvelope | JSONResponse:
        if request.query_params or len(request.headers.getlist("idempotency-key")) != 1:
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("request",)}])
        operation = await ChatService(settings, executor).send(
            user, conversation_id, payload, idempotency_key
        )
        location = f"/api/v1/conversations/{conversation_id}/operations/{operation.operation_id}"
        response.headers["Location"] = location
        response.headers["Cache-Control"] = "no-store"
        if operation.status == "completed":
            return TurnEnvelope(data=operation.turn)
        if operation.status == "pending":
            response.status_code = 202
            response.headers["Retry-After"] = "2"
            return OperationEnvelope(data=operation)
        unknown = operation.status == "outcomeUnknown"
        failure = problem_response(
            504 if unknown else 502,
            operation.failure_code or "execution_failed",
            "Chat outcome unknown" if unknown else "Chat execution failed",
            "Inspect the operation before taking any further action."
            if unknown
            else "Inspect the failed turn; explicitly retry it with a new key.",
        )
        failure.headers["Location"] = location
        return failure

    @router.get("/{conversation_id}/operations/{operation_id}")
    async def inspect_operation(
        conversation_id: UUID4,
        operation_id: UUID4,
        user: CurrentUser,
        request: Request,
        response: Response,
    ) -> OperationEnvelope:
        if request.query_params:
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])
        response.headers["Cache-Control"] = "no-store"
        operation = await ChatService(settings, executor).inspect(
            user, conversation_id, operation_id
        )
        if operation.status == "pending":
            response.headers["Retry-After"] = "2"
        return OperationEnvelope(data=operation)

    return router
