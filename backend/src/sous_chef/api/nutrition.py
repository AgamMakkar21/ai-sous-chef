from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.problems import problem_response
from sous_chef.api.revisions import require_no_query
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.groceries import GroceryName
from sous_chef.domain.nutrition import FdcId, UsdaFoodSearch
from sous_chef.domain.profiles import ProfileModel
from sous_chef.domain.recipes import NutritionEvidence, NutritionRequest, RecipeToolError
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.settings import ConfigurationError


class IngredientLookup(ProfileModel):
    name: Annotated[GroceryName, Field(pattern=r"^[^%_]+$")]


class SelectedIngredientLookup(IngredientLookup):
    food_id: FdcId | None = None


class NutritionEnvelope(ProfileModel):
    data: NutritionEvidence


def create_nutrition_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/nutrition", tags=["nutrition"])

    @router.post("/lookup", response_model=NutritionEnvelope)
    async def lookup(
        payload: SelectedIngredientLookup, _user: CurrentUser, request: Request, response: Response
    ) -> NutritionEnvelope | JSONResponse:
        require_no_query(request)
        tool = configured_tool(request)
        nutrition_request = NutritionRequest.model_validate(
            {
                "servings": 1,
                "ingredients": (
                    {"line_id": "ingredient", "name": payload.name, "quantity": 100, "unit": "g"},
                ),
                "selectedFoodIds": {"ingredient": payload.food_id} if payload.food_id else {},
            }
        )
        try:
            evidence = await tool.lookup(
                nutrition_request, deadline=monotonic() + tool.config.timeout_seconds
            )
        except RecipeToolError as error:
            return failed(error)
        response.headers["Cache-Control"] = "no-store"
        return NutritionEnvelope(data=evidence)

    def configured_tool(request: Request) -> UsdaNutritionTool:
        tool = getattr(request.app.state, "nutrition", None)
        if not isinstance(tool, UsdaNutritionTool):
            raise ConfigurationError("Enable USDA integration before looking up nutrition.")
        return tool

    def failed(error: RecipeToolError) -> JSONResponse:
        status, code, title = {
            FoundryFailure.TIMEOUT: (504, "nutrition_timeout", "Nutrition lookup timed out"),
            FoundryFailure.THROTTLED: (429, "nutrition_throttled", "USDA API quota exceeded"),
            FoundryFailure.INVALID_RESULT: (
                502,
                "nutrition_invalid_result",
                "Invalid nutrition response",
            ),
            FoundryFailure.REJECTED: (422, "invalid_input", "Invalid ingredient lookup"),
        }.get(error.code, (503, "nutrition_unavailable", "Nutrition service unavailable"))
        return problem_response(
            status, code, title, "Nutrition could not be retrieved. No estimate was invented."
        )

    @router.post("/search", response_model=UsdaFoodSearch)
    async def search(
        payload: IngredientLookup, _user: CurrentUser, request: Request, response: Response
    ) -> UsdaFoodSearch | JSONResponse:
        require_no_query(request)
        tool = configured_tool(request)
        try:
            matches = await tool.search(
                payload.name, deadline=monotonic() + tool.config.timeout_seconds
            )
        except RecipeToolError as error:
            return failed(error)
        response.headers["Cache-Control"] = "no-store"
        return matches

    return router
