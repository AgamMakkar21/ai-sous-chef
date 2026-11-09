import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4

import pytest
from conftest import AUTH_ISSUER
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.settings import Settings
from test_chat_api import FakeExecutor, send
from test_conversation_api import BASE, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_grocery_api import GROCERIES, add
from test_profile_api import ME, onboard, preferences, signed_token
from test_recipe_queries import seed_history
from test_recipe_safety import full_recipe


@dataclass(frozen=True)
class EndpointCase:
    method: str
    path: str
    status: int = 200
    body: dict[str, Any] | None = None
    guarded_record: Literal["profile", "conversation", "turn", "recipe"] | None = "profile"

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}"


SEND_CASE = EndpointCase(
    "POST",
    BASE + "/{conversation_id}/turns",
    body={"content": "matrix dinner request"},
    guarded_record="conversation",
)
INSPECT_CASE = EndpointCase(
    "GET",
    BASE + "/{conversation_id}/operations/{operation_id}",
    guarded_record="conversation",
)
SAVE_CASE = EndpointCase(
    "POST",
    "/api/v1/recipes",
    201,
    {"generatedRecipeId": "{generated_recipe_id}"},
    "turn",
)
COOK_CASE = EndpointCase(
    "POST",
    "/api/v1/recipes/{history_id}/cooked",
    body={},
    guarded_record="recipe",
)
FEEDBACK_CASE = EndpointCase(
    "PUT",
    "/api/v1/recipes/{history_id}/feedback",
    body={"rating": "liked", "note": "matrix private cooking note"},
    guarded_record="recipe",
)
REJECT_CASE = EndpointCase(
    "POST",
    "/api/v1/generated-recipes/{generated_recipe_id}/rejections",
    201,
    {"reason": "matrix explicit rejection"},
    "turn",
)
REVISE_CASE = EndpointCase(
    "POST",
    "/api/v1/generated-recipes/{generated_recipe_id}/revisions",
    body={"reason": "matrix requested revision"},
    guarded_record="turn",
)
CASES = (
    EndpointCase("GET", ME),
    EndpointCase("PUT", ME + "/onboarding", body=preferences()),
    EndpointCase("PATCH", ME + "/preferences", body={"likes": ["matrix updated taste"]}),
    EndpointCase("GET", GROCERIES),
    EndpointCase("POST", GROCERIES, 201, {"name": "matrix added grocery"}),
    EndpointCase("PATCH", GROCERIES + "/{grocery_id}", body={"quantity": 2}),
    EndpointCase("DELETE", GROCERIES + "/{grocery_id}", 204),
    EndpointCase("POST", BASE, 201, {"title": "matrix new conversation"}, None),
    EndpointCase("GET", BASE, guarded_record="conversation"),
    EndpointCase("GET", BASE + "/{conversation_id}/turns", guarded_record="conversation"),
    SEND_CASE,
    INSPECT_CASE,
    SAVE_CASE,
    COOK_CASE,
    FEEDBACK_CASE,
    REJECT_CASE,
    REVISE_CASE,
    EndpointCase("GET", "/api/v1/recipes", guarded_record="recipe"),
    EndpointCase("GET", "/api/v1/recipes/{history_id}", guarded_record="recipe"),
)
FOREIGN_ID_CASES = tuple(case for case in CASES if "{" in case.path or case == SAVE_CASE)
NON_USER_DATA = {
    ("POST", "/api/v1/nutrition/lookup"): "Read-only USDA data; no stored user-state access.",
    ("POST", "/api/v1/nutrition/search"): "Read-only USDA candidates; no stored user-state access.",
}


