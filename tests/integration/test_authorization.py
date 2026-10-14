from typing import Literal
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sous_chef.api.authentication import CurrentUser
from sous_chef.api.problems import register_problem_handlers
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.settings import Settings
from test_authentication import AUDIENCE, ISSUER, hosted_settings, signed_token


class SyntheticPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_shared_boundary_scopes_reads_and_mutations(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    environment: Literal["local", "dev", "prod"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = (
        local_settings if environment == "local" else hosted_settings(local_settings, environment)
    )
    alice = AuthenticatedUser.from_verified_identity(ISSUER, "alice")
    bob = AuthenticatedUser.from_verified_identity(ISSUER, "bob")
    shared_id, bob_only_id, corrupt_id = (str(uuid4()) for _ in range(3))
    records: dict[tuple[str, str], dict[str, object]] = {
        (alice.user_id, shared_id): {
            "id": shared_id,
            "userId": alice.user_id,
            "value": "alice-private",
        },
        (bob.user_id, shared_id): {
            "id": shared_id,
            "userId": bob.user_id,
            "value": "bob-private",
        },
        (bob.user_id, bob_only_id): {
            "id": bob_only_id,
            "userId": bob.user_id,
            "value": "bob-only-private",
        },
        (alice.user_id, corrupt_id): {
            "id": corrupt_id,
            "userId": bob.user_id,
            "value": "corrupt-private",
        },
    }
    lookups: list[tuple[str, str]] = []
    app = create_app(settings)

    def read_owned(user: AuthenticatedUser, resource_id: str) -> dict[str, object]:
        # This fixture models a point read with the server-selected Cosmos partition.
        key = (user.user_id, resource_id)
        lookups.append(key)
        return user.require_owned(records.get(key))

    @app.get("/owned/{resource_id}")
    async def read(resource_id: str, user: CurrentUser) -> dict[str, object]:
        record = read_owned(user, resource_id)
        return {"data": {"id": record["id"], "value": record["value"]}}

    @app.patch("/owned/{resource_id}")
    async def update(
        resource_id: str, patch: SyntheticPatch, user: CurrentUser
    ) -> dict[str, object]:
        record = read_owned(user, resource_id)
        record["value"] = patch.value
        return {"data": {"id": record["id"], "value": record["value"]}}

    with TestClient(app, base_url="https://testserver") as client:
        token = signed_token(signing_key, sub="alice")
        if environment == "local":
            client.headers["Authorization"] = "Bearer " + token
        else:
            client.headers["X-MS-TOKEN-AAD-ID-TOKEN"] = token
            session = client.get("/auth/session")
            assert session.status_code == 200
            client.headers["Origin"] = "https://testserver"
            client.headers["X-CSRF-Token"] = session.json()["csrfToken"]
        client.headers["X-MS-CLIENT-PRINCIPAL-ID"] = bob.user_id
        client.headers["X-MS-CLIENT-PRINCIPAL"] = "forged-principal"
        client.headers["X-User-ID"] = bob.user_id
        response = client.get(f"/owned/{shared_id}?userId={bob.user_id}")
        assert response.status_code == 200
        assert response.json()["data"]["value"] == "alice-private"
        response = client.patch(f"/owned/{shared_id}", json={"value": "alice-updated"})
        assert response.status_code == 200
        assert records[(alice.user_id, shared_id)]["value"] == "alice-updated"
        assert records[(bob.user_id, shared_id)]["value"] == "bob-private"
        response = client.patch(
            f"/owned/{shared_id}", json={"userId": bob.user_id, "value": "spoofed"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_input"
        assert bob.user_id not in response.text
        for resource_id in (bob_only_id, corrupt_id, str(uuid4())):
            for method in ("GET", "PATCH"):
                response = client.request(
                    method,
                    f"/owned/{resource_id}",
                    json={"value": "stolen"} if method == "PATCH" else None,
                )
                assert response.status_code == 404
                assert response.headers["content-type"] == "application/problem+json"
                assert response.headers["cache-control"] == "no-store"
                assert response.json() == {
                    "type": "urn:ai-sous-chef:problem:not-found",
                    "title": "Not found",
                    "status": 404,
                    "detail": "The resource was not found.",
                    "code": "not_found",
                    "traceId": response.headers["x-trace-id"],
                    "errors": [],
                }
        assert records[(bob.user_id, bob_only_id)]["value"] == "bob-only-private"
    assert lookups and all(key[0] == alice.user_id for key in lookups)
    for private in ("alice-private", "bob-private", "bob-only-private", "corrupt-private"):
        assert private not in caplog.text
    assert "not_found" in caplog.text


def test_dependency_fails_closed_without_authentication_middleware() -> None:
    app = FastAPI()
    register_problem_handlers(app)

    @app.get("/protected")
    async def protected(user: CurrentUser) -> dict[str, str]:
        return {"userId": user.user_id}

    with TestClient(app) as client:
        response = client.get("/protected", headers={"X-User-ID": "forged"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


def test_session_exposes_only_opaque_identity_and_csrf(
    local_settings: Settings, auth_headers: dict[str, str]
) -> None:
    with TestClient(create_app(local_settings)) as client:
        response = client.get("/auth/session", headers=auth_headers)
    assert response.status_code == 200
    assert set(response.json()) == {"userId", "csrfToken"}
    assert (
        response.json()["userId"]
        == AuthenticatedUser.from_verified_identity(ISSUER, "synthetic-user").user_id
    )
    assert response.headers["cache-control"] == "no-store"
    for sensitive in (ISSUER, AUDIENCE, auth_headers["Authorization"], "synthetic-user"):
        assert sensitive not in response.text
