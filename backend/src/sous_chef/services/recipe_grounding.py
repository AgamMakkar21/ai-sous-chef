"""Backend-shaped queries and bounded, untrusted evidence for the MVP recipe index."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.orchestrator import SpecialistArguments
from sous_chef.domain.recipe_corpus import DATASET_ID, SOURCE_FILE
from sous_chef.domain.recipes import (
    CuratedRecipe,
    RecipeGrounding,
    RecipeToolError,
)
from sous_chef.services.context import _encoding


def retrieval_query(context: RequestContext, arguments: SpecialistArguments) -> str:
    """Never include user IDs, conversation history or learned profile evidence."""
    preferences = context.explicit_preferences
    data = {
        "requirements": arguments.requirements,
        "timeBudgetMinutes": arguments.time_budget_minutes,
        "allergies": preferences.allergies,
        "dietaryRestrictions": preferences.dietary_restrictions,
        "cookingSkill": preferences.cooking_skill,
        "groceries": [row.name for row in context.groceries],
    }
    query = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(query) > 2000:
        # Do not silently drop mandatory restrictions to satisfy a query limit.
        raise RecipeToolError(FoundryFailure.REJECTED)
    return query


class _Chunk(BaseModel):
    model_config = ConfigDict(strict=True)
    ref_id: str = Field(min_length=1, max_length=128)
    grounding: str


class _TextContent(BaseModel):
    model_config = ConfigDict(strict=True)
    type: str
    text: str


class _McpResult(BaseModel):
    model_config = ConfigDict(strict=True)
    content: list[_TextContent] = Field(min_length=1, max_length=1)
    isError: bool = False


def parse_mcp_grounding(
    output: str, *, max_results: int, max_context_tokens: int
) -> RecipeGrounding:
    """Search MCP returns extracted chunks, NOT the REST references envelope."""
    if len(output) > 16000:
        raise RecipeToolError(FoundryFailure.INVALID_RESULT)
    try:
        chunks = json.loads(output)
        if isinstance(chunks, dict):
            envelope = _McpResult.model_validate(chunks.get("result", chunks))
            if envelope.isError:
                raise RecipeToolError(FoundryFailure.UNAVAILABLE)
            if envelope.content[0].type != "text":
                raise ValueError
            chunks = json.loads(envelope.content[0].text)
        if not isinstance(chunks, list) or len(chunks) > max_results:
            raise ValueError
        references: set[str] = set()
        recipes: list[CuratedRecipe] = []
        for value in chunks:
            chunk = _Chunk.model_validate(value)
            if chunk.ref_id in references:
                raise ValueError
            references.add(chunk.ref_id)
            recipe = CuratedRecipe.model_validate_json(chunk.grounding)
            source = recipe.source
            expected = (
                f"https://huggingface.co/datasets/{DATASET_ID}/blob/{source.version}/{SOURCE_FILE}"
            )
            if (
                re.fullmatch(r"hf-food-recipes-[0-9]{1,20}", source.source_id) is None
                or re.fullmatch(r"[0-9a-f]{40}", source.version) is None
                or source.reference != expected
            ):
                raise ValueError
            recipes.append(recipe)
        grounding = RecipeGrounding(
            status="available" if recipes else "empty", recipes=tuple(recipes)
        )
        tokens = _encoding().encode(output, disallowed_special=())
        if len(tokens) > max_context_tokens:
            raise ValueError
        return grounding
    except (ValueError, ValidationError):
        raise RecipeToolError(FoundryFailure.INVALID_RESULT) from None
