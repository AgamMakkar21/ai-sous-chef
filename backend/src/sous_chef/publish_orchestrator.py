"""Explicit owner-operated dev publication; never activates the application."""

import argparse
import hashlib
import json
import re

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.domain.agent_identity import IdentityError
from sous_chef.domain.orchestrator import POLICY
from sous_chef.orchestrator_definition import ORCHESTRATOR_MODEL, build_orchestrator_definition
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry


def publish(settings: Settings, project: AIProjectClient) -> str:
    """Append a source version to an existing identity-bound agent, never recreate it."""
    if settings.environment != "dev" or not settings.agents_enabled:
        raise ConfigurationError("Source publication requires explicitly enabled dev settings.")
    if settings.app_workload_principal_id is None:
        raise ConfigurationError("Publication requires the App Service workload principal ID.")
    name = settings.foundry_orchestrator_agent_name
    deployment = settings.foundry_orchestrator_deployment
    if name is None or deployment is None:
        raise ConfigurationError("Configure the Orchestrator agent and model deployment names.")
    identities = resolve_agent_identities(settings, project.agents)
    if any(project.agents.get(agent.agent_name).state != "enabled" for agent in identities):
        raise IdentityError("Both existing agents must be enabled.")
    definition = build_orchestrator_definition(deployment)
    fingerprint = hashlib.sha256(
        json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    version = project.agents.create_version(
        agent_name=name,
        definition=definition,
        metadata={
            "asc_policy_version": str(POLICY.version),
            "asc_model": ORCHESTRATOR_MODEL,
            "asc_definition_sha256": fingerprint,
        },
    )
    if not isinstance(version.version, str) or not re.fullmatch(
        r"[1-9][0-9]{0,9}", version.version
    ):
        raise ConfigurationError("Foundry returned an invalid source version.")
    return version.version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-dev-publication", action="store_true", required=True)
    parser.parse_args()
    configure_safe_telemetry()
    try:
        settings = load_settings()
        if settings.environment != "dev" or settings.foundry_project_endpoint is None:
            raise ConfigurationError("Select the approved dev Foundry project.")
        with (
            AzureCliCredential() as credential,
            AIProjectClient(
                endpoint=str(settings.foundry_project_endpoint),
                credential=credential,
                api_version="v1",
                retry_total=0,
            ) as project,
        ):
            version = publish(settings, project)
    except (AzureError, ConfigurationError, IdentityError):
        raise SystemExit(
            "Orchestrator source publication failed. Check dev configuration, "
            "identity bindings and publisher permissions; no release was activated."
        ) from None
    print(f"Published Orchestrator policy {POLICY.version}, source version {version}.")


if __name__ == "__main__":
    main()
