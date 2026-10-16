from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.exceptions import RequestValidationError

from sous_chef.api.authentication import CurrentUser
from sous_chef.domain.profiles import ExplicitPreferences, PreferenceUpdate, ProfileEnvelope
from sous_chef.services.profiles import ProfileService
from sous_chef.settings import Settings

Revision = Annotated[
    str,
    Header(alias="If-Match", min_length=1, max_length=1024, pattern=r"^[^\x00-\x20\x7f]+$"),
]


def _no_scope(request: Request) -> None:
    if request.query_params:
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])


def _revision(request: Request, etag: str) -> str:
    _no_scope(request)
    if len(request.headers.getlist("if-match")) != 1 or etag == "*" or etag.startswith("W/"):
        raise RequestValidationError([{"type": "value_error", "loc": ("header", "If-Match")}])
    return etag


def _headers(response: Response, etag: str) -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"


def create_profile_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["profile"])
    service = ProfileService(settings)

    @router.get("")
    async def get(user: CurrentUser, request: Request, response: Response) -> ProfileEnvelope:
        _no_scope(request)
        profile, etag = await service.get(user)
        _headers(response, etag)
        return ProfileEnvelope(data=profile)

    @router.put("/onboarding")
    async def onboard(
        payload: ExplicitPreferences,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> ProfileEnvelope:
        revision = _revision(request, if_match)
        profile, etag = await service.onboard(user, payload, revision)
        _headers(response, etag)
        return ProfileEnvelope(data=profile)

    @router.patch("/preferences")
    async def update(
        payload: PreferenceUpdate,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> ProfileEnvelope:
        revision = _revision(request, if_match)
        profile, etag = await service.update(user, payload, revision)
        _headers(response, etag)
        return ProfileEnvelope(data=profile)

    return router
