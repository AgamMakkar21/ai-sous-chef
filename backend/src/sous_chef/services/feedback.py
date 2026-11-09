"""Persist feedback and its outbox event without invoking learning."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    RecordKind,
    RecordPatch,
)
from sous_chef.domain.feedback import (
    FeedbackRequiresCooking,
    FeedbackRevision,
    SubmitFeedback,
)
from sous_chef.domain.recipe_lifecycle import RecipeHistory
from sous_chef.services.recipe_lifecycle import (
    _hash,
    _history,
    _identity,
    _read_receipt,
    _receipt,
    _result,
)
from sous_chef.settings import Settings
from sous_chef.telemetry import new_trace_id, trace_id


class FeedbackService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def submit(
        self,
        user: AuthenticatedUser,
        history_id: UUID,
        request: SubmitFeedback,
        key: str,
        etag: str,
    ) -> tuple[RecipeHistory, str]:
        receipt = _receipt(
            user,
            "feedback",
            key,
            _hash(
                "feedback",
                {"historyId": str(history_id), **request.model_dump(mode="json", by_alias=True)},
            ),
            history_id,
        )
        async with open_cosmos(self.settings) as repository:
            current = await _result(repository, user, history_id)
            if current.body["recordKind"] != "recipe":
                raise OwnedResourceNotFound
            view = _history(current)
            if await _read_receipt(repository, user, receipt) is None:
                if current.etag != etag:
                    raise CosmosError(CosmosFailure.CONFLICT, uuid4())
                if view.cooked_at is None:
                    raise FeedbackRequiresCooking
                changed = (view.rating, view.note) != (request.rating, request.note)
                revision = view.feedback_revision + int(changed)
                creates: tuple[NewRecord, ...] = (receipt,)
                if changed:
                    event = FeedbackRevision(
                        feedback_id=_identity(user, "recipe-feedback", f"{history_id}:{revision}"),
                        source_id=history_id,
                        history_id=history_id,
                        occasion_id=view.occasion_id,
                        generated_recipe_id=view.generated_recipe_id,
                        source_conversation_id=view.source_conversation_id,
                        source_turn_id=view.source_turn_id,
                        feedback_revision=revision,
                        supersedes_feedback_id=(
                            _identity(user, "recipe-feedback", f"{history_id}:{revision - 1}")
                            if revision > 1
                            else None
                        ),
                        rating=request.rating,
                        note=request.note,
                        trace_id=trace_id.get() or new_trace_id(),
                        observed_at=datetime.now(UTC),
                    )
                    creates += (
                        NewRecord(
                            kind=RecordKind.FEEDBACK_REVISION,
                            record_id=event.feedback_id,
                            fields=event.model_dump(mode="json", by_alias=True),
                        ),
                    )
                patch = RecordPatch(
                    etag=etag,
                    fields={
                        "rating": request.rating,
                        "note": request.note,
                        "feedbackRevision": revision,
                        "processingStatus": "pending" if changed else view.processing_status,
                    },
                )
                try:
                    # Even a no-op checks the ETag atomically with its retry receipt.
                    await repository.commit_feedback(
                        user, creates=creates, patches=((current, patch),)
                    )
                except CosmosError as error:
                    if (
                        error.code not in {CosmosFailure.EXISTS, CosmosFailure.CONFLICT}
                        and not error.outcome_unknown
                    ):
                        raise
                    if await _read_receipt(repository, user, receipt) is None:
                        if error.code == CosmosFailure.EXISTS:
                            raise CosmosError(CosmosFailure.CONFLICT, error.operation_id) from None
                        raise
            result = await _result(repository, user, history_id)
            return _history(result), result.etag
