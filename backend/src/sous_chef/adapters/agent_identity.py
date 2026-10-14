from typing import Any, Protocol

from sous_chef.domain.agent_identity import (
    AgentIdentity,
    AgentRole,
    IdentityError,
    verify_distinct,
)
from sous_chef.settings import ConfigurationError, Settings


class _Agents(Protocol):
    def get(self, agent_name: str) -> Any: ...


def resolve_agent_identities(settings: Settings, agents: _Agents) -> list[AgentIdentity]:
    """Read each deployed agent's Entra Agent identity from Foundry and verify they are distinct.

    Legacy agents have no instance identity (they share the project identity), so they fail here.
    Recreate them as new-model agents.
    """
    names = {
        AgentRole.ORCHESTRATOR: settings.foundry_orchestrator_agent_name,
        AgentRole.RECIPE_NUTRITION: settings.foundry_recipe_nutrition_agent_name,
    }
    identities: list[AgentIdentity] = []
    for role, name in names.items():
        if name is None:
            raise ConfigurationError("Set ASC_FOUNDRY_*_AGENT_NAME for both agents.")
        try:
            details = agents.get(name)
            instance = details.instance_identity
        except Exception as exc:
            raise IdentityError(
                f"Could not read the {role.value} agent: {type(exc).__name__}."
            ) from None
        if instance is None:
            raise IdentityError(
                f"The {role.value} agent has no Entra Agent identity (legacy agent). Recreate it."
            )
        identities.append(AgentIdentity(role, name, instance.principal_id, instance.client_id))
    verify_distinct(identities, settings.app_workload_principal_id)
    expected = {
        AgentRole.ORCHESTRATOR: settings.foundry_orchestrator_agent_principal_id,
        AgentRole.RECIPE_NUTRITION: settings.foundry_recipe_nutrition_agent_principal_id,
    }
    for identity in identities:
        pinned = expected[identity.role]
        if pinned is not None and pinned.lower() != identity.principal_id.lower():
            raise IdentityError(f"The {identity.role.value} agent is bound to the wrong identity.")
    return identities
