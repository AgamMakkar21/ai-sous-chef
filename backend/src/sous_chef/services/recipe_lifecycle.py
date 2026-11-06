"""Owned immutable acceptance, explicit intent and conditional cooking transitions."""

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4, ConfigDict, Field, JsonValue, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.chat import ChatExecutor, OperationView, SendMessage
from sous_chef.domain.context import LearnedPreferenceReader, RecipeRevisionContext
from sous_chef.domain.conversations import (
    ConversationModel,
    ConversationTurn,
    IdempotencyConflict,
)
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.profiles import require_onboarding
from sous_chef.domain.recipe_lifecycle import (
    RecipeBehavior,
    RecipeHistory,
    RecipeLifecycleConflict,
    SaveRecipe,
)
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services.chat import ChatService
from sous_chef.services.conversations import ConversationService
from sous_chef.services.profiles import _view as profile_view
from sous_chef.services.recipe_safety import check_current_recipe, prepare_recipe_save
from sous_chef.settings import Settings

HISTORY = ProductContainer.RECIPE_HISTORY
Action = Literal["save", "cooked", "rejected", "revisionRequested"]


class _StoredHistory(RecipeHistory):
    model_config = ConfigDict(extra="ignore")


class _StoredBehavior(RecipeBehavior):
    model_config = ConfigDict(extra="ignore")


class _Receipt(ConversationModel):
    model_config = ConfigDict(extra="ignore")

    record_kind: Literal["recipeOperation"]
    action: Action
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    result_id: UUID4


def _identity(user: AuthenticatedUser, scope: str, value: str) -> UUID:
    digest = hashlib.sha256(f"{user.user_id}\0{scope}\0{value}".encode()).digest()
    return UUID(bytes=digest[:16], version=4)