@dataclass(frozen=True)
class UserData:
    user: AuthenticatedUser
    headers: dict[str, str]
    marker: str
    grocery_id: str
    conversation_id: str
    second_conversation_id: str
    operation_id: str
    turn_ids: tuple[str, ...]
    generated_recipe_id: str
    history_id: str

    def path(self, case: EndpointCase) -> str:
        return case.path.format(
            grocery_id=self.grocery_id,
            conversation_id=self.conversation_id,
            operation_id=self.operation_id,
            generated_recipe_id=self.generated_recipe_id,
            history_id=self.history_id,
        )

    def hints(self) -> dict[str, str]:
        return {
            "X-User-ID": self.user.user_id,
            "X-MS-CLIENT-PRINCIPAL-ID": self.user.user_id,
            "X-MS-CLIENT-PRINCIPAL": self.marker,
        }

    def assert_private(self, response: Response) -> None:
        public = response.text + json.dumps(dict(response.headers))
        for value in (
            self.user.user_id,
            self.marker,
            self.grocery_id,
            self.conversation_id,
            self.second_conversation_id,
            self.operation_id,
            *self.turn_ids,
            self.generated_recipe_id,
            self.history_id,
        ):
            assert value not in public


@dataclass
class TwoUserApi:
    client: TestClient
    transport: Transport
    executor: FakeExecutor
    users: tuple[UserData, UserData]

    def profile(self, actor: UserData) -> dict[str, Any]:
        return self.transport.documents[("Users", actor.user.user_id, actor.user.user_id)]

    def request(
        self,
        case: EndpointCase,
        actor: UserData,
        target: UserData | None = None,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> Response:
        payload = deepcopy(case.body if body is None else body)
        if payload is not None and payload.get("generatedRecipeId") == "{generated_recipe_id}":
            payload["generatedRecipeId"] = (target or actor).generated_recipe_id
        revision = (
            self.transport.documents[("RecipeHistory", actor.user.user_id, actor.history_id)][
                "_etag"
            ]
            if case in (COOK_CASE, FEEDBACK_CASE)
            else self.profile(actor)["_etag"]
        )
        return self.client.request(
            case.method,
            (target or actor).path(case),
            headers={
                **actor.headers,
                "If-Match": revision,
                "Idempotency-Key": "matrix-request",
                **(headers or {}),
            },
            json=payload,
            params=params,
        )

    def assert_partition(self, actor: UserData, since: int) -> None:
        calls = self.transport.item_calls()[since:]
        assert calls
        for call in calls:
            assert json.loads(call.headers["x-ms-documentdb-partitionkey"]) == [actor.user.user_id]
            assert call.headers.get("x-ms-documentdb-query-enablecrosspartition") != "true"


@pytest.fixture
def two_user_api(
    conversation_settings: Settings,
    harness: tuple[Transport, SyntheticCredential],
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[TwoUserApi]:
    transport, _ = harness
    executor = FakeExecutor(transport)
    with TestClient(
        create_app(conversation_settings, chat_executor=executor), base_url="https://testserver"
    ) as client:
        users = []
        for subject in ("matrix-alice", "matrix-bob"):
            marker = subject + "-private"
            headers = {"Authorization": "Bearer " + signed_token(signing_key, subject)}
            client.headers.update(headers)
            settings = {**preferences(), "likes": [marker + " taste"]}
            onboard(client, settings)
            grocery = add(client, name=marker + " grocery", quantity=1)
            conversation = create(client, marker + " conversation")
            second = create(client, marker + " second conversation")
            turns = []
            generated_recipe_id = ""
            for index in range(2):
                executor.recipe = full_recipe() if index == 0 else None
                executor.recipe_id = uuid4()
                result = send(
                    client,
                    conversation,
                    key=f"seed-send-{index}",
                    content=f"{marker} question {index}",
                )
                assert result.status_code == 200, result.text
                turns.append(str(result.json()["data"]["turnId"]))
                if index == 0:
                    generated_recipe_id = result.json()["data"]["generatedRecipeId"]
            saved = client.post(
                "/api/v1/recipes",
                json={"generatedRecipeId": generated_recipe_id, "cooked": True},
                headers={"Idempotency-Key": "seed-acceptance"},
            )
            assert saved.status_code == 201, saved.text
            users.append(
                UserData(
                    AuthenticatedUser.from_verified_identity(AUTH_ISSUER, subject),
                    headers,
                    marker,
                    str(grocery.json()["data"]["groceryId"]),
                    conversation,
                    second,
                    result.headers["location"].rsplit("/", 1)[1],
                    tuple(turns),
                    generated_recipe_id,
                    saved.json()["data"]["historyId"],
                )
            )
        del client.headers["Authorization"]
        yield TwoUserApi(client, transport, executor, (users[0], users[1]))


def assert_problem(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, "Authorization regression: " + response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    problem: dict[str, Any] = response.json()
    assert problem["status"] == status
    assert problem["code"] == code
    assert problem.pop("traceId") == response.headers["x-trace-id"]
    return problem


def assert_route_inventory(app: FastAPI) -> None:
    operations: set[tuple[str, str]] = set()
    for route in app.routes:
        # FastAPI includes routers lazily; effective candidates also contain hidden routes.
        expand = getattr(route, "effective_candidates", None)
        candidates = expand() if expand is not None else (route,)
        for candidate in candidates:
            path = getattr(candidate, "path", "")
            if not path.startswith("/api/v1/") or path == "/api/v1/{path:path}":
                continue
            operations.update((method, path) for method in getattr(candidate, "methods", ()))
    covered = {(case.method, case.path) for case in CASES}
    assert not covered.intersection(NON_USER_DATA)
    assert operations == covered | NON_USER_DATA.keys(), (
        "New user-data endpoints must adopt the two-user matrix; "
        "only genuinely non-user-data operations may have an explicit exemption."
    )


def test_every_product_operation_has_a_matrix_case_or_reviewable_exemption(
    local_settings: Settings,
) -> None:
    assert_route_inventory(create_app(local_settings))


def test_structured_recipe_and_reference_stay_in_the_authenticated_partition(
    two_user_api: TwoUserApi,
) -> None:
    api = two_user_api
    alice, bob = api.users
    api.executor.recipe = full_recipe()
    before = len(api.transport.item_calls())
    generated = api.request(SEND_CASE, alice, headers=bob.hints())
    assert generated.status_code == 200, generated.text
    api.assert_partition(alice, before)
    turn = generated.json()["data"]
    assert turn["recipe"] and turn["generatedRecipeId"] == str(api.executor.recipe_id)
    calls = len(api.executor.calls)
    denied = api.request(SEND_CASE, bob, target=alice, headers=alice.hints())
    assert_problem(denied, 404, "not_found")
    history_case = next(
        case
        for case in CASES
        if case.path.endswith("/{conversation_id}/turns") and case.method == "GET"
    )
    before = len(api.transport.item_calls())
    history = api.request(history_case, bob, headers=alice.hints())
    assert history.status_code == 200
    api.assert_partition(bob, before)
    assert turn["generatedRecipeId"] not in history.text
    assert all(
        item["generatedRecipeId"] in (None, bob.generated_recipe_id)
        for item in history.json()["items"]
    )
    alice.assert_private(history)
    inspected = api.client.get(generated.headers["location"], headers=bob.headers)
    assert_problem(inspected, 404, "not_found")
    assert turn["generatedRecipeId"] not in denied.text + inspected.text
    assert len(api.executor.calls) == calls


@pytest.mark.parametrize("include_in_schema", [True, False])
def test_route_inventory_rejects_new_operations_even_when_hidden(
    local_settings: Settings, include_in_schema: bool
) -> None:
    app = create_app(local_settings)
    router = APIRouter()

    @router.get("/unregistered-user-data", include_in_schema=include_in_schema)
    async def unregistered() -> dict[str, str]:
        return {"synthetic": "not a production endpoint"}

    app.include_router(router, prefix="/api/v1")
    with pytest.raises(AssertionError, match="New user-data endpoints"):
        assert_route_inventory(app)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("actor_index", [0, 1])
def test_verified_user_can_read_and_mutate_only_their_partition_despite_forged_hints(
    two_user_api: TwoUserApi, case: EndpointCase, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    before = {
        key: deepcopy(value)
        for key, value in two_user_api.transport.documents.items()
        if key[1] == other.user.user_id
    }
    since = len(two_user_api.transport.item_calls())
    response = two_user_api.request(case, actor, headers=other.hints())
    assert response.status_code == case.status, response.text
    assert response.headers["cache-control"] == "no-store"
    other.assert_private(response)
    assert {
        key: value
        for key, value in two_user_api.transport.documents.items()
        if key[1] == other.user.user_id
    } == before
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize("case", FOREIGN_ID_CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("actor_index", [0, 1])
def test_guessed_foreign_ids_and_absent_ids_have_identical_safe_errors_and_no_side_effects(
    two_user_api: TwoUserApi, case: EndpointCase, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    absent = replace(
        other,
        grocery_id=str(uuid4()),
        conversation_id=str(uuid4()),
        operation_id=str(uuid4()),
        generated_recipe_id=str(uuid4()),
        history_id=str(uuid4()),
    )
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    problems = []
    for target in (other, absent):
        response = two_user_api.request(case, actor, target, headers=other.hints())
        problems.append(assert_problem(response, 404, "not_found"))
        other.assert_private(response)
    assert problems[0] == problems[1]
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize(
    "case", [case for case in CASES if case.guarded_record is not None], ids=lambda case: case.name
)
@pytest.mark.parametrize("actor_index", [0, 1])
def test_wrong_owner_records_in_the_authenticated_partition_fail_before_data_or_conflicts(
    two_user_api: TwoUserApi, case: EndpointCase, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    if case.guarded_record == "profile":
        record = two_user_api.profile(actor)
    elif case.guarded_record == "conversation":
        record = two_user_api.transport.documents[
            ("ChatHistory", actor.user.user_id, actor.conversation_id)
        ]
    elif case.guarded_record == "turn":
        record = two_user_api.transport.documents[
            ("ChatHistory", actor.user.user_id, actor.turn_ids[0])
        ]
    else:
        record = two_user_api.transport.documents[
            ("RecipeHistory", actor.user.user_id, actor.history_id)
        ]
    record["userId"] = other.user.user_id
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    response = two_user_api.request(case, actor, headers={"If-Match": '"stale"'})
    assert_problem(response, 404, "not_found")
    other.assert_private(response)
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize(
    ("case", "attack"),
    [(case, "query") for case in CASES]
    + [(case, "body") for case in CASES if case.method != "GET"],
    ids=lambda value: value.name if isinstance(value, EndpointCase) else value,
)
def test_client_user_id_is_rejected_without_storage_or_execution(
    two_user_api: TwoUserApi, case: EndpointCase, attack: str
) -> None:
    actor, other = two_user_api.users
    calls = len(two_user_api.transport.calls)
    executions = len(two_user_api.executor.calls)
    before = deepcopy(two_user_api.transport.documents)
    response = two_user_api.request(
        case,
        actor,
        body={**(case.body or {}), "userId": other.user.user_id} if attack == "body" else None,
        params={"userId": other.user.user_id} if attack == "query" else None,
    )
    assert_problem(response, 422, "invalid_input")
    other.assert_private(response)
    assert len(two_user_api.transport.calls) == calls
    assert len(two_user_api.executor.calls) == executions
    assert two_user_api.transport.documents == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_forged_identity_headers_without_verified_credentials_fail_closed(
    two_user_api: TwoUserApi, case: EndpointCase
) -> None:
    actor, other = two_user_api.users
    calls = len(two_user_api.transport.calls)
    executions = len(two_user_api.executor.calls)
    response = two_user_api.client.request(
        case.method,
        actor.path(case),
        headers={**other.hints(), "If-Match": '"stale"', "Idempotency-Key": "forged"},
        json=case.body,
    )
    assert_problem(response, 401, "unauthenticated")
    assert response.headers["www-authenticate"] == "Bearer"
    other.assert_private(response)
    assert len(two_user_api.transport.calls) == calls
    assert len(two_user_api.executor.calls) == executions


@pytest.mark.parametrize("kind", ["conversation", "turn"])
@pytest.mark.parametrize("actor_index", [0, 1])
def test_every_page_is_owned_and_another_users_cursor_is_rejected_before_storage(
    two_user_api: TwoUserApi, kind: str, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    suffix = "/{conversation_id}/turns" if kind == "turn" else ""
    case = EndpointCase("GET", BASE + suffix)
    cursor: str | None = None
    identifiers = []
    since = len(two_user_api.transport.item_calls())
    while True:
        response = two_user_api.request(
            case, actor, params={"cursor": cursor} if cursor else {"limit": "1"}
        )
        assert response.status_code == 200, response.text
        other.assert_private(response)
        page = response.json()
        assert len(page["items"]) == 1
        identifiers.append(page["items"][0]["turnId" if kind == "turn" else "conversationId"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
        calls = len(two_user_api.transport.calls)
        stolen = two_user_api.request(case, other, params={"cursor": cursor})
        assert_problem(stolen, 400, "invalid_cursor")
        actor.assert_private(stolen)
        assert cursor not in stolen.text
        assert len(two_user_api.transport.calls) == calls
    expected = (
        actor.turn_ids
        if kind == "turn"
        else (
            actor.conversation_id,
            actor.second_conversation_id,
        )
    )
    assert len(identifiers) == len(expected)
    assert set(identifiers) == set(expected)
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize("view", ["all", "awaitingFeedback", "rated"])
@pytest.mark.parametrize("actor_index", [0, 1])
def test_recipe_pages_and_filter_cursors_are_owned_in_both_directions(
    two_user_api: TwoUserApi, view: str, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    case = EndpointCase("GET", "/api/v1/recipes")
    expected = []
    for owner in two_user_api.users:
        template = two_user_api.transport.documents[
            ("RecipeHistory", owner.user.user_id, owner.history_id)
        ]
        seeded = [
            seed_history(
                two_user_api.transport, template, rating="liked" if view == "rated" else None
            )
            for _ in range(2)
        ]
        if owner == actor:
            expected = [record["id"] for record in seeded]
            if view in {"all", "awaitingFeedback"}:
                expected.append(owner.history_id)
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    params = {} if view == "all" else {view: "true"}
    cursor = None
    identifiers = []
    for _ in range(4):
        response = two_user_api.request(
            case,
            actor,
            headers=other.hints(),
            params={**params, **({"cursor": cursor} if cursor else {"limit": "1"})},
        )
        assert response.status_code == 200, response.text
        other.assert_private(response)
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert len(page["items"]) == 1
        identifiers.append(page["items"][0]["historyId"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
        calls = len(two_user_api.transport.calls)
        stolen = two_user_api.request(case, other, params={**params, "cursor": cursor})
        assert_problem(stolen, 400, "invalid_cursor")
        actor.assert_private(stolen)
        assert cursor not in stolen.text and len(two_user_api.transport.calls) == calls
    else:
        pytest.fail("Recipe pagination did not terminate.")
    assert len(identifiers) == len(set(identifiers)) == len(expected)
    assert set(identifiers) == set(expected)
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize("view", ["awaitingFeedback", "rated"])
@pytest.mark.parametrize("actor_index", [0, 1])
def test_filtered_recipe_queries_reject_wrong_owner_records_in_the_callers_partition(
    two_user_api: TwoUserApi, view: str, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    template = two_user_api.transport.documents[
        ("RecipeHistory", actor.user.user_id, actor.history_id)
    ]
    record = seed_history(
        two_user_api.transport, template, rating="liked" if view == "rated" else None
    )
    record["userId"] = other.user.user_id
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    response = two_user_api.request(
        EndpointCase("GET", "/api/v1/recipes"), actor, params={view: "true"}
    )
    assert_problem(response, 404, "not_found")
    other.assert_private(response)
    assert record["id"] not in response.text and record["_etag"] not in response.text
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize(
    "reference",
    ["wrong_conversation", "foreign_operation", "foreign_turn", "wrong_operation_owner", "retry"],
)
@pytest.mark.parametrize("actor_index", [0, 1])
def test_chat_references_cannot_cross_users_or_conversations(
    two_user_api: TwoUserApi, reference: str, actor_index: int
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    target = actor
    operation = two_user_api.transport.documents[
        ("ChatHistory", actor.user.user_id, actor.operation_id)
    ]
    if reference == "wrong_conversation":
        target = replace(actor, conversation_id=actor.second_conversation_id)
    elif reference == "foreign_operation":
        target = replace(actor, operation_id=other.operation_id)
    elif reference == "foreign_turn":
        operation["turnId"] = other.turn_ids[0]
    elif reference == "wrong_operation_owner":
        operation["userId"] = other.user.user_id
    case = SEND_CASE if reference == "retry" else INSPECT_CASE
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    response = two_user_api.request(
        case,
        actor,
        target,
        body={"content": "matrix dinner request", "retryOfTurnId": other.turn_ids[0]}
        if reference == "retry"
        else None,
    )
    assert_problem(
        response,
        502 if reference == "foreign_turn" else 404,
        "cosmos_invalid_result" if reference == "foreign_turn" else "not_found",
    )
    other.assert_private(response)
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize("case", [SAVE_CASE, REJECT_CASE, REVISE_CASE], ids=lambda case: case.name)
@pytest.mark.parametrize("actor_index", [0, 1])
@pytest.mark.parametrize("reference", ["foreign_conversation", "wrong_conversation_owner"])
def test_recipe_sources_require_their_owned_parent_conversation(
    two_user_api: TwoUserApi, case: EndpointCase, actor_index: int, reference: str
) -> None:
    actor, other = two_user_api.users[actor_index], two_user_api.users[1 - actor_index]
    source = two_user_api.transport.documents[
        ("ChatHistory", actor.user.user_id, actor.turn_ids[0])
    ]
    if reference == "foreign_conversation":
        source["conversationId"] = other.conversation_id
    else:
        two_user_api.transport.documents[
            ("ChatHistory", actor.user.user_id, actor.conversation_id)
        ]["userId"] = other.user.user_id
    before = deepcopy(two_user_api.transport.documents)
    executions = len(two_user_api.executor.calls)
    since = len(two_user_api.transport.item_calls())
    response = two_user_api.request(case, actor)
    assert_problem(response, 404, "not_found")
    other.assert_private(response)
    assert two_user_api.transport.documents == before
    assert len(two_user_api.executor.calls) == executions
    two_user_api.assert_partition(actor, since)


@pytest.mark.parametrize("failure", ["duplicate", "stale"])
def test_conflicts_expose_only_owned_metadata_and_do_not_mutate(
    two_user_api: TwoUserApi, failure: str
) -> None:
    actor, other = two_user_api.users
    before = deepcopy(two_user_api.transport.documents)
    since = len(two_user_api.transport.item_calls())
    case = EndpointCase(
        "POST" if failure == "duplicate" else "PATCH",
        GROCERIES if failure == "duplicate" else GROCERIES + "/{grocery_id}",
        body={"name": actor.marker + " grocery"} if failure == "duplicate" else {"quantity": 2},
    )
    response = two_user_api.request(
        case,
        actor,
        headers={"If-Match": two_user_api.profile(other)["_etag"]} if failure == "stale" else None,
    )
    assert_problem(
        response,
        409 if failure == "duplicate" else 412,
        "duplicate_grocery" if failure == "duplicate" else "revision_conflict",
    )
    other.assert_private(response)
    assert two_user_api.transport.documents == before
    two_user_api.assert_partition(actor, since)


def test_matrix_probe_detects_a_deliberately_disabled_ownership_guard(
    two_user_api: TwoUserApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor, other = two_user_api.users
    two_user_api.profile(actor)["userId"] = other.user.user_id
    assert_problem(two_user_api.request(CASES[0], actor), 404, "not_found")

    def unguarded(
        self: AuthenticatedUser, resource: Mapping[str, object] | None
    ) -> Mapping[str, object] | None:
        return resource

    monkeypatch.setattr(AuthenticatedUser, "require_owned", unguarded)
    exposed = two_user_api.request(CASES[0], actor)
    assert exposed.status_code == 200
    assert exposed.json()["data"]["userId"] == other.user.user_id
    with pytest.raises(AssertionError, match="Authorization regression"):
        assert_problem(exposed, 404, "not_found")
