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
from sous_chef.domain.profiles import ProfileModel
from sous_chef.domain.recipes import NutritionEvidence, NutritionRequest, RecipeToolError
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.settings import ConfigurationError


class IngredientLookup(ProfileModel):
    name: Annotated[GroceryName, Field(pattern=r"^[^%_]+$")]


class NutritionEnvelope(ProfileModel):
    data: NutritionEvidence


def create_nutrition_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/nutrition", tags=["nutrition"])

    @router.post("/lookup", response_model=NutritionEnvelope)
    async def lookup(
        payload: IngredientLookup, _user: CurrentUser, request: Request, response: Response
    ) -> NutritionEnvelope | JSONResponse:
        require_no_query(request)
        tool = getattr(request.app.state, "nutrition", None)
        if not isinstance(tool, UsdaNutritionTool):
            raise ConfigurationError("Enable USDA integration before looking up nutrition.")
        nutrition_request = NutritionRequest.model_validate(
            {
                "servings": 1,
                "ingredients": (
                    {"line_id": "ingredient", "name": payload.name, "quantity": 100, "unit": "g"},
                ),
            }
        )
        try:
            evidence = await tool.lookup(
                nutrition_request, deadline=monotonic() + tool.config.timeout_seconds
            )
        except RecipeToolError as error:
            status, code, title = {
                FoundryFailure.TIMEOUT: (504, "nutrition_timeout", "Nutrition lookup timed out"),
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
        response.headers["Cache-Control"] = "no-store"
        return NutritionEnvelope(data=evidence)

    return router
