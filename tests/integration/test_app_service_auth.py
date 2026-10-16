import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from fastapi.testclient import TestClient
from jwt import InvalidTokenError, PyJWKClientConnectionError
from sous_chef.adapters.app_service_auth import (
    MAX_IDENTITY_SIZE,
    AppServiceSessionVerifier,
    verified_session_identity,
)
from sous_chef.app import create_app
from sous_chef.settings import ConfigurationError, Settings

ORIGIN = "https://synthetic.azurewebsites.net"
SUBJECT = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier"
TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"


@pytest.fixture
def session_settings(local_settings: Settings) -> Settings:
    values = local_settings.model_dump()
    values.update(
        environment="dev",
        adapter_mode="live",
        auth_mode="app_service",
        auth_microsoft_tenant_id=TENANT,
        auth_issuer=None,
        auth_jwks_uri=None,
        auth_client_id=None,
        auth_origin=ORIGIN,
        cosmos_endpoint="https://cosmos.example.invalid/",
        cosmos_database="test",
        foundry_project_endpoint="https://foundry.example.invalid/api/projects/test",
        foundry_orchestrator_deployment="orchestrator",
        foundry_recipe_nutrition_deployment="recipe-nutrition",
        foundry_extraction_deployment="extraction",
    )
    return Settings(**values)


