"""Current-policy delivery/save/reuse hooks; no provider calls or recipe-save API."""

import json
from dataclasses import dataclass, field
from uuid import UUID

from sous_chef.adapters.cosmos import CosmosRepository
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import ProductContainer
from sous_chef.domain.food_safety import SafetyProfile
from sous_chef.domain.profiles import require_onboarding
from sous_chef.domain.recipe_safety import (
    POLICY_VERSION,
    RecipeSafetyError,
    RecipeSafetyFailure,
    render_recipe,
    validate_final_recipe,
)
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services.profiles import _view as profile_view


@dataclass(frozen=True)
class CheckedRecipe:
    """Valid only at this read boundary; callers must recheck on subsequent operations."""

    recipe: StructuredRecipe = field(repr=False)
    content: str = field(repr=False)
    profile_revision: str = field(repr=False)
    policy_version: str = POLICY_VERSION


async def check_current_recipe(
    repository: CosmosRepository,
    user: AuthenticatedUser,
    recipe: StructuredRecipe,
    *,
    deadline: float | None = None,
) -> CheckedRecipe:
    record = await repository.read(user, ProductContainer.USERS, user.user_id, deadline=deadline)
    if record is None:
        raise RecipeSafetyError(RecipeSafetyFailure.PROFILE)
    preferences = require_onboarding(profile_view(record))
    checked = validate_final_recipe(
        recipe,
        SafetyProfile(
            allergies=preferences.allergies,
            dietary_restrictions=preferences.dietary_restrictions,
        ),
    )
    return CheckedRecipe(checked, render_recipe(checked), record.etag)


async def prepare_recipe_save(
    repository: CosmosRepository,
    user: AuthenticatedUser,
    *,
    turn_id: UUID,
    generated_recipe_id: UUID,
    deadline: float | None = None,
) -> CheckedRecipe:
    """Load an owned completed generation, not a client recipe or an old approval."""
    record = await repository.read(
        user, ProductContainer.CHAT_HISTORY, str(turn_id), deadline=deadline
    )
    if (
        record is None
        or record.body.get("recordKind") != "turn"
        or record.body.get("status") != "completed"
        or record.body.get("generatedRecipeId") != str(generated_recipe_id)
        or not isinstance(record.body.get("recipe"), dict)
    ):
        raise OwnedResourceNotFound
    try:
        recipe = StructuredRecipe.model_validate_json(json.dumps(record.body["recipe"]))
    except ValueError:
        raise RecipeSafetyError(RecipeSafetyFailure.STRUCTURE) from None
    return await check_current_recipe(repository, user, recipe, deadline=deadline)
