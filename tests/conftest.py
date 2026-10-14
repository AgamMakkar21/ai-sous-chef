from collections.abc import Iterator
from time import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWKClient
from sous_chef.app import create_app
from sous_chef.settings import Settings

AUTH_ISSUER = "https://synthetic.ciamlogin.com/11111111-1111-1111-1111-111111111111/v2.0"
AUTH_AUDIENCE = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def local_settings(monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey) -> Settings:
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    public_key["kid"] = "synthetic"
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: {"keys": [public_key]})
    return Settings(
        environment="local",
        adapter_mode="local",
        _env_file=None,
        auth_issuer=AUTH_ISSUER,
        auth_jwks_uri="https://synthetic.ciamlogin.com/discovery/v2.0/keys",
        auth_client_id=AUTH_AUDIENCE,
        auth_origin="https://testserver",
    )


@pytest.fixture
def auth_headers(signing_key: rsa.RSAPrivateKey) -> dict[str, str]:
    now = int(time())
    token = jwt.encode(
        {
            "iss": AUTH_ISSUER,
            "aud": AUTH_AUDIENCE,
            "sub": "synthetic-user",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": "synthetic"},
    )
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def client(local_settings: Settings, auth_headers: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(create_app(local_settings), headers=auth_headers) as test_client:
        yield test_client
