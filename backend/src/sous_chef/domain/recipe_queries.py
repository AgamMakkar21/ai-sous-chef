"""Concise saved-recipe lists; details retain the original lifecycle snapshot."""

from typing import Literal

from pydantic import UUID4, AwareDatetime, model_validator

from sous_chef.domain.conversations import HistoryParameters
from sous_chef.domain.recipe_lifecycle import LifecycleModel


class RecipeHistoryParameters(HistoryParameters):
    awaiting_feedback: bool = False
    rated: bool = False

    @model_validator(mode="after")
    def disjoint_filters(self) -> "RecipeHistoryParameters":
        if self.awaiting_feedback and self.rated:
            raise ValueError("Awaiting-feedback and rated views are mutually exclusive.")
        return self


class RecipeSummary(LifecycleModel):
    history_id: UUID4
    title: str
    saved_at: AwareDatetime
    cooked_at: AwareDatetime | None
    state: Literal["planned", "cooked"]
    awaiting_feedback: bool
    rating: Literal["liked", "fine", "disliked"] | None
