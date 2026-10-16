import asyncio
import json
from collections.abc import Iterator
from copy import deepcopy
from time import time
from typing import Any, Literal

import httpx
import jwt
import pytest
from conftest import AUTH_AUDIENCE, AUTH_ISSUER
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Response as ClientResponse
from sous_chef.adapters import cosmos, foundry, foundry_gateway
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import MAX_RECORD_BYTES, CosmosError, CosmosFailure, RecordPatch
from sous_chef.domain.profiles import PREFERENCE_FIELDS
from sous_chef.settings import Settings
from test_cosmos_repository import USERS, SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_cosmos_repository import settings as cosmos_settings

ME = "/api/v1/me"
USER = AuthenticatedUser.from_verified_identity(AUTH_ISSUER, "synthetic-user")


def signed_token(key: rsa.RSAPrivateKey, subject: str = "synthetic-user") -> str:
    now = int(time())
    return jwt.encode(
        {
            "iss": AUTH_ISSUER,
            "aud": AUTH_AUDIENCE,
            "sub": subject,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "email": "synthetic-email-not-for-storage",
        },
        key,
        algorithm="RS256",
        headers={"kid": "synthetic"},
    )


def preferences() -> dict[str, Any]:
    return {
        "allergies": ["peanut", "peaches"],
        "dietaryRestrictions": ["vegetarian"],
        "likes": ["tart flavors"],
        "dislikes": ["bitter flavors"],
        "preferredCuisines": ["Italian"],
        "spicePreference": "medium",
        "cookingSkill": "intermediate",
        "typicalTimeBudgetMinutes": 30,
        "nutritionGoals": ["higher protein", "lower sodium"],
    }


@pytest.fixture
def profile_settings(local_settings: Settings) -> Settings:
    return cosmos_settings(
        auth_issuer=local_settings.auth_issuer,
        auth_jwks_uri=local_settings.auth_jwks_uri,
        auth_client_id=local_settings.auth_client_id,
        auth_origin=local_settings.auth_origin,
        _env_file=None,
    )


