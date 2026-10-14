"""Per-agent identities and tool allow-lists (ASC-12).

Runtime agent names, Entra Agent identity IDs and the App Service workload principal ID are
three different things and are never interchangeable. No agent may reach Cosmos directly.
"""

from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    RECIPE_NUTRITION = "recipe_nutrition"


class AgentTool(StrEnum):
    RECIPE_SPECIALIST = "recipe_specialist"
    FOUNDRY_IQ_RECIPES = "foundry_iq_recipes"
    USDA_NUTRITION = "usda_nutrition"
    WEB_IQ = "web_iq"


ALLOWED_TOOLS: dict[AgentRole, frozenset[AgentTool]] = {
    AgentRole.ORCHESTRATOR: frozenset({AgentTool.RECIPE_SPECIALIST}),
    AgentRole.RECIPE_NUTRITION: frozenset(
        {AgentTool.FOUNDRY_IQ_RECIPES, AgentTool.USDA_NUTRITION, AgentTool.WEB_IQ}
    ),
}


class IdentityError(RuntimeError):
    """An agent identity is missing, shared, or conflated with another principal."""


class ToolDenied(PermissionError):
    """The agent is not permitted to call this tool or operation."""


@dataclass(frozen=True)
class AgentIdentity:
    role: AgentRole
    agent_name: str
    principal_id: str
    client_id: str


def authorize_tool(role: AgentRole, operation: str) -> AgentTool:
    """Return the tool if the role may call it; deny anything else, including Cosmos."""
    try:
        tool = AgentTool(operation)
    except ValueError:
        raise ToolDenied(f"{role.value} may not call {operation!r}.") from None
    if tool not in ALLOWED_TOOLS[role]:
        raise ToolDenied(f"{role.value} may not call {operation!r}.")
    return tool


def verify_distinct(identities: list[AgentIdentity], workload_principal_id: str | None) -> None:
    seen: dict[str, AgentRole] = {}
    for identity in identities:
        for value in (identity.principal_id.lower(), identity.client_id.lower()):
            if value in seen and seen[value] != identity.role:
                raise IdentityError("Agents must not share an Entra Agent identity.")
        seen[identity.principal_id.lower()] = identity.role
        seen[identity.client_id.lower()] = identity.role
        if workload_principal_id and identity.principal_id.lower() == workload_principal_id.lower():
            raise IdentityError("An agent identity must differ from the App Service identity.")
    roles = {identity.role for identity in identities}
    if roles != set(AgentRole):
        raise IdentityError("Both the Orchestrator and Recipe + Nutrition identities are required.")
