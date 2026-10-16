"""One current profile per trusted user; no model or learning-pipeline calls."""

import json
from uuid import uuid4

from pydantic import ConfigDict, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.profiles import (
    ExplicitPreferences,
    PreferenceUpdate,
    ProfileView,
    require_onboarding,
)
from sous_chef.settings import Settings

USERS = ProductContainer.USERS


class _StoredProfile(ProfileView):
    model_config = ConfigDict(extra="ignore")


def _view(record: StoredRecord) -> ProfileView:
    try:
        stored = _StoredProfile.model_validate_json(json.dumps(record.body))
        return ProfileView.model_validate(stored.model_dump(by_alias=True))
    except ValidationError:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None


async def _read(repository: CosmosRepository, user: AuthenticatedUser) -> StoredRecord:
    record = await repository.read(user, USERS, user.user_id)
    if record is None:
        raise OwnedResourceNotFound
    return record


class ProfileService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get(self, user: AuthenticatedUser) -> tuple[ProfileView, str]:
        async with open_cosmos(self.settings) as repository:
            record = await repository.read(user, USERS, user.user_id)
            if record is None:
                try:
                    record = await repository.create(
                        user,
                        NewRecord(
                            kind=RecordKind.PROFILE,
                            fields={
                                "onboardingCompleted": False,
                                "explicitPreferences": None,
                                "groceries": [],
                                "learnedPreferences": [],
                            },
                        ),
                    )
                except CosmosError as error:
                    if error.code != CosmosFailure.EXISTS:
                        raise
                    record = await repository.read(user, USERS, user.user_id)
                    if record is None:
                        raise
            return _view(record), record.etag

    async def onboard(
        self, user: AuthenticatedUser, preferences: ExplicitPreferences, etag: str
    ) -> tuple[ProfileView, str]:
        async with open_cosmos(self.settings) as repository:
            _view(await _read(repository, user))
            updated = await repository.patch(
                user,
                USERS,
                user.user_id,
                RecordPatch(
                    etag=etag,
                    fields={
                        "onboardingCompleted": True,
                        "explicitPreferences": preferences.model_dump(mode="json", by_alias=True),
                    },
                ),
            )
            return _view(updated), updated.etag

    async def update(
        self, user: AuthenticatedUser, request: PreferenceUpdate, etag: str
    ) -> tuple[ProfileView, str]:
        async with open_cosmos(self.settings) as repository:
            record = await _read(repository, user)
            profile = _view(record)
            preferences = require_onboarding(profile)
            if record.etag != etag:
                raise CosmosError(CosmosFailure.CONFLICT, uuid4())
            fields = {
                **preferences.model_dump(mode="json", by_alias=True),
                **request.model_dump(mode="json", by_alias=True, exclude_unset=True),
            }
            complete = ExplicitPreferences.model_validate(fields)
            updated = await repository.patch(
                user,
                USERS,
                user.user_id,
                RecordPatch(
                    etag=etag,
                    fields={"explicitPreferences": complete.model_dump(mode="json", by_alias=True)},
                ),
            )
            return _view(updated), updated.etag
