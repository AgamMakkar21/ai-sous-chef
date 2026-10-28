from fastapi import APIRouter, Request, Response

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.revisions import (
    Revision,
    require_no_query,
    require_revision,
    set_revision_headers,
)
from sous_chef.domain.profiles import ExplicitPreferences, PreferenceUpdate, ProfileEnvelope
from sous_chef.services.profiles import ProfileService
from sous_chef.settings import Settings


def create_profile_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["profile"])
    service = ProfileService(settings)

    @router.get("")
    async def get(user: CurrentUser, request: Request, response: Response) -> ProfileEnvelope:
        require_no_query(request)
        profile, etag = await service.get(user)
        set_revision_headers(response, etag)
        return ProfileEnvelope(data=profile)

    @router.put("/onboarding")
    async def onboard(
        payload: ExplicitPreferences,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> ProfileEnvelope:
        revision = require_revision(request, if_match)
        profile, etag = await service.onboard(user, payload, revision)
        set_revision_headers(response, etag)
        return ProfileEnvelope(data=profile)

    @router.patch("/preferences")
    async def update(
        payload: PreferenceUpdate,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> ProfileEnvelope:
        revision = require_revision(request, if_match)
        profile, etag = await service.update(user, payload, revision)
        set_revision_headers(response, etag)
        return ProfileEnvelope(data=profile)

    return router
