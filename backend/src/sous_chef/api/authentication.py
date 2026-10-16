import secrets
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientConnectionError, PyJWKClientError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from sous_chef.adapters.app_service_auth import (
    PROVIDERS,
    SESSION_COOKIE,
    AppServiceSessionVerifier,
)
from sous_chef.api.problems import problem_response
from sous_chef.domain.authorization import AuthenticatedUser as AuthenticatedUser
from sous_chef.settings import Settings

_CSRF_COOKIE = "__Host-asc-csrf"


def get_authenticated_user(request: Request) -> AuthenticatedUser:
    user = getattr(request.state, "user", None)
    if not isinstance(user, AuthenticatedUser):
        raise HTTPException(status_code=401)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self.issuer = str(settings.auth_issuer) if settings.auth_issuer else None
        self.audience = settings.auth_client_id
        self.keys = (
            PyJWKClient(str(settings.auth_jwks_uri), timeout=5, lifespan=3600)
            if settings.auth_jwks_uri
            else None
        )

    def verify(self, token: str) -> AuthenticatedUser:
        if self.keys is None or self.issuer is None or self.audience is None:
            raise PyJWKClientConnectionError("Authentication is not configured.")
        # Reject invalid tokens before a key lookup; never follow a token-supplied key URL.
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise InvalidTokenError("Invalid signing header.")
        key = self.keys.get_signing_key_from_jwt(token)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf"], "strict_aud": True},
            )
        except (TypeError, OverflowError):
            # Signed but malformed NumericDate claims must not become server errors.
            raise InvalidTokenError("Invalid time claims.") from None
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or "\0" in subject or len(subject) > 255:
            raise InvalidTokenError("Invalid subject.")
        return AuthenticatedUser.from_verified_identity(self.issuer, subject)


class AuthenticationMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.verifier = TokenVerifier(settings)
        self.session_verifier = (
            AppServiceSessionVerifier(settings) if settings.auth_mode == "app_service" else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        public_paths = {"/health", "/ready", "/auth/login", "/auth/logout", "/auth/signed-out"}
        if self.settings.environment == "local":
            public_paths.update({"/docs", "/openapi.json"})
        if self.session_verifier is not None:
            public_paths.update(f"/auth/login/{provider}" for provider in PROVIDERS)
        if scope["path"] in public_paths:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        bearer = scheme.lower() == "bearer" and bool(separator)
        token = credential if bearer else ""
        if self.session_verifier is not None:
            cookies = [
                item.strip().partition("=")
                for raw in headers.getlist("cookie")
                for item in raw.split(";")
                if item.strip().partition("=")[0] == SESSION_COOKIE
            ]
            token = cookies[0][2] if len(cookies) == 1 and "authorization" not in headers else ""
        elif "authorization" not in headers and self.settings.environment != "local":
            token = headers.get("x-ms-token-aad-id-token", "")
        response: JSONResponse | None = None
        ambiguous_credentials = (
            len(headers.getlist("authorization")) > 1
            or len(headers.getlist("x-ms-token-aad-id-token")) > 1
        )
        if ambiguous_credentials or not token or len(token) > 16384:
            response = problem_response(
                401, "unauthenticated", "Authentication required", "Sign in again to continue."
            )
        else:
            try:
                user = (
                    await self.session_verifier.verify(token)
                    if self.session_verifier is not None
                    else await run_in_threadpool(self.verifier.verify, token)
                )
            except PyJWKClientConnectionError:
                response = problem_response(
                    503,
                    "authentication_unavailable",
                    "Authentication unavailable",
                    "Authentication could not be verified. Retry later.",
                )
            except (InvalidTokenError, PyJWKClientError):
                response = problem_response(
                    401, "unauthenticated", "Authentication required", "Sign in again to continue."
                )
            else:
                scope.setdefault("state", {})["user"] = user
                if not bearer and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                    request = Request(scope)
                    cookie = request.cookies.get(_CSRF_COOKIE, "")
                    csrf = headers.get("x-csrf-token", "")
                    origin = str(self.settings.auth_origin).rstrip("/")
                    if (
                        headers.get("origin") != origin
                        or not cookie
                        or not secrets.compare_digest(cookie.encode(), csrf.encode())
                    ):
                        response = problem_response(
                            403, "csrf_failed", "Forbidden", "Refresh the session and retry."
                        )
        if response is not None:
            if response.status_code == 401:
                response.headers["WWW-Authenticate"] = "Bearer"
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/auth")

    @router.get("/login", include_in_schema=False, response_model=None)
    async def login(request: Request) -> RedirectResponse | JSONResponse | HTMLResponse:
        if request.query_params:
            return problem_response(
                400, "invalid_input", "Invalid request", "Return URLs are fixed."
            )
        if settings.environment == "local":
            return problem_response(
                503,
                "authentication_unavailable",
                "Hosted login required",
                "Use the dev App Service login or a signed consumer token for local API testing.",
            )
        if settings.auth_mode == "app_service":
            links = "".join(
                f'<li><a href="/auth/login/{provider}">Sign in with {label}</a></li>'
                for provider, label in PROVIDERS.items()
            )
            return HTMLResponse(
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>Sign in - AI Sous Chef</title></head>"
                f"<body><h1>Sign in to AI Sous Chef</h1><ul>{links}</ul></body></html>",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'none'",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
        return RedirectResponse(
            "/.auth/login/aad?post_login_redirect_uri=%2F", headers={"Cache-Control": "no-store"}
        )

    @router.get("/login/{provider}", include_in_schema=False, response_model=None)
    async def provider_login(provider: str, request: Request) -> RedirectResponse | JSONResponse:
        if settings.auth_mode != "app_service" or provider not in PROVIDERS or request.query_params:
            return problem_response(
                400, "invalid_input", "Invalid request", "Choose a configured sign-in provider."
            )
        return RedirectResponse(
            f"/.auth/login/{provider}?post_login_redirect_uri=%2F",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/logout", include_in_schema=False, response_model=None)
    async def logout(request: Request) -> RedirectResponse | JSONResponse:
        if request.query_params:
            return problem_response(
                400, "invalid_input", "Invalid request", "Return URLs are fixed."
            )
        if settings.environment == "local":
            return problem_response(
                503,
                "authentication_unavailable",
                "Hosted logout required",
                "Discard the local bearer token and sign out through the dev App Service.",
            )
        response = RedirectResponse(
            "/.auth/logout?post_logout_redirect_uri=%2Fauth%2Fsigned-out",
            headers={"Cache-Control": "no-store"},
        )
        response.delete_cookie(_CSRF_COOKIE, secure=True, httponly=True, samesite="strict")
        return response

    @router.get("/signed-out")
    async def signed_out() -> JSONResponse:
        return JSONResponse(
            {"status": "signed_out", "loginUrl": "/auth/login"},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/session")
    async def session(user: CurrentUser) -> JSONResponse:
        csrf = secrets.token_urlsafe(32)
        response = JSONResponse(
            {"userId": user.user_id, "csrfToken": csrf}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            _CSRF_COOKIE, csrf, secure=True, httponly=True, samesite="strict", path="/"
        )
        return response

    return router
