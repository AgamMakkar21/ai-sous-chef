from time import time
from typing import Any, Literal

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.testclient import TestClient
from jwt import PyJWKClient, PyJWKClientConnectionError
from sous_chef.api.authentication import AuthenticatedUser
from sous_chef.app import create_app
from sous_chef.settings import ConfigurationError, Settings

ISSUER = "https://synthetic.ciamlogin.com/11111111-1111-1111-1111-111111111111/v2.0"
AUDIENCE = "22222222-2222-2222-2222-222222222222"


def signed_token(key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    now = int(time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "synthetic-user",
        "exp": now + 300,
        "iat": now,
        "nbf": now,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "synthetic"})


def hosted_settings(settings: Settings, environment: Literal["dev", "prod"] = "dev") -> Settings:
    # Tests supply the existing live-adapter fields without contacting those services.
    values = settings.model_dump()
    values.update(
        environment=environment,
        adapter_mode="live",
        cosmos_endpoint="https://cosmos.example.invalid/",
        cosmos_database="test",
        foundry_project_endpoint="https://foundry.example.invalid/api/projects/test",
        foundry_orchestrator_deployment="orchestrator",
        foundry_recipe_nutrition_deployment="recipe-nutrition",
        foundry_extraction_deployment="extraction",
    )
    return Settings(**values)


@pytest.mark.parametrize("path", ["/", "/assets/app.js", "/api/v1/me", "/auth/session", "/unknown"])
def test_anonymous_routes_fail_closed(local_settings: Settings, path: str) -> None:
    with TestClient(create_app(local_settings)) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert response.json()["traceId"] == response.headers["x-trace-id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["www-authenticate"] == "Bearer"
    assert "location" not in response.headers


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_injected_identity_headers_are_not_credentials(
    local_settings: Settings,
    environment: Literal["local", "dev", "prod"],
) -> None:
    settings = (
        local_settings if environment == "local" else hosted_settings(local_settings, environment)
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/auth/session",
            headers={
                "X-MS-CLIENT-PRINCIPAL-ID": "attacker",
                "X-MS-CLIENT-PRINCIPAL": "eyJ1c2VySWQiOiJhdHRhY2tlciJ9",
                "X-User-ID": "attacker",
            },
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://login.microsoftonline.com/common/v2.0"},
        {"aud": "unapproved-app"},
        {"aud": [AUDIENCE, "another-app"]},
        {"exp": int(time()) - 10},
        {"nbf": int(time()) + 3600},
        {"iat": int(time()) + 3600},
        {"sub": ""},
        {"sub": 123},
        {"sub": "x" * 256},
        {"sub": "alice\0bob"},
        {"exp": []},
        {"iat": {}},
        {"nbf": float("inf")},
    ],
)
def test_invalid_claims_fail(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
) -> None:
    with TestClient(create_app(local_settings)) as client:
        response = client.get(
            "/auth/session",
            headers={
                "Authorization": "Bearer " + signed_token(signing_key, **claims),
            },
        )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_missing_expiry_and_wrong_signature_fail(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    claims = jwt.decode(signed_token(signing_key), options={"verify_signature": False})
    del claims["exp"]
    missing_exp = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "synthetic"})
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_signature = signed_token(other_key)
    hs256 = jwt.encode(claims, "synthetic-secret-not-a-real-credential" * 2, algorithm="HS256")
    with TestClient(create_app(local_settings)) as client:
        for token in (missing_exp, wrong_signature, hs256, "not-a-jwt"):
            assert (
                client.get(
                    "/auth/session",
                    headers={
                        "Authorization": "Bearer " + token,
                    },
                ).status_code
                == 401
            )


