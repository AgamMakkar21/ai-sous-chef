import hashlib
from dataclasses import FrozenInstanceError

import pytest
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound


def test_identity_mapping_is_stable_opaque_and_issuer_scoped() -> None:
    user = AuthenticatedUser.from_verified_identity("https://issuer.example", "alice")
    assert user == AuthenticatedUser.from_verified_identity("https://issuer.example", "alice")
    assert user.user_id == hashlib.sha256(b"https://issuer.example\0alice").hexdigest()
    assert user != AuthenticatedUser.from_verified_identity("https://other.example", "alice")
    assert user != AuthenticatedUser.from_verified_identity("https://issuer.example", "bob")
    with pytest.raises(FrozenInstanceError):
        user.__setattr__("user_id", "bob")


@pytest.mark.parametrize(
    ("issuer", "subject"),
    [("", "alice"), ("issuer", ""), ("issuer\0other", "alice"), ("issuer", "alice\0bob")],
)
def test_invalid_identity_mapping_fails(issuer: str, subject: str) -> None:
    with pytest.raises(ValueError, match="verified issuer and subject"):
        AuthenticatedUser.from_verified_identity(issuer, subject)


def test_ownership_guard_preserves_the_owned_record() -> None:
    user = AuthenticatedUser.from_verified_identity("issuer", "alice")
    record = {"id": "opaque-id", "userId": user.user_id, "private": "synthetic"}
    assert user.require_owned(record) is record


@pytest.mark.parametrize(
    "record",
    [None, {}, {"userId": None}, {"userId": "another-user", "private": "synthetic-private"}],
)
def test_missing_foreign_or_ownerless_records_fail_without_details(
    record: dict[str, object] | None,
) -> None:
    user = AuthenticatedUser.from_verified_identity("issuer", "alice")
    with pytest.raises(OwnedResourceNotFound) as exc:
        user.require_owned(record)
    assert not str(exc.value)
