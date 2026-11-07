"""Explicit owner-operated dev publication; never activates the application."""

import argparse
import re
from dataclasses import dataclass

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig, validated_project_resource_id
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.orchestrator import POLICY
from sous_chef.orchestrator_definition import ORCHESTRATOR_MODEL, build_orchestrator_definition
from sous_chef.recipe_definition import (
    RECIPE_MODEL,
    RECIPE_POLICY_VERSION,
    RecipePhase,
    build_recipe_definition,
    definition_fingerprint,
    recipe_knowledge_tool,
    recipe_source_metadata,
)
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry


def publish(settings: Settings, project: AIProjectClient) -> str:
    """Append a source version to an existing identity-bound agent, never recreate it."""
    return publish_agent(settings, project, AgentRole.ORCHESTRATOR)


@dataclass(frozen=True)
class AgentSource:
    name: str
    definition: PromptAgentDefinition
    metadata: dict[str, str]


def prepare_agent_source(
    settings: Settings, role: AgentRole, *, phase: RecipePhase = RecipePhase.GENERATION
) -> AgentSource:
    if settings.environment != "dev" or not settings.agents_enabled:
        raise ConfigurationError("Source publication requires explicitly enabled dev settings.")
    if settings.app_workload_principal_id is None:
        raise ConfigurationError("Publication requires the App Service workload principal ID.")
    if role == AgentRole.ORCHESTRATOR:
        if phase != RecipePhase.GENERATION:
            raise ConfigurationError("Specialist phases cannot be published to the Orchestrator.")
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
    policy_name = (
        "asc-responsible-ai-v1"
        if role == AgentRole.ORCHESTRATOR
        else settings.foundry_recipe_nutrition_rai_policy_name
    )
    assert policy_name is not None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", policy_name):
        raise ConfigurationError("Configure an approved RAI policy name, not a URL or resource ID.")
    account_id = validated_project_resource_id(settings).rsplit("/projects/", 1)[0]
    policy_id = f"{account_id}/raiPolicies/{policy_name}"
    if role == AgentRole.ORCHESTRATOR:
        definition = build_orchestrator_definition(deployment, policy_id)
    else:
        knowledge = RecipeKnowledgeConfig.from_settings(settings)
        definition = build_recipe_definition(
            deployment,
            policy_id,
            phase=phase,
            knowledge_tool=(
                recipe_knowledge_tool(knowledge.mcp_endpoint, knowledge.connection_name)
                if phase == RecipePhase.RETRIEVAL
                else None
            ),
        )
    return AgentSource(
        name=name,
        definition=definition,
        metadata=recipe_source_metadata(definition, phase)
        if role == AgentRole.RECIPE_NUTRITION
        else {
            "asc_policy_version": str(policy_version),
            "asc_model": model,
            "asc_definition_sha256": definition_fingerprint(definition),
        },
    )


def publish_agent(
    settings: Settings,
    project: AIProjectClient,
    role: AgentRole,
    *,
    phase: RecipePhase = RecipePhase.GENERATION,
) -> str:
    source = prepare_agent_source(settings, role, phase=phase)
    identities = resolve_agent_identities(settings, project.agents)
    if any(project.agents.get(agent.agent_name).state != "enabled" for agent in identities):
        raise IdentityError("Both existing agents must be enabled.")
    version = project.agents.create_version(
        agent_name=source.name,
        definition=source.definition,
        metadata=source.metadata,
    )
    if not isinstance(version.version, str) or not re.fullmatch(
        r"[1-9][0-9]{0,9}", version.version
    ):
        raise ConfigurationError("Foundry returned an invalid source version.")
    return version.version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-dev-publication", action="store_true")
    parser.add_argument(
        "--agent", choices=[role.value for role in AgentRole], default="orchestrator"
    )
    parser.add_argument(
        "--phase", choices=[phase.value for phase in RecipePhase], default="generation"
    )
    parser.add_argument(
        "--verify-version", help="Read back this explicit source version; no writes."
    )
    args = parser.parse_args()
    if not args.approve_dev_publication and args.verify_version is None:
        parser.error("Explicit publication approval or --verify-version is required.")
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
            role, phase = AgentRole(args.agent), RecipePhase(args.phase)
            if args.verify_version is not None:
                from sous_chef.bootstrap_agents import verify_identity, verify_version

                if not re.fullmatch(r"[1-9][0-9]{0,9}", args.verify_version):
                    raise ConfigurationError("Select an explicit numeric source version.")
                source = prepare_agent_source(settings, role, phase=phase)
                resolve_agent_identities(settings, project.agents)
                verify_identity(settings, role, project.agents.get(source.name))
                if (
                    verify_version(
                        source, project.agents.get_version(source.name, args.verify_version)
                    )
                    != args.verify_version
                ):
                    raise ConfigurationError(
                        "The read-back does not match the approved version pin."
                    )
                version = args.verify_version
            else:
                version = publish_agent(settings, project, role, phase=phase)
    except (AzureError, ConfigurationError, IdentityError):
        raise SystemExit(
            "Agent source publication failed. Check dev configuration, "
            "identity bindings and publisher permissions; no release was activated."
        ) from None
    action = "Verified" if args.verify_version is not None else "Published"
    label = f" {args.phase}" if args.agent == AgentRole.RECIPE_NUTRITION.value else ""
    print(f"{action} {args.agent}{label} source version {version}.")


if __name__ == "__main__":
    main()