def test_user_scope_is_server_derived_and_isolated(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    app = create_app(local_settings)

    @app.get("/scope")
    async def scope(request: Request) -> dict[str, str]:
        user: AuthenticatedUser = request.state.user
        return {"userId": user.user_id}

    with TestClient(app) as client:
        users = [
            client.get(
                "/scope?userId=attacker",
                headers={
                    "Authorization": "Bearer " + signed_token(signing_key, sub=subject),
                    "X-User-ID": "attacker",
                },
            ).json()["userId"]
            for subject in ("alice", "bob", "alice")
        ]
    assert users[0] == users[2]
    assert users[0] != users[1]
    assert all(len(user) == 64 and user != "attacker" for user in users)


def test_key_service_failure_is_not_a_success_or_invalid_login(
    local_settings: Settings,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unavailable(self: PyJWKClient) -> None:
        raise PyJWKClientConnectionError("private-provider-detail")

    monkeypatch.setattr(PyJWKClient, "fetch_data", unavailable)
    with TestClient(create_app(local_settings)) as client:
        response = client.get("/auth/session", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "authentication_unavailable"
    assert "authentication_unavailable" in caplog.text
    assert "private-provider-detail" not in response.text + caplog.text
    assert auth_headers["Authorization"] not in caplog.text


def test_cookie_session_and_csrf(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    app = create_app(hosted_settings(local_settings))

    @app.post("/mutation")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as client:
        client.headers["X-MS-TOKEN-AAD-ID-TOKEN"] = signed_token(signing_key)
        session = client.get("/auth/session")
        assert session.status_code == 200
        assert session.headers["cache-control"] == "no-store"
        assert "HttpOnly" in session.headers["set-cookie"]
        assert "Secure" in session.headers["set-cookie"]
        assert "SameSite=strict" in session.headers["set-cookie"]
        assert client.post("/mutation").status_code == 403
        csrf = session.json()["csrfToken"]
        headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}
        assert client.post("/mutation", headers=headers).status_code == 200
        headers["Origin"] = "https://attacker.invalid"
        assert client.post("/mutation", headers=headers).status_code == 403
        client.headers["X-MS-TOKEN-AAD-ID-TOKEN"] = signed_token(
            signing_key,
            exp=int(time()) - 10,
        )
        assert client.get("/auth/session").status_code == 401


def test_local_testing_requires_bearer_not_platform_headers(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    with TestClient(create_app(local_settings)) as client:
        assert (
            client.get(
                "/auth/session",
                headers={
                    "X-MS-TOKEN-AAD-ID-TOKEN": signed_token(signing_key),
                },
            ).status_code
            == 401
        )
        assert client.get("/auth/login").status_code == 503


def test_hosted_login_logout_and_redirect_rejection(local_settings: Settings) -> None:
    with TestClient(create_app(hosted_settings(local_settings))) as client:
        login = client.get("/auth/login", follow_redirects=False)
        assert login.headers["location"] == "/.auth/login/aad?post_login_redirect_uri=%2F"
        logout = client.get("/auth/logout", follow_redirects=False)
        assert logout.headers["location"] == (
            "/.auth/logout?post_logout_redirect_uri=%2Fauth%2Fsigned-out"
        )
        assert client.get("/auth/signed-out").json()["loginUrl"] == "/auth/login"
        for path in ("/auth/login", "/auth/logout"):
            response = client.get(path + "?returnTo=https://attacker.invalid")
            assert response.status_code == 400
            assert "location" not in response.headers
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


@pytest.mark.parametrize(
    "issuer",
    [
        "https://login.microsoftonline.com/common/v2.0",
        "http://synthetic.ciamlogin.com/tenant/v2.0",
        "https://synthetic.ciamlogin.com.attacker.invalid/v2.0",
        "https://synthetic.ciamlogin.com:444/tenant/v2.0",
        "https://synthetic.ciamlogin.com/tenant/v2.0?secret=private",
    ],
)
def test_only_explicit_consumer_issuer_is_supported(
    local_settings: Settings,
    issuer: str,
) -> None:
    values = local_settings.model_dump()
    values["auth_issuer"] = issuer
    with pytest.raises(ConfigurationError, match="ciamlogin.com"):
        Settings(**values)


def test_partial_auth_configuration_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="ASC_AUTH"):
        Settings(environment="local", adapter_mode="local", auth_issuer=ISSUER)


def test_unconfigured_local_app_has_no_bypass() -> None:
    with TestClient(create_app(Settings(environment="local", adapter_mode="local"))) as client:
        assert client.get("/api/v1/me").status_code == 401
        assert client.get("/health").status_code == 200


def test_bearer_scheme_is_case_insensitive(
    local_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    with TestClient(create_app(local_settings)) as client:
        response = client.get(
            "/auth/session",
            headers={"Authorization": auth_headers["Authorization"].replace("Bearer ", "bearer ")},
        )
    assert response.status_code == 200


@pytest.mark.parametrize("authorization", ["", "Basic invalid", "Bearer invalid"])
def test_invalid_explicit_credentials_never_fall_back_to_platform_session(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    authorization: str,
) -> None:
    with TestClient(create_app(hosted_settings(local_settings))) as client:
        response = client.get(
            "/auth/session",
            headers={
                "Authorization": authorization,
                "X-MS-TOKEN-AAD-ID-TOKEN": signed_token(signing_key),
            },
        )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("header", ["Authorization", "X-MS-TOKEN-AAD-ID-TOKEN"])
def test_duplicate_credentials_are_rejected(
    local_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    header: str,
) -> None:
    token = signed_token(signing_key)
    credential = "Bearer " + token if header == "Authorization" else token
    with TestClient(create_app(hosted_settings(local_settings))) as client:
        response = client.get("/auth/session", headers=[(header, credential), (header, credential)])
    assert response.status_code == 401


def test_bearer_mutation_does_not_require_ambient_cookie_credentials(
    local_settings: Settings, auth_headers: dict[str, str]
) -> None:
    app = create_app(hosted_settings(local_settings))

    @app.post("/mutation")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/mutation", headers=auth_headers)
        assert response.status_code == 200
        preflight = client.options(
            "/mutation",
            headers={
                "Origin": "https://attacker.invalid",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers
