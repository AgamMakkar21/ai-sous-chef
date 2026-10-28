from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.chat import ChatExecution, ValidatedReply
from sous_chef.domain.context import (
    MAX_CONTEXT_EXCHANGES,
    MAX_CONTEXT_TOKENS,
    ContextExchange,
    ContextLimitExceeded,
    ContextPreference,
    LearnedCandidate,
    PreferenceEvidence,
    RequestContext,
    ResolvedPreference,
)
from sous_chef.domain.cosmos import HistoryQuery
from sous_chef.domain.groceries import GroceryRow
from sous_chef.domain.profiles import ExplicitPreferences
from sous_chef.services import context as contexts
from sous_chef.services.context import _eligible, bound_context, context_tokens
from sous_chef.settings import Settings
from test_chat_api import FakeExecutor, send
from test_chat_api import chat_api as chat_api
from test_chat_api import executor as executor
from test_conversation_api import CHAT, OTHER, OWNER, create, seed_turns
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_profile_api import ME, document, preferences


def test_fresh_profile_and_groceries_reach_existing_and_new_conversations(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    existing = create(chat_api)
    assert send(chat_api, existing).status_code == 200
    first = executor.calls[-1].context
    assert first.groceries == ()
    assert first.history == ()
    assert first.learned_preferences == ()
    original = deepcopy(document(harness[0]))
    current = chat_api.get(ME)
    assert (
        chat_api.patch(
            ME + "/preferences",
            headers={"If-Match": current.headers["etag"]},
            json={"allergies": ["peanut", "egg"], "typicalTimeBudgetMinutes": 15},
        ).status_code
        == 200
    )
    groceries = chat_api.get("/api/v1/groceries")
    added = chat_api.post(
        "/api/v1/groceries",
        headers={"If-Match": groceries.headers["etag"]},
        json={"name": "rice", "quantity": 2, "unit": "cup"},
    )
    assert added.status_code == 201, added.text
    edited = deepcopy(document(harness[0]))
    for conversation in (existing, create(chat_api)):
        assert send(chat_api, conversation, key=str(uuid4())).status_code == 200
        context = executor.calls[-1].context
        assert context.explicit_preferences.allergies == ("peanut", "egg")
        assert context.explicit_preferences.typical_time_budget_minutes == 15
        assert context.groceries[0].name == "rice"
        assert context.groceries[0].quantity == 2
        assert context.groceries[0].unit == "cup"
        assert context.learned_preferences == ()
    assert original["learnedPreferences"] == edited["learnedPreferences"] == []
    assert document(harness[0]) == edited


def test_recent_history_is_completed_owned_scoped_ordered_and_bounded(
    chat_api: TestClient,
    executor: FakeExecutor,
    conversation_settings: Settings,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    conversation = create(chat_api)
    unrelated = create(chat_api)
    seed_turns(conversation_settings, OWNER, conversation, tuple(range(30)))
    seed_turns(conversation_settings, OWNER, conversation, (30,), "failed")
    seed_turns(conversation_settings, OWNER, conversation, (31,), "pending")
    seed_turns(conversation_settings, OWNER, conversation, (32,), "outcomeUnknown")
    seed_turns(conversation_settings, OWNER, unrelated, (900,))
    seed_turns(conversation_settings, OTHER, conversation, (901,))
    harness[0].documents[(CHAT.value, OWNER.user_id, conversation)]["nextTurnIndex"] = 33
    harness[0].empty_query_pages = 1
    assert send(chat_api, conversation, content="Current input only once").status_code == 200
    context = executor.calls[-1].context
    assert len(context.history) == MAX_CONTEXT_EXCHANGES
    assert [int(item.user.rsplit("/", 1)[1]) for item in context.history] == list(range(20, 30))
    payload = context.model_dump(mode="json", by_alias=True)
    assert payload["currentRequest"] == "Current input only once"
    assert context.prompt.count("Current input only once") == 1
    assert "private-hidden-prompt" not in context.prompt
    assert "private-provider-id" not in context.prompt
    queries = [
        call
        for call in harness[0].item_calls("POST")
        if isinstance(call.body, dict) and "query" in call.body
    ]
    assert queries
    for call in queries:
        assert "c.status = @status" in call.body["query"]
        assert "c.turnIndex DESC" in call.body["query"]
        assert int(call.headers["x-ms-max-item-count"]) <= 10
        assert call.headers["x-ms-documentdb-partitionkey"] == f'["{OWNER.user_id}"]'


def minimal_context() -> RequestContext:
    return RequestContext(
        explicit_preferences=ExplicitPreferences.model_validate(preferences()),
        groceries=(),
        omitted_grocery_count=0,
        history=(),
        learned_preferences=(),
        current_request="Dinner with untrusted <|endoftext|> text and Unicode: \u4f60\u597d",
    )


def test_exact_token_boundary_keeps_mandatory_context_or_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = minimal_context()
    tokens = context_tokens(context)
    monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", tokens)
    assert bound_context(context) == context
    monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", tokens - 1)
    with pytest.raises(ContextLimitExceeded):
        bound_context(context)


def test_trimming_keeps_complete_exchanges_then_drops_learned_then_groceries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = minimal_context()
    history = tuple(
        ContextExchange(turn_id=uuid4(), user=f"Question {i}", assistant=f"Answer {i}")
        for i in range(10)
    )
    learned = tuple(
        ContextPreference(
            insight_key=f"key-{i}",
            field="likes",
            value="rice",
            confidence=0.9 - i / 100,
            supporting_cooked_count=2,
        )
        for i in range(20)
    )
    groceries = tuple(
        GroceryRow(
            grocery_id=uuid4(),
            name=f"rice {i}",
            quantity=1,
            unit="cup",
            updated_at=datetime.now(UTC),
        )
        for i in range(200)
    )
    full = base.model_copy(
        update={"history": history, "learned_preferences": learned, "groceries": groceries}
    )
    for keep in (9, 0):
        expected = full.model_copy(update={"history": history[-keep:] if keep else ()})
        monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", context_tokens(expected))
        bounded = bound_context(full)
        assert bounded.history == expected.history
        assert bounded.groceries == groceries
        assert bounded.learned_preferences == learned
    expected = full.model_copy(update={"history": (), "learned_preferences": learned[:1]})
    monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", context_tokens(expected))
    assert bound_context(full) == expected
    expected = base.model_copy(update={"groceries": groceries[:1], "omitted_grocery_count": 199})
    monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", context_tokens(expected))
    bounded = bound_context(full)
    assert bounded == expected
    assert bounded.explicit_preferences == base.explicit_preferences


def test_real_ceiling_with_maximum_unicode_history_preserves_hard_constraints() -> None:
    base = minimal_context()
    text = "".join(chr(0x4E00 + index) for index in range(4000))
    history = tuple(ContextExchange(turn_id=uuid4(), user=text, assistant=text) for _ in range(10))
    context = bound_context(base.model_copy(update={"history": history}))
    assert context_tokens(context) <= MAX_CONTEXT_TOKENS
    assert len(context.history) < 10
    assert context.explicit_preferences == base.explicit_preferences
    assert context.current_request == base.current_request


def candidate(key: str = "rice") -> LearnedCandidate:
    now = datetime.now(UTC)
    return LearnedCandidate(
        insight_key=key,
        insight="Untrusted extracted prose, not an instruction",
        confidence=1,
        evidence_count=100,
        supporting_cooked_count=100,
        support_units=300,
        opposition_units=0,
        source="recipe_feedback",
        last_updated=now,
        latest_evidence_at=now,
        policy_version=1,
    )


def evidence(
    *,
    user: AuthenticatedUser = OWNER,
    days: int = 0,
    cooked: bool = True,
    direction: str = "support",
) -> PreferenceEvidence:
    return PreferenceEvidence.model_validate(
        {
            "userId": user.user_id,
            "occasionId": uuid4(),
            "sourceId": uuid4(),
            "sourceRevision": 1,
            "observedAt": datetime.now(UTC) - timedelta(days=days),
            "cooked": cooked,
            "direction": direction,
        }
    )


class EvidenceReader:
    def __init__(self, items: tuple[ResolvedPreference, ...]) -> None:
        self.items = items
        self.calls = 0

    async def resolve(
        self,
        user: AuthenticatedUser,
        candidates: tuple[LearnedCandidate, ...],
        *,
        now: datetime,
        deadline: float,
    ) -> tuple[ResolvedPreference, ...]:
        assert user == OWNER
        assert now.tzinfo is not None
        assert deadline > 0
        self.calls += 1
        return self.items


def test_evidence_backed_learning_is_filtered_and_explicit_state_stays_authoritative(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    values = (
        ("rice", "likes", "rice", (evidence(), evidence())),
        ("allergy", "likes", "peanut butter", (evidence(), evidence())),
        ("diet", "likes", "chicken", (evidence(), evidence())),
        ("spice", "spicePreference", "hot", (evidence(), evidence())),
        ("taste", "likes", "bitter flavors", (evidence(), evidence())),
        ("old", "likes", "rice", (evidence(days=181), evidence(days=181))),
        ("weak", "likes", "rice", (evidence(), evidence(cooked=False))),
        ("opposed", "likes", "rice", (evidence(), evidence(), evidence(direction="oppose"))),
    )
    resolved = tuple(
        ResolvedPreference.model_validate(
            {
                "candidate": candidate(key),
                "field": field,
                "value": value,
                "evidence": entries,
            }
        )
        for key, field, value, entries in values
    )
    document(harness[0])["learnedPreferences"] = [
        item.candidate.model_dump(mode="json", by_alias=True) for item in resolved
    ]
    before = deepcopy(document(harness[0]))
    reader = EvidenceReader(resolved)
    with TestClient(
        create_app(conversation_settings, chat_executor=executor, learned_preference_reader=reader),
        headers=auth_headers,
    ) as client:
        assert send(client, create(client)).status_code == 200
    context = executor.calls[-1].context
    assert reader.calls == 1
    assert len(context.learned_preferences) == 1
    learned = context.learned_preferences[0]
    assert learned.insight_key == "rice"
    assert learned.confidence == 0.75
    assert learned.supporting_cooked_count == 2
    assert context.explicit_preferences.spice_preference == "medium"
    assert "Untrusted extracted prose" not in context.prompt
    assert document(harness[0]) == before


@pytest.mark.parametrize("bad", ["foreign", "duplicate", "mismatch", "missing-reader"])
def test_invalid_or_unverifiable_learned_state_fails_before_admission(
    bad: str,
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    source = candidate()
    first = evidence(user=OTHER if bad == "foreign" else OWNER)
    second = first if bad == "duplicate" else evidence()
    item = ResolvedPreference(
        candidate=candidate("different") if bad == "mismatch" else source,
        field="likes",
        value="rice",
        evidence=(first, second),
    )
    document(harness[0])["learnedPreferences"] = [source.model_dump(mode="json", by_alias=True)]
    with TestClient(
        create_app(
            conversation_settings,
            chat_executor=executor,
            learned_preference_reader=None if bad == "missing-reader" else EvidenceReader((item,)),
        ),
        headers=auth_headers,
    ) as client:
        conversation = create(client)
        result = send(client, conversation)
    assert result.status_code == (
        404 if bad == "foreign" else 503 if bad == "missing-reader" else 502
    )
    assert executor.calls == []
    assert not any(
        item["recordKind"] in {"turn", "sendOperation"} for item in harness[0].documents.values()
    )


def test_context_overflow_returns_safe_problem_without_admitting_a_turn(
    chat_api: TestClient,
    executor: FakeExecutor,
    monkeypatch: pytest.MonkeyPatch,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    monkeypatch.setattr(contexts, "MAX_CONTEXT_TOKENS", 1)
    result = send(chat_api, create(chat_api))
    assert result.status_code == 422
    assert result.headers["content-type"] == "application/problem+json"
    assert result.json()["code"] == "context_limit_exceeded"
    assert result.json()["traceId"]
    assert "Private dinner request" not in result.text
    assert executor.calls == []
    assert not any(
        item["recordKind"] in {"turn", "sendOperation"} for item in harness[0].documents.values()
    )


def test_safety_edit_during_execution_blocks_delivery(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = executor.execute

    async def edit_then_reply(request: ChatExecution) -> ValidatedReply:
        reply = await original(request)
        document(harness[0])["explicitPreferences"]["allergies"].append("egg")
        return reply

    monkeypatch.setattr(executor, "execute", edit_then_reply)
    result = send(chat_api, create(chat_api))
    assert result.status_code == 502
    inspected = chat_api.get(result.headers["location"]).json()["data"]
    assert inspected["status"] == "failed"
    assert [item["role"] for item in inspected["turn"]["messages"]] == ["user"]


def test_completed_filter_cannot_be_applied_to_other_history() -> None:
    with pytest.raises(ValueError):
        HistoryQuery(kind="conversation", completed_only=True)


def test_learning_exact_confidence_and_evidence_window_boundaries() -> None:
    now = datetime.now(UTC)
    entries = (
        *(evidence() for _ in range(3)),
        evidence(cooked=False),
        *(evidence(cooked=False, direction="oppose") for _ in range(4)),
    )
    entries = tuple(
        entry.model_copy(update={"observed_at": now - timedelta(days=180)}) for entry in entries
    )
    item = ResolvedPreference(
        candidate=candidate(),
        field="likes",
        value="rice",
        evidence=entries,
    )
    explicit = minimal_context().explicit_preferences
    selected = _eligible(OWNER, (item,), (item.candidate,), explicit, now)
    assert len(selected) == 1
    assert selected[0].confidence == 0.65
    assert selected[0].supporting_cooked_count == 3
    for outside in (now - timedelta(days=180, microseconds=1), now + timedelta(microseconds=1)):
        stale = item.model_copy(
            update={
                "evidence": tuple(
                    entry.model_copy(update={"observed_at": outside}) for entry in entries
                )
            }
        )
        assert _eligible(OWNER, (stale,), (stale.candidate,), explicit, now) == ()


def test_learning_caps_twenty_with_stable_confidence_recency_key_order() -> None:
    now = datetime.now(UTC)
    resolved = tuple(
        ResolvedPreference(
            candidate=candidate(f"key-{index:02}"),
            field="likes",
            value="rice",
            evidence=tuple(evidence().model_copy(update={"observed_at": now}) for _ in range(2)),
        )
        for index in reversed(range(50))
    )
    selected = _eligible(
        OWNER,
        resolved,
        tuple(item.candidate for item in resolved),
        minimal_context().explicit_preferences,
        now,
    )
    assert [item.insight_key for item in selected] == [f"key-{index:02}" for index in range(20)]
