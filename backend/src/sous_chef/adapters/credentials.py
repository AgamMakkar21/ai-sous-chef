import os

from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential

from sous_chef.settings import ConfigurationError, Settings

CLIENT_ID_VARIABLE = "AZURE_CLIENT_ID"


def build_credential(settings: Settings) -> TokenCredential:
    """Pick exactly one credential type for the environment, with no fallback chain.

    Deployed (dev/prod): only the App Service's user-assigned managed identity.
    Local: only the developer's explicit `az login`.
    """
    if settings.environment == "local":
        return AzureCliCredential()
    return ManagedIdentityCredential(client_id=_managed_identity_client_id())


def build_async_credential(settings: Settings) -> AsyncTokenCredential:
    """Use the same single-credential policy for cancellable async SDK calls."""
    if settings.environment == "local":
        return AsyncAzureCliCredential()
    return AsyncManagedIdentityCredential(client_id=_managed_identity_client_id())


def _managed_identity_client_id() -> str:
    client_id = os.environ.get(CLIENT_ID_VARIABLE)
    if not client_id:
        raise ConfigurationError(
            f"Set {CLIENT_ID_VARIABLE} to the app's managed identity client ID for dev and prod."
        )
    return client_id
