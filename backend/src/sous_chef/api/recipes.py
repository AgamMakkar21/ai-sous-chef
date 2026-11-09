from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import UUID4

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.conversations import _query, operation_response
from sous_chef.api.revisions import (
    Revision,
    require_no_query,
    require_revision,
    set_revision_headers,
)
from sous_chef.domain.chat import ChatExecutor, OperationEnvelope, TurnEnvelope
from sous_chef.domain.context import LearnedPreferenceReader
from sous_chef.domain.conversations import HistoryPage
from sous_chef.domain.feedback import SubmitFeedback
from sous_chef.domain.recipe_lifecycle import (
    BehaviorEnvelope,
    ConfirmCooking,
    RecipeEnvelope,
    RejectRecipe,
    ReviseRecipe,
    SaveRecipe,
)
from sous_chef.domain.recipe_queries import RecipeHistoryParameters, RecipeSummary
from sous_chef.services.feedback import FeedbackService
from sous_chef.services.recipe_lifecycle import RecipeLifecycleService
from sous_chef.services.recipe_queries import RecipeQueryService
from sous_chef.settings import Settings

IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")]


def _mutation(request: Request) -> None:
    require_no_query(request)
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("header",)}])


def create_recipe_router(
    settings: Settings,
    executor: ChatExecutor | None = None,
    learned_reader: LearnedPreferenceReader | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["recipes"])
    service = RecipeLifecycleService(settings, executor, learned_reader)
    queries = RecipeQueryService(settings)
    feedback = FeedbackService(settings)

    @router.get("/recipes")
    async def list_recipes(
        user: CurrentUser,
        parameters: Annotated[RecipeHistoryParameters, Query()],
        request: Request,
        response: Response,
    ) -> HistoryPage[RecipeSummary]:
        _query(request)
        response.headers["Cache-Control"] = "no-store"
        return await queries.list(user, parameters)

    @router.get("/recipes/{history_id}")
    async def recipe_detail(
        history_id: UUID4,
        user: CurrentUser,
        request: Request,
        response: Response,
    ) -> RecipeEnvelope:
        require_no_query(request)
        result, etag = await queries.detail(user, history_id)
        set_revision_headers(response, etag)
        return RecipeEnvelope(data=result)

    @router.post("/recipes", status_code=201)
    async def save(
        payload: SaveRecipe,
        user: CurrentUser,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> RecipeEnvelope:
        _mutation(request)
        result, etag = await service.save(user, payload, idempotency_key)
        set_revision_headers(response, etag)
        response.headers["Location"] = f"/api/v1/recipes/{result.history_id}"
        return RecipeEnvelope(data=result)

    @router.post("/recipes/{history_id}/cooked")
    async def cooked(
        history_id: UUID4,
        payload: ConfirmCooking,
        user: CurrentUser,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: Revision,
    ) -> RecipeEnvelope:
        _mutation(request)
        etag = require_revision(request, if_match)
        result, etag = await service.cooked(user, history_id, idempotency_key, etag)
        set_revision_headers(response, etag)
        return RecipeEnvelope(data=result)

    @router.put("/recipes/{history_id}/feedback")
    async def submit_feedback(
        history_id: UUID4,
        payload: SubmitFeedback,
        user: CurrentUser,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: Revision,
    ) -> RecipeEnvelope:
        _mutation(request)
        etag = require_revision(request, if_match)
        result, etag = await feedback.submit(user, history_id, payload, idempotency_key, etag)
        set_revision_headers(response, etag)
        return RecipeEnvelope(data=result)

    @router.post("/generated-recipes/{generated_recipe_id}/rejections", status_code=201)
    async def reject(
        generated_recipe_id: UUID4,
        payload: RejectRecipe,
        user: CurrentUser,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> BehaviorEnvelope:
        _mutation(request)
        response.headers["Cache-Control"] = "no-store"
        result = await service.reject(user, generated_recipe_id, payload.reason, idempotency_key)
        return BehaviorEnvelope(data=result)

    @router.post(
        "/generated-recipes/{generated_recipe_id}/revisions",
        response_model=TurnEnvelope | OperationEnvelope,
    )
    async def revise(
        generated_recipe_id: UUID4,
        payload: ReviseRecipe,
        user: CurrentUser,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> TurnEnvelope | OperationEnvelope | JSONResponse:
        _mutation(request)
        result = await service.revise(user, generated_recipe_id, payload.reason, idempotency_key)
        return operation_response(result, response)

    return router
