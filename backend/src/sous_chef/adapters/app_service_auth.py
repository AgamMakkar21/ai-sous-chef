"""Validate an opaque session at the configured App Service, never via caller identity headers."""

import json
import re

import aiohttp
from jwt import InvalidTokenError, PyJWKClientConnectionError

from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.settings import ConfigurationError, Settings

PROVIDERS = {"aad": "Microsoft", "google": "Google", "github": "GitHub"}
SESSION_COOKIE = "AppServiceAuthSession"
MAX_SESSION_SIZE = 16384
MAX_IDENTITY_SIZE = 65536


def _unique_claim(claims: list[dict[str, object]], names: tuple[str, ...]) -> str:
    values = [claim.get("val") for claim in claims if claim.get("typ") in names]
    if not values or any(value != values[0] for value in values):
        raise InvalidTokenError("App Service must supply unambiguous identity claims.")
    value = values[0]
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or any(ord(character) < 32 for character in value)
    ):
        raise InvalidTokenError("Invalid App Service identity claim.")
    return value


def verified_session_identity(
    payload: object, origin: str, microsoft_tenant: str | None = None
) -> AuthenticatedUser:
    if not isinstance(payload, list) or len(payload) != 1:
        raise InvalidTokenError("Invalid App Service session.")
    identity = payload[0]
    if not isinstance(identity, dict):
        raise InvalidTokenError("Invalid App Service session.")
    provider = identity.get("provider_name")
    claims = identity.get("user_claims")
    if not isinstance(claims, list) or any(not isinstance(claim, dict) for claim in claims):
        raise InvalidTokenError("Invalid App Service claims.")
    subject = _unique_claim(
        claims, ("sub", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier")
    )
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise InvalidTokenError("Invalid App Service identity.")
    namespace = f"{origin}/.auth/{provider}"
    if provider == "aad":
        tenant = _unique_claim(
            claims, ("tid", "http://schemas.microsoft.com/identity/claims/tenantid")
        ).lower()
        issuer = _unique_claim(claims, ("iss",))
        if (
            not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", tenant)
            or microsoft_tenant is None
            or tenant != microsoft_tenant.lower()
            or issuer != f"https://login.microsoftonline.com/{tenant}/v2.0"
        ):
            raise InvalidTokenError("Invalid Microsoft tenant or issuer.")
        namespace = f"{namespace}/{tenant}"
    return AuthenticatedUser.from_verified_identity(namespace, subject)


class AppServiceSessionVerifier:
    def __init__(self, settings: Settings) -> None:
        if (
            settings.auth_mode != "app_service"
            or settings.auth_origin is None
            or settings.auth_microsoft_tenant_id is None
        ):
            raise ConfigurationError("Configure hosted App Service authentication explicitly.")
        self._origin = str(settings.auth_origin).rstrip("/")
        self._microsoft_tenant = settings.auth_microsoft_tenant_id

    async def verify(self, session: str) -> AuthenticatedUser:
        if (
            not session
            or len(session) > MAX_SESSION_SIZE
            or any(ord(character) < 33 or ord(character) > 126 for character in session)
            or any(character in session for character in ';,"\\')
        ):
            raise InvalidTokenError("Invalid App Service session.")
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            ) as client:
                async with client.get(
                    f"{self._origin}/.auth/me",
                    headers={"Cookie": f"{SESSION_COOKIE}={session}"},
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise InvalidTokenError("App Service session was rejected.")
                    if response.status != 200:
                        raise PyJWKClientConnectionError("App Service session service unavailable.")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) > MAX_IDENTITY_SIZE:
                            raise PyJWKClientConnectionError(
                                "Invalid App Service identity response."
                            )
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise PyJWKClientConnectionError("Invalid App Service identity response.") from None
            return verified_session_identity(payload, self._origin, self._microsoft_tenant)
        except (aiohttp.ClientError, TimeoutError):
            raise PyJWKClientConnectionError(
                "App Service session verification unavailable."
            ) from None