@pytest.fixture
def profile_client(
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> Iterator[TestClient]:
    with TestClient(create_app(profile_settings), headers=auth_headers) as client:
        yield client


def onboard(client: TestClient, body: dict[str, Any] | None = None) -> ClientResponse:
    current = client.get(ME)
    assert current.status_code == 200
    result = client.put(
        ME + "/onboarding",
        headers={"If-Match": current.headers["etag"]},
        json=preferences() if body is None else body,
    )
    assert result.status_code == 200, result.text
    return result


def document(transport: Transport, user: AuthenticatedUser = USER) -> dict[str, Any]:
    return transport.documents[("Users", user.user_id, user.user_id)]


def test_openapi_matches_required_revisions_and_nonnullable_partial_fields(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    schema = profile_client.get("/openapi.json").json()
    partial = schema["components"]["schemas"]["PreferenceUpdate"]
    assert partial["minProperties"] == 1
    assert not partial.get("required")
    assert set(partial["properties"]) == PREFERENCE_FIELDS
    for field in partial["properties"].values():
        assert field.get("type") in {"array", "string", "integer"}
        assert "anyOf" not in field
    for suffix, method in (("/onboarding", "put"), ("/preferences", "patch")):
        headers = schema["paths"][ME + suffix][method]["parameters"]
        revision = next(header for header in headers if header["name"] == "If-Match")
        assert revision["required"] is True
    assert not transport.calls


def test_repeat_login_has_one_incomplete_profile_without_invented_choices(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, credential = harness
    first = profile_client.get(ME)
    again = profile_client.get(ME)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert first.headers["etag"] == again.headers["etag"]
    assert first.headers["cache-control"] == "no-store"
    assert set(first.json()["data"]) == {
        "userId",
        "onboardingCompleted",
        "explicitPreferences",
        "createdAt",
        "updatedAt",
    }
    assert first.json()["data"]["onboardingCompleted"] is False
    assert first.json()["data"]["explicitPreferences"] is None
    assert first.json()["data"]["userId"] == USER.user_id
    assert first.json()["data"]["createdAt"].endswith("Z")
    stored = document(transport)
    assert stored["learnedPreferences"] == stored["groceries"] == []
    assert "email" not in stored
    assert len(transport.documents) == len(transport.item_calls("POST")) == 1
    assert credential.closed and transport.closed
    for private in (USER.user_id, "synthetic-user", AUTH_ISSUER):
        assert private not in caplog.text
    assert all(call.path.endswith("/" + USER.user_id) for call in transport.item_calls("GET"))


def test_concurrent_first_login_reconciles_create_conflict(
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    transport.block_reads = 2

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(profile_settings)),
            base_url="https://testserver",
            headers=auth_headers,
        ) as client:
            results = await asyncio.gather(client.get(ME), client.get(ME))
        assert all(result.status_code == 200 for result in results)
        assert results[0].json() == results[1].json()
        assert results[0].headers["etag"] == results[1].headers["etag"]

    asyncio.run(scenario())
    assert len(transport.documents) == 1
    assert len(transport.item_calls("POST")) == 2


def test_lost_creation_response_is_not_replayed_and_next_login_reconciles(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    transport.write_then_disconnect = True
    uncertain = profile_client.get(ME)
    assert uncertain.status_code == 503
    assert "unknown" in uncertain.json()["detail"]
    assert len(transport.documents) == len(transport.item_calls("POST")) == 1
    recovered = profile_client.get(ME)
    assert recovered.status_code == 200
    assert recovered.json()["data"]["onboardingCompleted"] is False
    assert len(transport.item_calls("POST")) == 1
    assert "secret-private" not in uncertain.text


@pytest.mark.parametrize("field", sorted(PREFERENCE_FIELDS))
def test_onboarding_requires_every_field_including_explicit_empty_safety_choices(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    field: str,
) -> None:
    transport, _ = harness
    initial = profile_client.get(ME)
    body = preferences()
    del body[field]
    response = profile_client.put(
        ME + "/onboarding", headers={"If-Match": initial.headers["etag"]}, json=body
    )
    assert response.status_code == 422
    assert {"field": field, "code": "required"} in response.json()["errors"]
    assert document(transport)["onboardingCompleted"] is False
    assert not transport.item_calls("PATCH")


def test_empty_safety_selections_mean_confirmed_none_only_after_full_onboarding(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    first = profile_client.get(ME)
    blocked = profile_client.patch(
        ME + "/preferences", headers={"If-Match": first.headers["etag"]}, json=preferences()
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "onboarding_required"
    assert document(transport)["explicitPreferences"] is None
    body = preferences()
    body["allergies"] = body["dietaryRestrictions"] = []
    completed = onboard(profile_client, body)
    assert completed.json()["data"]["onboardingCompleted"] is True
    assert completed.json()["data"]["explicitPreferences"] == body
    assert profile_client.get(ME).json() == completed.json()


def test_all_fields_round_trip_and_partial_edits_preserve_unspecified_fields(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    original = onboard(profile_client)
    assert original.json()["data"]["explicitPreferences"] == preferences()
    assert document(transport)["learnedPreferences"] == []
    changes = {
        "allergies": [],
        "dietaryRestrictions": ["vegan"],
        "likes": [" citrus-forward flavors "],
        "dislikes": ["smoky flavors"],
        "preferredCuisines": ["Korean"],
        "spicePreference": "hot",
        "cookingSkill": "advanced",
        "typicalTimeBudgetMinutes": 45,
        "nutritionGoals": ["balanced eating"],
    }
    edited = profile_client.patch(
        ME + "/preferences", headers={"If-Match": original.headers["etag"]}, json=changes
    )
    assert edited.status_code == 200
    changes["likes"] = ["citrus-forward flavors"]
    assert edited.json()["data"]["explicitPreferences"] == changes
    partial = profile_client.patch(
        ME + "/preferences", headers={"If-Match": edited.headers["etag"]}, json={"dislikes": []}
    )
    assert partial.status_code == 200
    changes["dislikes"] = []
    assert partial.json()["data"]["explicitPreferences"] == changes
    assert partial.json()["data"]["createdAt"] == original.json()["data"]["createdAt"]
    assert partial.headers["etag"] != edited.headers["etag"]
    assert profile_client.get(ME).json() == partial.json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allergies", ["peanut"] * 50),
        ("dietaryRestrictions", ["vegan"] * 20),
        ("likes", [" " + "\U0001f34b" * 100 + " "] * 50),
        ("dislikes", ["x"] * 50),
        ("preferredCuisines", ["x"] * 20),
        ("nutritionGoals", ["x"] * 20),
        ("spicePreference", "none"),
        ("spicePreference", "mild"),
        ("cookingSkill", "beginner"),
        ("typicalTimeBudgetMinutes", 5),
        ("typicalTimeBudgetMinutes", 240),
    ],
)
def test_valid_boundaries_round_trip_after_trimming_unicode_codepoints(
    profile_client: TestClient, field: str, value: object
) -> None:
    completed = onboard(profile_client)
    response = profile_client.patch(
        ME + "/preferences", headers={"If-Match": completed.headers["etag"]}, json={field: value}
    )
    assert response.status_code == 200
    expected = [entry.strip() for entry in value] if isinstance(value, list) else value
    assert response.json()["data"]["explicitPreferences"][field] == expected


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("allergies", ["allergic to"], "unsupported_constraint"),
        ("dietaryRestrictions", ["keto"], "unsupported_constraint"),
        ("allergies", ["peanut"] * 51, "too_many_items"),
        ("dietaryRestrictions", ["vegan"] * 21, "too_many_items"),
        ("likes", ["x"] * 51, "too_many_items"),
        ("dislikes", ["x"] * 51, "too_many_items"),
        ("preferredCuisines", ["x"] * 21, "too_many_items"),
        ("nutritionGoals", ["x"] * 21, "too_many_items"),
        ("likes", ["x" * 101], "too_long"),
        ("likes", ["  "], "blank"),
        ("allergies", [True], "invalid_value"),
        ("likes", "private-input", "invalid_value"),
        ("spicePreference", "fiery", "invalid_value"),
        ("cookingSkill", "expert", "invalid_value"),
        ("typicalTimeBudgetMinutes", 4, "out_of_range"),
        ("typicalTimeBudgetMinutes", 241, "out_of_range"),
        ("typicalTimeBudgetMinutes", 30.0, "invalid_value"),
        ("typicalTimeBudgetMinutes", "30", "invalid_value"),
        ("typicalTimeBudgetMinutes", True, "invalid_value"),
        ("allergies", None, "null_not_allowed"),
        ("spicePreference", None, "null_not_allowed"),
    ],
)
def test_invalid_preferences_have_safe_field_errors_and_never_write(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    field: str,
    value: object,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness
    completed = onboard(profile_client)
    before = deepcopy(document(transport))
    writes = len(transport.item_calls("PATCH"))
    response = profile_client.patch(
        ME + "/preferences", headers={"If-Match": completed.headers["etag"]}, json={field: value}
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert any(
        error["field"].split(".")[0] == field and error["code"] == code
        for error in response.json()["errors"]
    )
    assert document(transport) == before
    assert len(transport.item_calls("PATCH")) == writes
    assert "private-input" not in response.text + caplog.text
    assert "unsupported_constraint" not in response.text or "keto" not in response.text


@pytest.mark.parametrize(
    "field",
    [
        "userId",
        "ownerId",
        "onboardingCompleted",
        "learnedPreferences",
        "confidence",
        "evidenceCount",
        "createdAt",
        "email",
        "private-property",
        "preferred_cuisines",
    ],
)
@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_scope_learned_state_metadata_and_unknown_properties_are_not_writable(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    field: str,
    method: str,
) -> None:
    transport, _ = harness
    completed = onboard(profile_client)
    before = deepcopy(document(transport))
    body = preferences() if method == "PUT" else {"likes": []}
    body[field] = "private-value"
    response = profile_client.request(
        method,
        ME + ("/onboarding" if method == "PUT" else "/preferences"),
        headers={"If-Match": completed.headers["etag"]},
        json=body,
    )
    assert response.status_code == 422
    assert {"field": "request", "code": "unknown_field"} in response.json()["errors"]
    assert field not in response.text
    assert "private-value" not in response.text
    assert document(transport) == before


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_writes_require_current_etag(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential], method: str
) -> None:
    transport, _ = harness
    initial = profile_client.get(ME)
    completed = onboard(profile_client)
    body = preferences() if method == "PUT" else {"likes": []}
    path = ME + ("/onboarding" if method == "PUT" else "/preferences")
    missing = profile_client.request(method, path, json=body)
    assert missing.status_code == 428
    assert missing.json()["code"] == "precondition_required"
    stale = profile_client.request(
        method, path, headers={"If-Match": initial.headers["etag"]}, json=body
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "revision_conflict"
    assert document(transport)["explicitPreferences"] == preferences()
    assert document(transport)["_etag"] == completed.headers["etag"]


@pytest.mark.parametrize("etag", ["", "*", 'W/"1"', "bad revision", "x" * 1025])
def test_invalid_revisions_are_rejected_before_cosmos(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential], etag: str
) -> None:
    transport, _ = harness
    response = profile_client.put(
        ME + "/onboarding", headers={"If-Match": etag}, json=preferences()
    )
    assert response.status_code == 422
    assert any(error["field"] == "If-Match" for error in response.json()["errors"])
    assert not transport.calls


def test_duplicate_revision_headers_are_rejected(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    response = profile_client.put(
        ME + "/onboarding", headers=[("If-Match", '"1"'), ("If-Match", '"2"')], json=preferences()
    )
    assert response.status_code == 422
    assert not transport.calls


@pytest.mark.parametrize("field", ["groceries", "learnedPreferences"])
@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_concurrent_explicit_and_independent_edits_cannot_erase_each_other(
    profile_client: TestClient,
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    field: str,
    method: str,
) -> None:
    transport, _ = harness
    completed = onboard(profile_client)
    independent: dict[str, Any] = {
        "groceries": [{"groceryId": "synthetic-row", "name": "synthetic beans"}],
        "learnedPreferences": [{"insight": "synthetic learned taste", "confidence": 0.8}],
    }
    transport.block_reads = 2
    transport.read_count = 0
    transport.barrier = asyncio.Event()

    async def scenario() -> None:
        path = ME + ("/onboarding" if method == "PUT" else "/preferences")
        body = {"likes": ["synthetic explicit taste"]}
        if method == "PUT":
            body = {**preferences(), **body}
        async with (
            cosmos.open_cosmos(profile_settings) as repository,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(profile_settings)),
                base_url="https://testserver",
                headers=auth_headers,
            ) as client,
        ):
            explicit, other = await asyncio.gather(
                client.request(
                    method,
                    path,
                    headers={"If-Match": completed.headers["etag"]},
                    json=body,
                ),
                repository.patch(
                    USER,
                    USERS,
                    USER.user_id,
                    RecordPatch(etag=completed.headers["etag"], fields={field: independent[field]}),
                ),
                return_exceptions=True,
            )
            assert isinstance(explicit, httpx.Response)
            if explicit.status_code == 412:
                assert not isinstance(other, BaseException)
                latest = await client.get(ME)
                retried = await client.request(
                    method,
                    path,
                    headers={"If-Match": latest.headers["etag"]},
                    json=body,
                )
                assert retried.status_code == 200
            else:
                assert explicit.status_code == 200
                assert isinstance(other, CosmosError) and other.code == CosmosFailure.CONFLICT
                await repository.patch(
                    USER,
                    USERS,
                    USER.user_id,
                    RecordPatch(etag=explicit.headers["etag"], fields={field: independent[field]}),
                )

    asyncio.run(scenario())
    stored = document(transport)
    assert stored[field] == independent[field]
    assert stored["explicitPreferences"]["likes"] == ["synthetic explicit taste"]
    assert stored["explicitPreferences"]["allergies"] == preferences()["allergies"]
    assert all(
        operation["path"] in {"/explicitPreferences", "/onboardingCompleted", "/updatedAt"}
        for call in transport.item_calls("PATCH")
        if any(operation["path"] == "/explicitPreferences" for operation in call.body["operations"])
        for operation in call.body["operations"]
    )


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_verified_identity_selects_partition_and_client_scope_is_rejected(
    profile_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    harness: tuple[Transport, SyntheticCredential],
    environment: Literal["local", "dev", "prod"],
) -> None:
    transport, _ = harness
    alice = AuthenticatedUser.from_verified_identity(AUTH_ISSUER, "alice")
    bob = AuthenticatedUser.from_verified_identity(AUTH_ISSUER, "bob")
    settings = profile_settings.model_copy(update={"environment": environment})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        for user, subject in ((bob, "bob"), (alice, "alice")):
            token = signed_token(signing_key, subject)
            client.headers.clear()
            if environment == "local":
                client.headers["Authorization"] = "Bearer " + token
            else:
                client.headers["X-MS-TOKEN-AAD-ID-TOKEN"] = token
                session = client.get("/auth/session")
                client.headers["Origin"] = "https://testserver"
                client.headers["X-CSRF-Token"] = session.json()["csrfToken"]
            client.headers["X-MS-CLIENT-PRINCIPAL-ID"] = bob.user_id
            client.headers["X-User-ID"] = bob.user_id
            completed = onboard(client)
            assert completed.json()["data"]["userId"] == user.user_id
            assert "email" not in document(transport, user)
        before = deepcopy(document(transport, bob))
        for suffix in ("", "/onboarding", "/preferences"):
            method = "GET" if not suffix else "PUT" if suffix == "/onboarding" else "PATCH"
            response = client.request(
                method,
                ME + suffix + "?userId=" + bob.user_id,
                headers={"If-Match": completed.headers["etag"]},
                json=None if method == "GET" else preferences(),
            )
            assert response.status_code == 422
            assert bob.user_id not in response.text
        assert client.get(ME + "/" + bob.user_id).status_code == 404
        updated = client.patch(
            ME + "/preferences",
            headers={"If-Match": completed.headers["etag"]},
            json={"likes": ["synthetic alice taste"]},
        )
        assert updated.status_code == 200
        assert document(transport, bob) == before
        assert document(transport, alice)["explicitPreferences"]["likes"] == [
            "synthetic alice taste"
        ]
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"])[0] in {alice.user_id, bob.user_id}
        for call in transport.item_calls()
    )


def test_hosted_mutation_requires_session_csrf_and_exact_origin(
    profile_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    with TestClient(
        create_app(profile_settings.model_copy(update={"environment": "dev"})),
        base_url="https://testserver",
        headers={"X-MS-TOKEN-AAD-ID-TOKEN": signed_token(signing_key)},
    ) as client:
        initial = client.get(ME)
        for headers in ({}, {"Origin": "https://other.invalid", "X-CSRF-Token": "forged"}):
            response = client.put(
                ME + "/onboarding",
                headers={**headers, "If-Match": initial.headers["etag"]},
                json=preferences(),
            )
            assert response.status_code == 403
            assert response.json()["code"] == "csrf_failed"
    assert not transport.item_calls("PATCH")


def test_profile_calls_invoke_no_models_and_write_no_learning_sources(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Profile operations must not invoke or construct model clients.")

    monkeypatch.setattr(foundry.FoundryModelClient, "__init__", forbidden)
    monkeypatch.setattr(foundry.FoundryModelClient, "invoke", forbidden)
    monkeypatch.setattr(foundry_gateway.FoundryGateway, "__init__", forbidden)
    monkeypatch.setattr(foundry_gateway.FoundryGateway, "invoke", forbidden)
    completed = onboard(profile_client)
    response = profile_client.patch(
        ME + "/preferences", headers={"If-Match": completed.headers["etag"]}, json={"likes": []}
    )
    assert response.status_code == 200
    assert all("/colls/Users/docs" in call.path for call in transport.item_calls())
    assert document(transport)["learnedPreferences"] == []
    assert len(transport.documents) == 1


@pytest.mark.parametrize(
    "corruption",
    [
        {"onboardingCompleted": True},
        {"onboardingCompleted": "private-corrupt-value"},
        {"explicitPreferences": {"allergies": []}},
        {"updatedAt": "private-corrupt-value"},
    ],
)
def test_invalid_stored_profiles_fail_without_reinitializing_or_leaking_values(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    corruption: dict[str, object],
) -> None:
    transport, _ = harness
    assert profile_client.get(ME).status_code == 200
    document(transport).update(corruption)
    response = profile_client.get(ME)
    assert response.status_code == 502
    assert response.json()["code"] == "cosmos_invalid_result"
    assert "private-corrupt-value" not in response.text
    assert len(transport.item_calls("POST")) == 1


def test_configuration_and_dependency_failures_never_look_like_empty_profiles(
    local_settings: Settings,
    auth_headers: dict[str, str],
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    with TestClient(create_app(local_settings), headers=auth_headers) as client:
        missing = client.get(ME)
    assert missing.status_code == 503
    assert missing.json()["code"] == "configuration_unavailable"
    transport.faults["GET"].extend([503] * 3)
    response = profile_client.get(ME)
    assert response.status_code == 503
    assert response.json()["code"] == "cosmos_unavailable"
    assert "secret-private" not in response.text
    assert not transport.documents


def test_projected_record_ceiling_counts_preserved_unrelated_state(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    completed = onboard(profile_client)
    stored = document(transport)
    stored["futurePrivateState"] = ""
    size = len(json.dumps({key: value for key, value in stored.items() if key != "_etag"}).encode())
    stored["futurePrivateState"] = "x" * (MAX_RECORD_BYTES - size - 5)
    writes = len(transport.item_calls("PATCH"))
    response = profile_client.patch(
        ME + "/preferences",
        headers={"If-Match": completed.headers["etag"]},
        json={"likes": ["a much longer explicit preference"]},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "record_too_large"
    assert len(transport.item_calls("PATCH")) == writes
    assert stored["explicitPreferences"] == preferences()
    assert "futurePrivateState" not in response.text


@pytest.mark.parametrize(
    ("body", "status"),
    [("{", 400), ("null", 422), ("[]", 422), ("{}", 422), ('{"likes": [null]}', 422)],
)
def test_malformed_and_empty_updates_are_safe(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    body: str,
    status: int,
) -> None:
    transport, _ = harness
    response = profile_client.patch(
        ME + "/preferences",
        headers={"Content-Type": "application/json", "If-Match": '"1"'},
        content=body,
    )
    assert response.status_code == status
    assert not transport.calls


def test_validation_error_projection_is_bounded(
    profile_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    response = profile_client.patch(
        ME + "/preferences",
        headers={"If-Match": '"1"'},
        json={"likes": [None] * 1000},
    )
    assert response.status_code == 422
    assert 1 <= len(response.json()["errors"]) <= 32
    assert len(response.content) < 4096
    assert not transport.calls


@pytest.mark.parametrize("declared", [False, True])
def test_oversized_request_is_rejected_before_storage(
    profile_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    declared: bool,
) -> None:
    transport, _ = harness
    headers = {"Content-Type": "application/json", "If-Match": '"1"'}
    if declared:
        headers["Content-Length"] = str(128 * 1024 + 1)
    response = profile_client.put(
        ME + "/onboarding",
        headers=headers,
        content=b"{}" if declared else b" " * (128 * 1024 + 1),
    )
    assert response.status_code == 413
    assert response.json()["code"] == "body_too_large"
    assert not transport.calls
