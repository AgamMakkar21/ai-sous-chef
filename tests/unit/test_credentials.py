from typing import Literal

import pytest
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from sous_chef.adapters.credentials import build_credential
from sous_chef.settings import ConfigurationError, Settings


def make_settings(environment: Literal["local", "dev", "prod"]) -> Settings:
    return Settings.model_construct(environment=environment, adapter_mode="live")


def test_deployed_uses_managed_identity_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    credential = build_credential(make_settings("dev"))
    assert isinstance(credential, ManagedIdentityCredential)


def test_deployed_without_client_id_fails_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    with pytest.raises(ConfigurationError):
        build_credential(make_settings("dev"))


def test_local_uses_az_login_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    assert isinstance(build_credential(make_settings("local")), AzureCliCredential)