def _hash(action: Action, fields: dict[str, JsonValue]) -> str:
    return hashlib.sha256(
        json.dumps({"action": action, **fields}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _history(record: StoredRecord) -> RecipeHistory:
    try:
        stored = _StoredHistory.model_validate_json(json.dumps(record.body))
        if record.body["recordKind"] != "recipe" or record.body["id"] != str(stored.history_id):
            raise ValueError
        return RecipeHistory.model_validate(
            stored.model_dump(exclude={"state", "awaiting_feedback"})
        )
    except (ValueError, KeyError):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None


def _behavior(record: StoredRecord) -> RecipeBehavior:
    try:
        stored = _StoredBehavior.model_validate_json(json.dumps(record.body))
        if record.body["recordKind"] != "behavior" or record.body["id"] != str(stored.behavior_id):
            raise ValueError
        return RecipeBehavior.model_validate(stored.model_dump())
    except (ValueError, KeyError):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None


async def _source(
    repository: CosmosRepository, user: AuthenticatedUser, recipe_id: UUID4
) -> ConversationTurn:
    record = await repository.generated_recipe(user, str(recipe_id))
    try:
        conversation_id = UUID(str(record.body["conversationId"]))
    except (ValueError, KeyError):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
    conversation = await repository.read(user, ProductContainer.CHAT_HISTORY, str(conversation_id))
    if conversation is None:
        raise OwnedResourceNotFound
    ConversationService._conversation(conversation)
    turn = ConversationService._turn(record, conversation_id)
    if turn.messages[0].entity_id != user.user_id or turn.recipe is None:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
    return turn


async def _read_receipt(
    repository: CosmosRepository,
    user: AuthenticatedUser,
    request: NewRecord,
) -> _Receipt | None:
    record = await repository.read(user, HISTORY, str(request.record_id))
    if record is None:
        return None
    try:
        receipt = _Receipt.model_validate_json(json.dumps(record.body))
    except ValidationError:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
    if receipt.action != request.fields["action"] or not secrets.compare_digest(
        receipt.request_hash, str(request.fields["requestHash"])
    ):
        raise IdempotencyConflict
    if str(receipt.result_id) != request.fields["resultId"]:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
    return receipt


def _receipt(
    user: AuthenticatedUser, action: Action, key: str, digest: str, result_id: UUID4
) -> NewRecord:
    return NewRecord(
        kind=RecordKind.RECIPE_OPERATION,
        record_id=_identity(user, "recipe-" + action, key),
        fields={"action": action, "requestHash": digest, "resultId": str(result_id)},
    )


async def _result(
    repository: CosmosRepository, user: AuthenticatedUser, item_id: UUID4
) -> StoredRecord:
    record = await repository.read(user, HISTORY, str(item_id))
    if record is None:
        raise OwnedResourceNotFound
    return record


async def _record_intent(
    repository: CosmosRepository,
    user: AuthenticatedUser,
    receipt: NewRecord,
    effects: tuple[NewRecord, ...],
    *,
    cooked: bool = False,
) -> None:
    for attempt in range(3):
        if await _read_receipt(repository, user, receipt) is not None:
            return
        missing = []
        for effect in effects:
            current = await repository.read(user, HISTORY, str(effect.record_id))
            if current is None:
                missing.append(effect)
            elif current.body["recordKind"] != effect.kind:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
            elif (
                effect.kind == RecordKind.RECIPE and cooked and _history(current).cooked_at is None
            ):
                raise RecipeLifecycleConflict
        for index, effect in enumerate(missing):
            if effect.kind == RecordKind.RECIPE:
                checked = await check_current_recipe(
                    repository,
                    user,
                    StructuredRecipe.model_validate_json(json.dumps(effect.fields["recipe"])),
                )
                missing[index] = effect.model_copy(
                    update={
                        "fields": {
                            **effect.fields,
                            "safetyProfileRevision": checked.profile_revision,
                            "safetyPolicyVersion": checked.policy_version,
                        }
                    }
                )
        try:
            await repository.commit_recipe(user, creates=(*missing, receipt))
            return
        except CosmosError as error:
            if error.code != CosmosFailure.EXISTS and not error.outcome_unknown:
                raise
            if await _read_receipt(repository, user, receipt) is not None:
                return
            # Only a definitive create collision can be retried, after rereading the effect.
            if error.outcome_unknown or attempt == 2:
                raise


class RecipeLifecycleService:
    def __init__(
        self,
        settings: Settings,
        executor: ChatExecutor | None = None,
        learned_reader: LearnedPreferenceReader | None = None,
    ) -> None:
        self.settings = settings
        self.chat = ChatService(settings, executor, learned_reader)

    async def save(
        self, user: AuthenticatedUser, request: SaveRecipe, key: str
    ) -> tuple[RecipeHistory, str]:
        history_id = _identity(user, "recipe-snapshot", str(request.generated_recipe_id))
        receipt = _receipt(
            user,
            "save",
            key,
            _hash("save", request.model_dump(mode="json", by_alias=True)),
            history_id,
        )
        async with open_cosmos(self.settings) as repository:
            source = await _source(repository, user, request.generated_recipe_id)
            if await _read_receipt(repository, user, receipt) is None:
                checked = await prepare_recipe_save(
                    repository,
                    user,
                    turn_id=source.turn_id,
                    generated_recipe_id=request.generated_recipe_id,
                )
                now = datetime.now(UTC)
                snapshot = RecipeHistory(
                    history_id=history_id,
                    generated_recipe_id=request.generated_recipe_id,
                    occasion_id=source.occasion_id or request.generated_recipe_id,
                    supersedes_recipe_id=source.supersedes_recipe_id,
                    source_conversation_id=source.conversation_id,
                    source_turn_id=source.turn_id,
                    recipe=checked.recipe,
                    saved_at=now,
                    cooked_at=now if request.cooked else None,
                    processing_status="notEligible",
                )
                event = RecipeBehavior(
                    behavior_id=_identity(user, "recipe-saved", str(history_id)),
                    kind="saved",
                    source_id=history_id,
                    generated_recipe_id=request.generated_recipe_id,
                    occasion_id=snapshot.occasion_id,
                    source_conversation_id=source.conversation_id,
                    source_turn_id=source.turn_id,
                    history_id=history_id,
                    observed_at=now,
                )
                await _record_intent(
                    repository,
                    user,
                    receipt,
                    (
                        NewRecord(
                            kind=RecordKind.RECIPE,
                            record_id=history_id,
                            fields={
                                **snapshot.model_dump(
                                    mode="json",
                                    by_alias=True,
                                    exclude={"state", "awaiting_feedback"},
                                ),
                                "safetyProfileRevision": checked.profile_revision,
                                "safetyPolicyVersion": checked.policy_version,
                            },
                        ),
                        NewRecord(
                            kind=RecordKind.BEHAVIOR,
                            record_id=event.behavior_id,
                            fields=event.model_dump(mode="json", by_alias=True),
                        ),
                    ),
                    cooked=request.cooked,
                )
            result = await _result(repository, user, history_id)
            return _history(result), result.etag

    async def cooked(
        self, user: AuthenticatedUser, history_id: UUID4, key: str, etag: str
    ) -> tuple[RecipeHistory, str]:
        receipt = _receipt(
            user, "cooked", key, _hash("cooked", {"historyId": str(history_id)}), history_id
        )
        async with open_cosmos(self.settings) as repository:
            current = await _result(repository, user, history_id)
            if current.body["recordKind"] != "recipe":
                raise OwnedResourceNotFound
            view = _history(current)
            if await _read_receipt(repository, user, receipt) is None:
                if current.etag != etag:
                    raise CosmosError(CosmosFailure.CONFLICT, uuid4())
                patches: tuple[tuple[StoredRecord, RecordPatch], ...] = ()
                if view.cooked_at is None:
                    await check_current_recipe(repository, user, view.recipe)
                    patches = (
                        (
                            current,
                            RecordPatch(
                                etag=etag,
                                fields={
                                    "cookedAt": datetime.now(UTC)
                                    .isoformat(timespec="microseconds")
                                    .replace("+00:00", "Z")
                                },
                            ),
                        ),
                    )
                try:
                    await repository.commit_recipe(user, creates=(receipt,), patches=patches)
                except CosmosError as error:
                    if (
                        error.code not in {CosmosFailure.EXISTS, CosmosFailure.CONFLICT}
                        and not error.outcome_unknown
                    ) or await _read_receipt(repository, user, receipt) is None:
                        raise
            result = await _result(repository, user, history_id)
            return _history(result), result.etag

    async def _signal(
        self,
        user: AuthenticatedUser,
        recipe_id: UUID4,
        kind: Literal["rejected", "revisionRequested"],
        reason: str | None,
        key: str,
    ) -> tuple[ConversationTurn, RecipeBehavior]:
        digest = _hash(kind, {"generatedRecipeId": str(recipe_id), "reason": reason})
        behavior_id = _identity(user, "recipe-behavior", digest)
        receipt = _receipt(user, kind, key, digest, behavior_id)
        async with open_cosmos(self.settings) as repository:
            source = await _source(repository, user, recipe_id)
            if kind == "revisionRequested":
                profile = await repository.read(user, ProductContainer.USERS, user.user_id)
                if profile is None:
                    raise OwnedResourceNotFound
                require_onboarding(profile_view(profile))
            event = RecipeBehavior(
                behavior_id=behavior_id,
                kind=kind,
                source_id=source.turn_id,
                generated_recipe_id=recipe_id,
                occasion_id=source.occasion_id or recipe_id,
                source_conversation_id=source.conversation_id,
                source_turn_id=source.turn_id,
                reason=reason,
                observed_at=datetime.now(UTC),
            )
            await _record_intent(
                repository,
                user,
                receipt,
                (
                    NewRecord(
                        kind=RecordKind.BEHAVIOR,
                        record_id=behavior_id,
                        fields=event.model_dump(mode="json", by_alias=True),
                    ),
                ),
            )
            return source, _behavior(await _result(repository, user, behavior_id))

    async def reject(
        self, user: AuthenticatedUser, recipe_id: UUID4, reason: str | None, key: str
    ) -> RecipeBehavior:
        _, event = await self._signal(user, recipe_id, "rejected", reason, key)
        return event

    async def revise(
        self, user: AuthenticatedUser, recipe_id: UUID4, reason: str, key: str
    ) -> OperationView:
        source, event = await self._signal(user, recipe_id, "revisionRequested", reason, key)
        assert source.recipe is not None
        return await self.chat.send(
            user,
            source.conversation_id,
            SendMessage(content=reason),
            "recipe-revision:" + str(event.behavior_id),
            revision=RecipeRevisionContext(
                generated_recipe_id=recipe_id,
                occasion_id=event.occasion_id,
                recipe=source.recipe,
            ),
        )
