"""Partition-scoped reads of stored snapshots, without AI or profile changes."""

from pydantic import UUID4

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.conversations import HistoryPage
from sous_chef.domain.cosmos import HistoryQuery, PageRequest
from sous_chef.domain.recipe_lifecycle import RecipeHistory
from sous_chef.domain.recipe_queries import RecipeHistoryParameters, RecipeSummary
from sous_chef.services.pagination import HistoryPagination, HistoryScope
from sous_chef.services.recipe_lifecycle import HISTORY, _history
from sous_chef.settings import Settings


class RecipeQueryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list(
        self, user: AuthenticatedUser, parameters: RecipeHistoryParameters
    ) -> HistoryPage[RecipeSummary]:
        scope: HistoryScope = (
            "recipe-pending"
            if parameters.awaiting_feedback
            else "recipe-rated"
            if parameters.rated
            else "recipe"
        )
        pagination = HistoryPagination(self.settings)
        state, issued_at = pagination._page(user, parameters, scope, None)
        async with open_cosmos(self.settings) as repository:
            page = await repository.query(
                user,
                HistoryQuery(
                    kind="recipe",
                    created_before=state.created_before,
                    recipe_filter=(
                        "awaitingFeedback"
                        if parameters.awaiting_feedback
                        else "rated"
                        if parameters.rated
                        else "all"
                    ),
                ),
                PageRequest(limit=state.limit, continuation_token=state.continuation_token),
            )
            histories = tuple(_history(record) for record in page.items)
            return HistoryPage(
                items=tuple(
                    RecipeSummary(
                        history_id=history.history_id,
                        title=history.recipe.title,
                        saved_at=history.saved_at,
                        cooked_at=history.cooked_at,
                        state=history.state,
                        awaiting_feedback=history.awaiting_feedback,
                        rating=history.rating,
                    )
                    for history in histories
                ),
                next_cursor=pagination._cursor(state, page.continuation_token, issued_at),
            )

    async def detail(self, user: AuthenticatedUser, history_id: UUID4) -> tuple[RecipeHistory, str]:
        async with open_cosmos(self.settings) as repository:
            record = await repository.read(user, HISTORY, str(history_id))
            if record is None or record.body["recordKind"] != "recipe":
                raise OwnedResourceNotFound
            return _history(record), record.etag
