import asyncio
import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

import httpx
import pytest
from azure.core.pipeline.transport import AsyncHttpResponse, HttpRequest
from conftest import AUTH_ISSUER
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import JsonValue
from sous_chef.adapters import cosmos, foundry, foundry_gateway
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import MAX_RECORD_BYTES, CosmosError, CosmosFailure, RecordPatch
from sous_chef.settings import Settings
from test_cosmos_repository import USERS, SyntheticCredential, Transport
from test_cosmos_repository import Response as CosmosResponse
from test_cosmos_repository import harness as harness
from test_profile_api import ME, USER, document, onboard, signed_token
from test_profile_api import profile_client as profile_client
from test_profile_api import profile_settings as profile_settings

GROCERIES = "/api/v1/groceries"


@pytest.fixture
def grocery_client(profile_client: TestClient) -> TestClient:
    assert profile_client.get(ME).status_code == 200
    return profile_client


def add(client: TestClient, **body: object) -> Response:
    current = client.get(GROCERIES)
    assert current.status_code == 200, current.text
    response = client.post(
        GROCERIES, headers={"If-Match": current.headers["etag"]}, json={"name": "lime", **body}
    )
    assert response.status_code == 201, response.text
    return response


def test_inventory_round_trip_preserves_profile_and_never_invokes_ai_or_learning(
    grocery_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Grocery operations must not construct or invoke model clients.")

    for model in (foundry.FoundryModelClient, foundry_gateway.FoundryGateway):
        monkeypatch.setattr(model, "__init__", forbidden)
        monkeypatch.setattr(model, "invoke", forbidden)
    onboard(grocery_client)
    stored = document(transport)
    stored["learnedPreferences"] = [{"insight": "synthetic learned taste", "confidence": 0.8}]
    stored["futurePrivateState"] = {"private": "unchanged"}
    before = deepcopy(stored)
    empty = grocery_client.get(GROCERIES)
    assert empty.json() == {"items": [], "nextCursor": None}
    assert empty.headers["etag"] == grocery_client.get(ME).headers["etag"]
    created = add(grocery_client, name="  synthetic lime  ")
    row = created.json()["data"]
    assert row["name"] == "synthetic lime"
    assert row["quantity"] is row["unit"] is row["ingredientId"] is None
    assert row["updatedAt"].endswith("Z")
    path = GROCERIES + "/" + row["groceryId"]
    assert created.headers["location"] == path
    etag = created.headers["etag"]
    for quantity in (1.125, 2.125, 1.125):
        updated = grocery_client.patch(
            path, headers={"If-Match": etag}, json={"quantity": quantity, "unit": "piece"}
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["groceryId"] == row["groceryId"]
        assert updated.json()["data"]["quantity"] == quantity
        assert updated.headers["etag"] != etag
        assert updated.headers["cache-control"] == "no-store"
        etag = updated.headers["etag"]
    listed = grocery_client.get(GROCERIES)
    assert listed.json()["items"] == [updated.json()["data"]]
    removed = grocery_client.delete(path, headers={"If-Match": etag})
    assert removed.status_code == 204 and not removed.content
    assert removed.headers["etag"] != etag
    assert grocery_client.get(GROCERIES).json() == {"items": [], "nextCursor": None}
    for field in ("explicitPreferences", "learnedPreferences", "futurePrivateState", "createdAt"):
        assert document(transport)[field] == before[field]
    assert all("/colls/Users/docs" in call.path for call in transport.item_calls())
    assert len(transport.documents) == 1
    assert all(
        operation["path"] in {"/groceries", "/updatedAt"}
        for call in transport.item_calls("PATCH")
        if any(operation["path"] == "/groceries" for operation in call.body["operations"])
        for operation in call.body["operations"]
    )
    assert "synthetic lime" not in caplog.text
    assert "synthetic learned taste" not in caplog.text


def test_duplicates_identify_owned_row_without_merging_or_converting_units(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    first = add(grocery_client, name="Straße", quantity=2, unit="g")
    duplicate = grocery_client.post(
        GROCERIES,
        headers={"If-Match": first.headers["etag"]},
        json={"name": " STRASSE ", "quantity": 3, "unit": "g"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["errors"] == [
        {"field": "name", "code": "duplicate", "groceryId": first.json()["data"]["groceryId"]}
    ]
    other = add(grocery_client, name="strasse", quantity=1, unit="kg")
    renamed = grocery_client.patch(
        GROCERIES + "/" + other.json()["data"]["groceryId"],
        headers={"If-Match": other.headers["etag"]},
        json={"unit": "g"},
    )
    assert renamed.status_code == 409
    unknown_unit = add(grocery_client, name="strasse", quantity=4)
    assert unknown_unit.json()["data"]["unit"] is None
    assert [row["quantity"] for row in document(transport)["groceries"]] == [2, 1, 4]


def test_unknown_quantities_and_mapping_changes_are_explicit(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    created = add(grocery_client, name="lime", quantity=2, unit="piece")
    path = GROCERIES + "/" + created.json()["data"]["groceryId"]
    document(transport)["groceries"][0]["ingredientId"] = "ingredient:lime"
    unknown = grocery_client.patch(
        path, headers={"If-Match": created.headers["etag"]}, json={"quantity": None}
    )
    assert unknown.status_code == 200
    assert unknown.json()["data"]["quantity"] is None
    assert unknown.json()["data"]["ingredientId"] == "ingredient:lime"
    renamed = grocery_client.patch(
        path, headers={"If-Match": unknown.headers["etag"]}, json={"name": "lemon", "unit": None}
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["groceryId"] == created.json()["data"]["groceryId"]
    assert renamed.json()["data"]["ingredientId"] is renamed.json()["data"]["unit"] is None


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"quantity": 0}, "quantity", "must_be_positive"),
        ({"quantity": -1}, "quantity", "must_be_positive"),
        ({"quantity": 1_000_001}, "quantity", "out_of_range"),
        ({"quantity": 0.0001}, "quantity", "too_many_decimal_places"),
        ({"quantity": float("nan")}, "quantity", "invalid_value"),
        ({"quantity": float("inf")}, "quantity", "invalid_value"),
        ({"quantity": True}, "quantity", "invalid_value"),
        ({"quantity": "2"}, "quantity", "invalid_value"),
        ({"name": "  "}, "name", "blank"),
        ({"name": "x" * 101}, "name", "too_long"),
        ({"name": "bad\x00name"}, "name", "invalid_value"),
        ({"name": "bad\nname"}, "name", "invalid_value"),
        ({"name": 123}, "name", "invalid_value"),
        ({"name": None}, "name", "null_not_allowed"),
        ({"unit": "litres"}, "unit", "invalid_value"),
        ({"unit": "KG"}, "unit", "invalid_value"),
        ({}, "groceries", "empty_update"),
    ],
)
def test_invalid_edits_never_mutate_or_silently_remove_inventory(
    grocery_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    body: dict[str, object],
    field: str,
    code: str,
) -> None:
    transport, _ = harness
    created = add(grocery_client, quantity=1)
    before = deepcopy(document(transport))
    count = len(transport.item_calls("PATCH"))
    response = grocery_client.patch(
        GROCERIES + "/" + created.json()["data"]["groceryId"],
        headers={"If-Match": created.headers["etag"], "Content-Type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422, response.text
    assert {"field": field, "code": code} in response.json()["errors"]
    assert document(transport) == before
    assert len(transport.item_calls("PATCH")) == count
    assert "bad" not in response.text


@pytest.mark.parametrize("quantity", [0.001, 1_000_000])
def test_quantity_and_name_boundaries_round_trip(
    grocery_client: TestClient, quantity: float
) -> None:
    created = add(grocery_client, name="\u00e9" * 100, quantity=quantity, unit="kg")
    assert created.json()["data"]["quantity"] == quantity
    assert grocery_client.get(GROCERIES).json()["items"] == [created.json()["data"]]


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_mutations_require_current_collection_revision(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential], method: str
) -> None:
    transport, _ = harness
    stale = grocery_client.get(GROCERIES).headers["etag"]
    created = add(grocery_client)
    path = GROCERIES if method == "POST" else GROCERIES + "/" + created.json()["data"]["groceryId"]
    body = {"name": "new grocery"} if method == "POST" else {"quantity": 2}
    before = deepcopy(document(transport))
    missing = grocery_client.request(method, path, json=body if method != "DELETE" else None)
    assert missing.status_code == 428
    outdated = grocery_client.request(
        method, path, headers={"If-Match": stale}, json=body if method != "DELETE" else None
    )
    assert outdated.status_code == 412
    assert document(transport) == before


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "field", ["userId", "groceryId", "ingredientId", "updatedAt", "learnedPreferences"]
)
def test_clients_cannot_supply_scope_mapping_or_inferred_fields(
    grocery_client: TestClient, method: str, field: str
) -> None:
    created = add(grocery_client)
    path = GROCERIES if method == "POST" else GROCERIES + "/" + created.json()["data"]["groceryId"]
    response = grocery_client.request(
        method,
        path,
        headers={"If-Match": created.headers["etag"]},
        json={"name": "another", field: "private-value"},
    )
    assert response.status_code == 422
    assert {"field": "request", "code": "unknown_field"} in response.json()["errors"]
    assert field not in response.text and "private-value" not in response.text


def test_verified_partition_blocks_other_users_ids_and_scope_hints(
    grocery_client: TestClient,
    signing_key: rsa.RSAPrivateKey,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    alice = add(grocery_client, name="alice lime")
    grocery_client.headers["Authorization"] = "Bearer " + signed_token(signing_key, "bob")
    assert grocery_client.get(ME).status_code == 200
    bob = add(grocery_client, name="bob lime")
    bob_user = AuthenticatedUser.from_verified_identity(AUTH_ISSUER, "bob")
    before = deepcopy(document(transport, bob_user))
    grocery_client.headers.update(auth_headers)
    grocery_client.headers["X-User-ID"] = bob_user.user_id
    grocery_client.headers["X-MS-CLIENT-PRINCIPAL-ID"] = bob_user.user_id
    assert grocery_client.get(GROCERIES).json()["items"] == [alice.json()["data"]]
    for foreign_id in (bob.json()["data"]["groceryId"], str(uuid4())):
        for method in ("PATCH", "DELETE"):
            response = grocery_client.request(
                method,
                GROCERIES + "/" + foreign_id,
                headers={"If-Match": alice.headers["etag"]},
                json={"quantity": 9} if method == "PATCH" else None,
            )
            assert response.status_code == 404
    assert grocery_client.get(GROCERIES + "?userId=" + bob_user.user_id).status_code == 422
    assert document(transport, bob_user) == before
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"])[0]
        in {USER.user_id, bob_user.user_id}
        for call in transport.item_calls()
    )


def test_concurrent_increments_conflict_then_explicit_retry_preserves_both(
    grocery_client: TestClient,
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    created = add(grocery_client, quantity=1)
    path = GROCERIES + "/" + created.json()["data"]["groceryId"]
    transport.block_reads = 2
    transport.read_count = 0
    transport.barrier = asyncio.Event()

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(profile_settings)),
            base_url="https://testserver",
            headers=auth_headers,
        ) as client:
            results = await asyncio.gather(
                *(
                    client.patch(
                        path, headers={"If-Match": created.headers["etag"]}, json={"quantity": 2}
                    )
                    for _ in range(2)
                )
            )
            assert sorted(result.status_code for result in results) == [200, 412]
            latest = await client.get(GROCERIES)
            retry = await client.patch(
                path,
                headers={"If-Match": latest.headers["etag"]},
                json={"quantity": latest.json()["items"][0]["quantity"] + 1},
            )
            assert retry.status_code == 200
            assert retry.json()["data"]["quantity"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["explicitPreferences", "learnedPreferences"])
def test_concurrent_inventory_and_other_profile_sections_survive_explicit_conflict_retry(
    grocery_client: TestClient,
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    field: str,
) -> None:
    transport, _ = harness
    onboard(grocery_client)
    created = add(grocery_client, quantity=1)
    path = GROCERIES + "/" + created.json()["data"]["groceryId"]
    independent_fields: dict[str, JsonValue] = {
        "explicitPreferences": {
            **deepcopy(document(transport)["explicitPreferences"]),
            "likes": ["synthetic explicit taste"],
        },
        "learnedPreferences": [{"insight": "synthetic learned taste", "confidence": 0.8}],
    }
    other = independent_fields[field]
    transport.block_reads = 2
    transport.read_count = 0
    transport.barrier = asyncio.Event()

    async def scenario() -> None:
        async with (
            cosmos.open_cosmos(profile_settings) as repository,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(profile_settings)),
                base_url="https://testserver",
                headers=auth_headers,
            ) as client,
        ):
            inventory, independent = await asyncio.gather(
                client.patch(
                    path, headers={"If-Match": created.headers["etag"]}, json={"quantity": 2}
                ),
                repository.patch(
                    USER,
                    USERS,
                    USER.user_id,
                    RecordPatch(etag=created.headers["etag"], fields={field: other}),
                ),
                return_exceptions=True,
            )
            assert isinstance(inventory, httpx.Response)
            if inventory.status_code == 412:
                assert not isinstance(independent, BaseException)
                latest = await client.get(GROCERIES)
                retried = await client.patch(
                    path, headers={"If-Match": latest.headers["etag"]}, json={"quantity": 2}
                )
                assert retried.status_code == 200
            else:
                assert inventory.status_code == 200
                assert (
                    isinstance(independent, CosmosError)
                    and independent.code == CosmosFailure.CONFLICT
                )
                await repository.patch(
                    USER,
                    USERS,
                    USER.user_id,
                    RecordPatch(etag=inventory.headers["etag"], fields={field: other}),
                )

    asyncio.run(scenario())
    assert document(transport)["groceries"][0]["quantity"] == 2
    assert document(transport)[field] == other


