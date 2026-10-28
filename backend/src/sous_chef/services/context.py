"""Fresh, owned, bounded model input; never writes user preferences or chat."""

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import uuid4

import tiktoken
from pydantic import UUID4, ConfigDict, Field, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.context import (
    CONTEXT_FRAMING_TOKENS,
    MAX_CONTEXT_EXCHANGES,
    MAX_CONTEXT_LEARNED,
    MAX_CONTEXT_TOKENS,
    ContextExchange,
    ContextLimitExceeded,
    ContextPreference,
    LearnedCandidate,
    LearnedPreferenceReader,
    RequestContext,
    ResolvedPreference,
)
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    HistoryQuery,
    PageRequest,
    StoredRecord,
)
from sous_chef.domain.food_safety import (
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.profiles import ExplicitPreferences, ProfileModel, require_onboarding
from sous_chef.services.conversations import ConversationService
from sous_chef.services.groceries import _rows as grocery_rows
from sous_chef.services.profiles import _view as profile_view
from sous_chef.settings import ConfigurationError


class _LearnedState(ProfileModel):
    model_config = ConfigDict(extra="ignore")

    learned_preferences: tuple[LearnedCandidate, ...] = Field(max_length=50)


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("o200k_base")


def context_tokens(context: RequestContext) -> int:
    # Special-token-looking user text is ordinary data, not tokenizer control input.
    return len(_encoding().encode(context.prompt, disallowed_special=())) + CONTEXT_FRAMING_TOKENS


def bound_context(context: RequestContext) -> RequestContext:
    while context_tokens(context) > MAX_CONTEXT_TOKENS:
        if context.history:
            context = context.model_copy(update={"history": context.history[1:]})
        elif context.learned_preferences:
            context = context.model_copy(
                update={"learned_preferences": context.learned_preferences[:-1]}
            )
        elif context.groceries:
            context = context.model_copy(
                update={
                    "groceries": context.groceries[:-1],
                    "omitted_grocery_count": context.omitted_grocery_count + 1,
                }
            )
        else:
            raise ContextLimitExceeded
    return context


def _conflicts(preference: ResolvedPreference, explicit: ExplicitPreferences) -> bool:
    value = preference.value.casefold()
    if preference.field == "spicePreference":
        return value != explicit.spice_preference
    if preference.field == "likes":
        safety = validate_ingredients(
            SafetyProfile(
                allergies=explicit.allergies,
                dietary_restrictions=explicit.dietary_restrictions,
                dislikes=explicit.dislikes,
            ),
            [IngredientLine(line_id="learned", name=preference.value)],
        )
        return safety.verdict != Verdict.PASS or bool(safety.dislike_matches)
    if preference.field == "dislikes":
        return value in {item.casefold() for item in explicit.likes}
    selections = (
        explicit.preferred_cuisines
        if preference.field == "preferredCuisines"
        else explicit.nutrition_goals
    )
    return bool(selections) and value not in {item.casefold() for item in selections}


def _eligible(
    user: AuthenticatedUser,
    resolved: tuple[ResolvedPreference, ...],
    candidates: tuple[LearnedCandidate, ...],
    explicit: ExplicitPreferences,
    now: datetime,
) -> tuple[ContextPreference, ...]:
    supplied = {item.insight_key: item for item in candidates}
    if len(supplied) != len(candidates) or len(resolved) != len(candidates):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
    seen: set[str] = set()
    ranked: list[tuple[ContextPreference, datetime]] = []
    for item in resolved:
        candidate = item.candidate
        if candidate.insight_key in seen or supplied.get(candidate.insight_key) != candidate:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        seen.add(candidate.insight_key)
        occasions = set()
        current = []
        for evidence in item.evidence:
            user.require_owned({"userId": evidence.user_id})
            if evidence.occasion_id in occasions:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
            occasions.add(evidence.occasion_id)
            if now - timedelta(days=180) <= evidence.observed_at <= now:
                current.append(evidence)
        support = sum(
            3 if evidence.cooked else 1 for evidence in current if evidence.direction == "support"
        )
        opposition = sum(
            3 if evidence.cooked else 1 for evidence in current if evidence.direction == "oppose"
        )
        cooked = sum(evidence.cooked and evidence.direction == "support" for evidence in current)
        confidence = (3 + support) / (6 + support + opposition)
        if cooked < 2 or confidence < 0.65 or _conflicts(item, explicit):
            continue
        ranked.append(
            (
                ContextPreference(
                    insight_key=candidate.insight_key,
                    field=item.field,
                    value=item.value,
                    confidence=confidence,
                    supporting_cooked_count=cooked,
                ),
                max(evidence.observed_at for evidence in current),
            )
        )
    ranked.sort(key=lambda pair: (-pair[0].confidence, -pair[1].timestamp(), pair[0].insight_key))
    return tuple(item for item, _ in ranked[:MAX_CONTEXT_LEARNED])


class ContextService:
    def __init__(self, learned_reader: LearnedPreferenceReader | None = None) -> None:
        self.learned_reader = learned_reader

    async def build(
        self,
        repository: CosmosRepository,
        user: AuthenticatedUser,
        conversation_id: UUID4,
        profile: StoredRecord,
        content: str,
        *,
        deadline: float,
    ) -> RequestContext:
        user.require_owned(profile.body)
        explicit = require_onboarding(profile_view(profile))
        groceries = grocery_rows(profile)
        try:
            candidates = _LearnedState.model_validate_json(
                json.dumps(profile.body)
            ).learned_preferences
        except ValidationError:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
        learned: tuple[ContextPreference, ...] = ()
        now = datetime.now(UTC)
        if candidates:
            if self.learned_reader is None:
                raise ConfigurationError("Learned context requires a current-evidence reader.")
            resolved = await self.learned_reader.resolve(
                user, candidates, now=now, deadline=deadline
            )
            learned = _eligible(user, resolved, candidates, explicit, now)

        exchanges: list[ContextExchange] = []
        token = None
        tokens_seen: set[str] = set()
        query = HistoryQuery(
            kind="turn",
            conversation_id=conversation_id,
            completed_only=True,
            created_before=now,
        )
        while len(exchanges) < MAX_CONTEXT_EXCHANGES:
            page = await repository.query(
                user,
                query,
                PageRequest(limit=MAX_CONTEXT_EXCHANGES - len(exchanges), continuation_token=token),
                deadline=deadline,
            )
            for record in page.items:
                user.require_owned(record.body)
                turn = ConversationService._turn(record, conversation_id)
                if turn.status != "completed" or turn.messages[0].entity_id != user.user_id:
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
                exchanges.append(
                    ContextExchange(
                        turn_id=turn.turn_id,
                        user=turn.messages[0].content,
                        assistant=turn.messages[1].content,
                    )
                )
            token = page.continuation_token
            if token is None:
                break
            if token in tokens_seen:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
            tokens_seen.add(token)
        ranked_groceries = tuple(
            sorted(
                groceries,
                key=lambda row: (
                    row.name.casefold() not in content.casefold(),
                    str(row.grocery_id),
                ),
            )
        )
        context = RequestContext(
            explicit_preferences=explicit,
            groceries=ranked_groceries,
            omitted_grocery_count=0,
            history=tuple(reversed(exchanges)),
            learned_preferences=learned,
            current_request=content,
        )
        return bound_context(context)