def identity(
    provider: str = "github", subject: str = "12345", tenant: str = TENANT
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = [
        {"typ": SUBJECT, "val": subject},
        {"typ": "email", "val": "same@example.invalid"},
    ]
    if provider == "aad":
        claims.extend(
            [
                {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
                {"typ": "iss", "val": f"https://login.microsoftonline.com/{tenant}/v2.0"},
            ]
        )
    return [
        {
            "provider_name": provider,
            "user_id": "not-used-for-ownership",
            "user_claims": claims,
        }
    ]


def test_provider_and_app_namespaces_are_separate() -> None:
    users = {
        verified_session_identity(identity(provider), ORIGIN, TENANT).user_id
        for provider in ("aad", "google", "github")
    }
    assert len(users) == 3
    assert (
        verified_session_identity(identity(), "https://other.azurewebsites.net").user_id
        not in users
    )
    assert verified_session_identity(identity(subject="999"), ORIGIN).user_id not in users
    first = verified_session_identity(identity(), ORIGIN)
    changed_email = identity()
    changed_email[0]["user_claims"] = [{"typ": SUBJECT, "val": "12345"}]
    assert verified_session_identity(changed_email, ORIGIN) == first


def test_microsoft_tenants_and_personal_accounts_have_separate_ownership() -> None:
    users = {
        verified_session_identity(identity("aad", tenant=tenant), ORIGIN, tenant).user_id
        for tenant in (TENANT, OTHER_TENANT, PERSONAL_TENANT)
    }
    assert len(users) == 3
    assert verified_session_identity(identity("google"), ORIGIN).user_id not in users


@pytest.mark.parametrize("tenant", [OTHER_TENANT, PERSONAL_TENANT])
def test_microsoft_rejects_unapproved_tenants(tenant: str) -> None:
    with pytest.raises(InvalidTokenError):
        verified_session_identity(identity("aad", tenant=tenant), ORIGIN, TENANT)


def test_microsoft_requires_an_explicit_approved_tenant() -> None:
    with pytest.raises(InvalidTokenError):
        verified_session_identity(identity("aad"), ORIGIN)


@pytest.mark.parametrize(
    "claims",
    [
        [{"typ": SUBJECT, "val": "same-user"}],
        [{"typ": "tid", "val": TENANT}],
        [
            {"typ": "tid", "val": "common"},
            {"typ": "iss", "val": "https://login.microsoftonline.com/common/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": "https://login.microsoftonline.com/common/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": f"https://attacker.invalid/{TENANT}/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": f"http://login.microsoftonline.com/{TENANT}/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": f"https://login.microsoftonline.com/{TENANT}/v2.0?x=1"},
        ],
        [{"typ": "tid", "val": []}, {"typ": "iss", "val": "invalid"}],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": OTHER_TENANT},
            {"typ": "iss", "val": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
        ],
        [
            {"typ": "tid", "val": TENANT},
            {"typ": "iss", "val": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
            {"typ": "iss", "val": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"},
        ],
    ],
)
def test_microsoft_session_requires_consistent_verified_tenant_and_issuer(
    claims: list[dict[str, object]],
) -> None:
    payload = [
        {"provider_name": "aad", "user_claims": [{"typ": SUBJECT, "val": "same-user"}, *claims]}
    ]
    with pytest.raises(InvalidTokenError):
        verified_session_identity(payload, ORIGIN, TENANT)


def test_microsoft_accepts_unmapped_tenant_and_subject_claims() -> None:
    payload = [
        {
            "provider_name": "aad",
            "user_claims": [
                {"typ": "sub", "val": "same-user"},
                {"typ": "tid", "val": TENANT},
                {"typ": "iss", "val": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
            ],
        }
    ]
    assert verified_session_identity(payload, ORIGIN, TENANT) == verified_session_identity(
        identity("aad", subject="same-user"), ORIGIN, TENANT
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        [None],
        identity() * 2,
        identity("facebook"),
        identity(subject=""),
        identity(subject="x" * 256),
        identity(subject="a\0b"),
        [{"provider_name": "google", "user_id": "email@example.invalid", "user_claims": []}],
        [{"provider_name": "github", "user_claims": [{"typ": "email", "val": "email"}]}],
        [{"provider_name": "aad", "user_claims": [None]}],
        [{"provider_name": "google", "user_claims": [{"typ": [], "val": "malformed"}]}],
        [
            {
                "provider_name": "aad",
                "user_claims": [
                    {"typ": SUBJECT, "val": "one"},
                    {"typ": "sub", "val": "different"},
                ],
            }
        ],
    ],
)
def test_unverified_or_ambiguous_identity_fails_closed(payload: object) -> None:
    with pytest.raises(InvalidTokenError):
        verified_session_identity(payload, ORIGIN)


@pytest.mark.parametrize(
    "provider, tenant",
    [
        ("aad", TENANT),
        ("google", TENANT),
        ("github", TENANT),
    ],
)
def test_provider_login_and_session(
    session_settings: Settings,
    provider: str,
    tenant: str,
) -> None:
    user = verified_session_identity(identity(provider, tenant=tenant), ORIGIN, TENANT)
    with (
        patch.object(
            AppServiceSessionVerifier, "verify", new_callable=AsyncMock, return_value=user
        ) as verify,
        TestClient(create_app(session_settings), base_url=ORIGIN) as client,
    ):
        selector = client.get("/auth/login")
        assert selector.status_code == 200
        assert "frame-ancestors 'none'" in selector.headers["content-security-policy"]
        for name in ("aad", "google", "github"):
            assert f'href="/auth/login/{name}"' in selector.text
        login = client.get(f"/auth/login/{provider}", follow_redirects=False)
        assert login.headers["location"] == f"/.auth/login/{provider}?post_login_redirect_uri=%2F"
        bad = client.get(f"/auth/login/{provider}?returnTo=https://attacker.invalid")
        assert bad.status_code == 400
        assert "location" not in bad.headers
        client.cookies.set("AppServiceAuthSession", "opaque-cookie")
        response = client.get(
            "/auth/session",
            headers={
                "Host": "attacker.invalid",
                "X-MS-CLIENT-PRINCIPAL-ID": "attacker",
            },
        )
        assert response.status_code == 200
        assert response.json()["userId"] == user.user_id
        assert set(response.json()) == {"userId", "csrfToken"}
        verify.assert_awaited_once_with("opaque-cookie")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-MS-CLIENT-PRINCIPAL-ID": "attacker"},
        {"X-MS-CLIENT-PRINCIPAL": "forged"},
        {"X-MS-TOKEN-AAD-ID-TOKEN": "forged"},
        {"Cookie": "AppServiceAuthSession=one; AppServiceAuthSession=two"},
        {"Cookie": "AppServiceAuthSession=one", "Authorization": "Bearer ignored"},
        {"Cookie": "AppServiceAuthSession=one", "Authorization": ""},
    ],
)
def test_headers_and_ambiguous_credentials_cannot_establish_session(
    session_settings: Settings,
    headers: dict[str, str],
) -> None:
    with (
        patch.object(AppServiceSessionVerifier, "verify", new_callable=AsyncMock) as verify,
        TestClient(create_app(session_settings), base_url=ORIGIN) as client,
    ):
        assert client.get("/auth/session", headers=headers).status_code == 401
        verify.assert_not_awaited()


def test_cookie_mutations_require_origin_and_csrf(session_settings: Settings) -> None:
    with (
        patch.object(
            AppServiceSessionVerifier,
            "verify",
            new_callable=AsyncMock,
            return_value=verified_session_identity(identity(), ORIGIN),
        ),
        TestClient(create_app(session_settings), base_url=ORIGIN) as client,
    ):
        client.cookies.set("AppServiceAuthSession", "opaque-cookie")
        response = client.get("/auth/session")
        assert client.post("/api/v1/not-implemented").status_code == 403
        headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrfToken"]}
        assert client.post("/api/v1/not-implemented", headers=headers).status_code == 404
        headers["Origin"] = "https://attacker.invalid"
        assert client.post("/api/v1/not-implemented", headers=headers).status_code == 403
        logout = client.get("/auth/logout", follow_redirects=False)
        assert "__Host-asc-csrf" in logout.headers["set-cookie"]
        assert "Max-Age=0" in logout.headers["set-cookie"]


@pytest.mark.parametrize(
    "failure, expected",
    [
        (InvalidTokenError("expired"), 401),
        (PyJWKClientConnectionError("private upstream details"), 503),
    ],
)
def test_session_failure_is_not_success(
    session_settings: Settings,
    failure: Exception,
    expected: int,
) -> None:
    with (
        patch.object(
            AppServiceSessionVerifier, "verify", new_callable=AsyncMock, side_effect=failure
        ),
        TestClient(create_app(session_settings), base_url=ORIGIN) as client,
    ):
        response = client.get("/auth/session", headers={"Cookie": "AppServiceAuthSession=opaque"})
        assert response.status_code == expected
        assert "private upstream details" not in response.text


@pytest.mark.parametrize(
    "origin",
    [
        "http://synthetic.azurewebsites.net",
        "https://attacker.invalid",
        "https://synthetic.azurewebsites.net.attacker.invalid",
        "https://synthetic.azurewebsites.net:444",
        ORIGIN + "/path",
        ORIGIN + "?query=one",
        "https://user@synthetic.azurewebsites.net",
    ],
)
def test_introspection_target_cannot_be_arbitrary(session_settings: Settings, origin: str) -> None:
    values = session_settings.model_dump() | {"auth_origin": origin}
    with pytest.raises(ConfigurationError):
        Settings(**values)


def test_mode_must_be_hosted_and_not_mix_external_settings(session_settings: Settings) -> None:
    for update in (
        {"environment": "local"},
        {"auth_origin": None},
        {"auth_microsoft_tenant_id": None},
        {"auth_client_id": "22222222-2222-2222-2222-222222222222"},
    ):
        with pytest.raises(ConfigurationError):
            Settings(**(session_settings.model_dump() | update))


@pytest.mark.parametrize(
    "status, body, error",
    [
        (200, json.dumps(identity()).encode(), None),
        (200, json.dumps(identity("google")).encode(), None),
        (200, json.dumps(identity("aad")).encode(), None),
        (200, json.dumps(identity("aad", tenant=OTHER_TENANT)).encode(), InvalidTokenError),
        (200, json.dumps(identity("aad", tenant=PERSONAL_TENANT)).encode(), InvalidTokenError),
        (200, b"[]", InvalidTokenError),
        (401, b"private", InvalidTokenError),
        (403, b"private", InvalidTokenError),
        (302, b"redirect", PyJWKClientConnectionError),
        (500, b"private", PyJWKClientConnectionError),
        (200, b"not-json", PyJWKClientConnectionError),
        (200, b"\xff", PyJWKClientConnectionError),
        (
            200,
            b"[" * 2000 + b"]" * 2000,
            (InvalidTokenError, PyJWKClientConnectionError),
        ),
        (200, b"x" * (MAX_IDENTITY_SIZE + 1), PyJWKClientConnectionError),
    ],
    ids=[
        "valid",
        "google",
        "approved-microsoft",
        "other-microsoft-tenant",
        "personal-microsoft",
        "empty",
        "unauthorized",
        "forbidden",
        "redirect",
        "upstream-error",
        "malformed-json",
        "invalid-encoding",
        "deep-json",
        "oversized",
    ],
)
def test_introspection_transport_is_fixed_and_bounded(
    session_settings: Settings,
    status: int,
    body: bytes,
    error: type[Exception] | tuple[type[Exception], ...] | None,
) -> None:
    async def chunks(size: int) -> AsyncIterator[bytes]:
        # Small chunks exercise reads that do not fill the requested buffer.
        for start in range(0, len(body), 11):
            yield body[start : start + 11]

    response = MagicMock(status=status)
    response.content.iter_chunked.side_effect = chunks
    response_context = AsyncMock()
    response_context.__aenter__.return_value = response
    client = MagicMock()
    client.get.return_value = response_context
    context = AsyncMock()
    context.__aenter__.return_value = client
    with patch(
        "sous_chef.adapters.app_service_auth.aiohttp.ClientSession", return_value=context
    ) as factory:
        verifier = AppServiceSessionVerifier(session_settings)
        if error is None:
            assert asyncio.run(verifier.verify("opaque")) == verified_session_identity(
                json.loads(body), ORIGIN, TENANT
            )
        else:
            with pytest.raises(error):
                asyncio.run(verifier.verify("opaque"))
        client.get.assert_called_once_with(
            ORIGIN + "/.auth/me",
            headers={"Cookie": "AppServiceAuthSession=opaque"},
            allow_redirects=False,
        )
        assert factory.call_args.kwargs["timeout"].total == 5
        assert factory.call_args.kwargs["trust_env"] is False
        assert isinstance(factory.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)


@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientConnectionError()])
def test_transport_outage_fails_closed(session_settings: Settings, failure: Exception) -> None:
    with patch("sous_chef.adapters.app_service_auth.aiohttp.ClientSession", side_effect=failure):
        with pytest.raises(PyJWKClientConnectionError):
            asyncio.run(AppServiceSessionVerifier(session_settings).verify("opaque"))


@pytest.mark.parametrize("cookie", ["", "a;b", "a,b", 'a"b', "a\\b", "a\r\nb", "x" * 16385])
def test_unsafe_cookie_is_never_forwarded(session_settings: Settings, cookie: str) -> None:
    with patch("sous_chef.adapters.app_service_auth.aiohttp.ClientSession") as factory:
        with pytest.raises(InvalidTokenError):
            asyncio.run(AppServiceSessionVerifier(session_settings).verify(cookie))
        factory.assert_not_called()