def test_full_inventory_is_bounded_and_projected_record_limit_preserves_state(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    created = add(grocery_client)
    row = created.json()["data"]
    stored = document(transport)
    stored["groceries"] = [
        {**row, "groceryId": str(uuid4()), "name": f"synthetic grocery {index}"}
        for index in range(200)
    ]
    assert len(grocery_client.get(GROCERIES).json()["items"]) == 200
    full = grocery_client.post(
        GROCERIES, headers={"If-Match": created.headers["etag"]}, json={"name": "overflow"}
    )
    assert full.status_code == 422 and full.json()["code"] == "grocery_limit_reached"
    stored["groceries"] = [row]
    stored["futurePrivateState"] = ""
    size = len(json.dumps({key: value for key, value in stored.items() if key != "_etag"}).encode())
    stored["futurePrivateState"] = "x" * (MAX_RECORD_BYTES - size - 5)
    before = deepcopy(stored)
    oversized = grocery_client.patch(
        GROCERIES + "/" + row["groceryId"],
        headers={"If-Match": created.headers["etag"]},
        json={"name": "x" * 100},
    )
    assert oversized.status_code == 413
    assert stored == before


def test_uncertain_write_is_not_replayed_or_reported_as_success(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    created = add(grocery_client, quantity=1)
    transport.write_then_disconnect = True
    count = len(transport.item_calls("PATCH"))
    response = grocery_client.patch(
        GROCERIES + "/" + created.json()["data"]["groceryId"],
        headers={"If-Match": created.headers["etag"]},
        json={"quantity": 2},
    )
    assert response.status_code == 503
    assert "unknown" in response.json()["detail"]
    assert len(transport.item_calls("PATCH")) == count + 1
    assert grocery_client.get(GROCERIES).json()["items"][0]["quantity"] == 2
    assert "secret-private" not in response.text


@pytest.mark.parametrize("state", [None, {}, [{"name": "private-corrupt-value"}]])
def test_corrupt_storage_is_not_hidden_by_empty_inventory(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential], state: object
) -> None:
    transport, _ = harness
    document(transport)["groceries"] = state
    response = grocery_client.get(GROCERIES)
    assert response.status_code == 502 and response.json()["code"] == "cosmos_invalid_result"
    assert "private-corrupt-value" not in response.text


def test_missing_profile_or_configuration_is_not_a_successful_empty_inventory(
    profile_client: TestClient, local_settings: Settings, auth_headers: dict[str, str]
) -> None:
    assert profile_client.get(GROCERIES).status_code == 404
    with TestClient(create_app(local_settings), headers=auth_headers) as client:
        assert client.get(GROCERIES).status_code == 503


def test_delete_rejects_bodies_and_all_mutation_bodies_are_bounded(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    created = add(grocery_client)
    path = GROCERIES + "/" + created.json()["data"]["groceryId"]
    before = deepcopy(document(transport))
    for method, target in (("POST", GROCERIES), ("PATCH", path), ("DELETE", path)):
        response = grocery_client.request(
            method,
            target,
            headers={"If-Match": created.headers["etag"]},
            json={"name": "x" * (128 * 1024)},
        )
        assert response.status_code == 413
    rejected = grocery_client.request(
        "DELETE",
        path,
        headers={"If-Match": created.headers["etag"]},
        json={"userId": "private-value"},
    )
    assert rejected.status_code == 422 and "private-value" not in rejected.text
    assert document(transport) == before


@pytest.mark.parametrize("revision", ["*", 'W/"1"', '"1", "2"'])
def test_mutations_reject_non_specific_revisions(grocery_client: TestClient, revision: str) -> None:
    response = grocery_client.post(GROCERIES, headers={"If-Match": revision}, json={"name": "lime"})
    assert response.status_code == 422


@pytest.mark.parametrize("duplicate_id", [True, False])
def test_duplicate_stored_ids_or_names_are_not_rewritten_as_valid_state(
    grocery_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    duplicate_id: bool,
) -> None:
    transport, _ = harness
    created = add(grocery_client)
    row = created.json()["data"]
    duplicate = {**row, "name": "another"} if duplicate_id else {**row, "groceryId": str(uuid4())}
    document(transport)["groceries"].append(duplicate)
    before = len(transport.item_calls("PATCH"))
    assert grocery_client.get(GROCERIES).status_code == 502
    response = grocery_client.patch(
        GROCERIES + "/" + row["groceryId"],
        headers={"If-Match": created.headers["etag"]},
        json={"quantity": 2},
    )
    assert response.status_code == 502
    assert len(transport.item_calls("PATCH")) == before


def test_invalid_result_after_committed_write_reports_unknown_outcome(
    grocery_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness
    created = add(grocery_client, quantity=1)
    original_send = transport.send

    async def invalid_result(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        response = await original_send(request, **kwargs)
        if request.method == "PATCH":
            assert isinstance(response, CosmosResponse)
            body = json.loads(response.body())
            body["groceries"] = None
            response._body = json.dumps(body).encode()
        return response

    monkeypatch.setattr(transport, "send", invalid_result)
    count = len(transport.item_calls("PATCH"))
    response = grocery_client.patch(
        GROCERIES + "/" + created.json()["data"]["groceryId"],
        headers={"If-Match": created.headers["etag"]},
        json={"quantity": 2},
    )
    assert response.status_code == 502
    assert "outcome is unknown" in response.json()["detail"]
    assert document(transport)["groceries"][0]["quantity"] == 2
    assert len(transport.item_calls("PATCH")) == count + 1


def test_rejected_database_write_never_returns_success_or_changes_inventory(
    grocery_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    created = add(grocery_client)
    before = deepcopy(document(transport))
    count = len(transport.item_calls("PATCH"))
    transport.faults["PATCH"].append(503)
    response = grocery_client.patch(
        GROCERIES + "/" + created.json()["data"]["groceryId"],
        headers={"If-Match": created.headers["etag"]},
        json={"quantity": 2},
    )
    assert response.status_code == 503
    assert "etag" not in response.headers and "data" not in response.json()
    assert document(transport) == before
    assert len(transport.item_calls("PATCH")) == count + 1


def test_hosted_grocery_mutations_keep_csrf_and_origin_protection(
    profile_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    with TestClient(
        create_app(profile_settings.model_copy(update={"environment": "dev"})),
        base_url="https://testserver",
        headers={"X-MS-TOKEN-AAD-ID-TOKEN": signed_token(signing_key)},
    ) as client:
        assert client.get(ME).status_code == 200
        session = client.get("/auth/session")
        csrf = session.json()["csrfToken"]
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": csrf})
        created = add(client)
        path = GROCERIES + "/" + created.json()["data"]["groceryId"]
        client.headers.pop("X-CSRF-Token")
        for method, target in (("POST", GROCERIES), ("PATCH", path), ("DELETE", path)):
            body = (
                {"name": "another"}
                if method == "POST"
                else {"quantity": 2}
                if method == "PATCH"
                else None
            )
            for headers in ({}, {"Origin": "https://other.invalid", "X-CSRF-Token": csrf}):
                response = client.request(
                    method,
                    target,
                    headers={**headers, "If-Match": created.headers["etag"]},
                    json=body,
                )
                assert response.status_code == 403
                assert response.json()["code"] == "csrf_failed"


@pytest.mark.parametrize(
    "quantity",
    [
        "1.0000000000000001",
        "1000000.00000000001",
        "1.00000000000000000000000000000001",
        "1e309",
        "1e-1000000000",
    ],
)
def test_wire_precision_is_not_rounded_into_an_allowed_quantity(
    grocery_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    quantity: str,
) -> None:
    transport, _ = harness
    revision = grocery_client.get(GROCERIES).headers["etag"]
    response = grocery_client.post(
        GROCERIES,
        headers={"If-Match": revision, "Content-Type": "application/json"},
        content='{"name":"lime","quantity":' + quantity + "}",
    )
    assert response.status_code == 422
    assert document(transport)["groceries"] == []
