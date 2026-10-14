import hashlib
from collections.abc import Mapping
from dataclasses import dataclass


class OwnedResourceNotFound(Exception):
    """An absent resource and a resource owned by someone else are indistinguishable."""


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str

    @classmethod
    def from_verified_identity(cls, issuer: str, subject: str) -> "AuthenticatedUser":
        if not issuer or "\0" in issuer or not subject or "\0" in subject:
            raise ValueError("A verified issuer and subject are required.")
        return cls(hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest())

    def require_owned[T: Mapping[str, object]](self, resource: T | None) -> T:
        """Check a record fetched within this user's partition, before using its contents."""
        if resource is None or resource.get("userId") != self.user_id:
            raise OwnedResourceNotFound
        return resource
