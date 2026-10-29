"""Explicit owner-operated dev publication; never activates the application."""

import argparse
import hashlib
import json
import re

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.orchestrator import POLICY
from sous_chef.orchestrator_definition import ORCHESTRATOR_MODEL, build_orchestrator_definition
from sous_chef.recipe_definition import (
    RECIPE_MODEL,
    RECIPE_POLICY_VERSION,
    build_recipe_definition,
    recipe_knowledge_tool,
)
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry


def publish(settings: Settings, project: AIProjectClient) -> str:
    """Append a source version to an existing identity-bound agent, never recreate it."""
    return publish_agent(settings, project, AgentRole.ORCHESTRATOR)


def publish_agent(settings: Settings, project: AIProjectClient, role: AgentRole) -> str:
    if settings.environment != "dev" or not settings.agents_enabled:
        raise ConfigurationError("Source publication requires explicitly enabled dev settings.")
    if settings.app_workload_principal_id is None:
        raise ConfigurationError("Publication requires the App Service workload principal ID.")
    if role == AgentRole.ORCHESTRATOR:
        name = settings.foundry_orchestrator_agent_name
        deployment = settings.foundry_orchestrator_deployment
        model = ORCHESTRATOR_MODEL
        policy_version = POLICY.version
    else:
        name = settings.foundry_recipe_nutrition_agent_name
        deployment = settings.foundry_recipe_nutrition_deployment
        model = RECIPE_MODEL
        policy_version = RECIPE_POLICY_VERSION
    if name is None or deployment is None:
        raise ConfigurationError("Configure the agent and model deployment names.")
    if (
        role == AgentRole.RECIPE_NUTRITION
        and settings.foundry_recipe_nutrition_rai_policy_name is None
    ):
        raise ConfigurationError(
            "Set ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME to the approved baseline policy."
        )
    identities = resolve_agent_identities(settings, project.agents)
    if any(project.agents.get(agent.agent_name).state != "enabled" for agent in identities):
        raise IdentityError("Both existing agents must be enabled.")
    if role == AgentRole.ORCHESTRATOR:
        definition = build_orchestrator_definition(deployment)
    else:
        assert settings.foundry_recipe_nutrition_rai_policy_name is not None
        knowledge = RecipeKnowledgeConfig.from_settings(settings)
        definition = build_recipe_definition(
            deployment,
            settings.foundry_recipe_nutrition_rai_policy_name,
            knowledge_tool=recipe_knowledge_tool(knowledge.mcp_endpoint, knowledge.connection_name),
        )
    fingerprint = hashlib.sha256(
        json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    version = project.agents.create_version(
        agent_name=name,
        definition=definition,
        metadata={
            "asc_policy_version": str(policy_version),
            "asc_model": model,
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
    parser.add_argument(
        "--agent", choices=[role.value for role in AgentRole], default="orchestrator"
    )
    args = parser.parse_args()
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
            version = publish_agent(settings, project, AgentRole(args.agent))
    except (AzureError, ConfigurationError, IdentityError):
        raise SystemExit(
            "Agent source publication failed. Check dev configuration, "
            "identity bindings and publisher permissions; no release was activated."
        ) from None
    print(f"Published {args.agent} source version {version}.")


if __name__ == "__main__":
    main()
