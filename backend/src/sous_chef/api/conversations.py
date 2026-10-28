from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import UUID4

from sous_chef.api.authentication import CurrentUser
from sous_chef.domain.conversations import (
    ConversationEnvelope,
    ConversationSummary,
    ConversationTurn,
    CreateConversation,
    HistoryPage,
    HistoryParameters,
)
from sous_chef.services.conversations import ConversationService
from sous_chef.settings import Settings


def _query(request: Request) -> None:
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])


def create_conversation_router(settings: Settings) -> APIRouter:
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

    return router
