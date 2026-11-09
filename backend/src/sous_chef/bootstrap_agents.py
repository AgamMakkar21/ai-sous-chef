"""Owner-approved first-time dev agent creation; never activates the app or grants access."""

import argparse
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentDetails, AgentVersionDetails, ApiErrorResponse
from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError
from azure.identity import AzureCliCredential

from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.publish_orchestrator import AgentSource, prepare_agent_source
from sous_chef.recipe_definition import RECIPE_KNOWLEDGE_TOOL, RecipePhase, web_search_tool
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry

RequestStep = Literal["read_agent", "read_version", "create_version", "resolve_identities"]
_REQUEST_STEPS: tuple[RequestStep, ...] = (
    "read_agent",
    "read_version",
    "create_version",
    "resolve_identities",
)
_REJECTION_FIELDS = {
    "name",
    "definition",
    "rai_policy_name",
    *(
        prefix + field
        for prefix in ("", "definition.")
        for field in (
            "model",
            "instructions",
            "rai_config",
            "rai_config.rai_policy_name",
            "tools",
            "tools[0].parameters",
            "tools[0].strict",
            "text.format.schema",
        )
    ),
}


@contextmanager
def request_step(step: RequestStep, role: AgentRole | None = None) -> Iterator[None]:
    try:
        yield
    except AzureError as error:
        error.add_note(f"bootstrap step={step} role={role.value if role else 'both'}")
        raise


def azure_failure_detail(error: AzureError) -> str:
    allowed_notes = {
        f"bootstrap step={step} role={role}"
        for step in _REQUEST_STEPS
        for role in ("both", *(role.value for role in AgentRole))
    }
    step = next(
        (note for note in getattr(error, "__notes__", ()) if note in allowed_notes),
        "bootstrap SDK request",
    )
    status = (
        f" HTTP {error.status_code}."
        if isinstance(error, HttpResponseError)
        and isinstance(error.status_code, int)
        and 100 <= error.status_code <= 599
        else " No HTTP status was available."
    )
    field = ""
    if (
        isinstance(error, HttpResponseError)
        and isinstance(error.model, ApiErrorResponse)
        and error.model.error is not None
        and error.model.error.param in _REJECTION_FIELDS
    ):
        field = f" Rejected field: {error.model.error.param}."
    return (
        f"{step} failed ({type(error).__name__}).{status}{field}"
        " Provider response content withheld."
    )


@dataclass(frozen=True)
class BootstrappedAgent:
    name: str
    version: str
    principal_id: str


def definition_difference_paths(
    expected: object, actual: object, path: str = "definition"
) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key in expected:
            if key not in actual:
                differences.append(f"{path}.{key} (missing)")
            else:
                differences.extend(
                    definition_difference_paths(expected[key], actual[key], f"{path}.{key}")
                )
        for key in actual.keys() - expected.keys():
            label = (
                key
                if key
                in {
                    "temperature",
                    "top_p",
                    "tools",
                    "tool_choice",
                    "text",
                    "format",
                    "type",
                    "reasoning",
                    "effort",
                    "summary",
                    "rai_config",
                    "rai_policy_name",
                    "strict",
                    "description",
                    "function",
                    "parallel_tool_calls",
                }
                else "<unrecognized-field>"
            )
            differences.append(f"{path}.{label} (added)")
        return sorted(differences)
    if isinstance(expected, list) and isinstance(actual, list):
        differences = []
        if len(expected) != len(actual):
            differences.append(f"{path} (length)")
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            differences.extend(definition_difference_paths(left, right, f"{path}[{index}]"))
        return differences
    return [] if expected == actual else [path]


def source_capabilities(definition: dict[str, object]) -> dict[str, object]:
    """Report bounded known capability settings, never instructions, URLs or custom values."""
    tools = definition.get("tools")
    if not isinstance(tools, list):
        return {"tools_shape_valid": False}
    summaries: list[dict[str, object]] = []
    for tool in tools[:8]:
        if not isinstance(tool, dict):
            summaries.append({"type": "<unrecognized>"})
            continue
        kind = tool.get("type")
        summary: dict[str, object] = {
            "type": kind
            if kind in ("mcp", "web_search", "web_search_preview", "function")
            else "<unrecognized>",
        }
        if kind in ("web_search", "web_search_preview"):
            summary["matches_reviewed_web_tool"] = tool == web_search_tool().as_dict()
            size = tool.get("search_context_size")
            summary["search_context_size"] = (
                size if size in ("low", "medium", "high") else "<unrecognized>"
            )
            access = tool.get("external_web_access")
            summary["external_web_access"] = access if type(access) is bool else "<unrecognized>"
            summary["filters_present"] = tool.get("filters") is not None
            summary["user_location_present"] = tool.get("user_location") is not None
        summaries.append(summary)
    choice = definition.get("tool_choice")
    result: dict[str, object] = {
        "tools": summaries,
        "tools_omitted": max(0, len(tools) - 8),
        "tool_choice": choice if choice in ("none", "auto", "required") else "<unrecognized>",
    }
    reasoning = definition.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        summary_mode = reasoning.get("summary")
        result["reasoning"] = {
            "effort": effort
            if effort in ("none", "minimal", "low", "medium", "high", "xhigh")
            else "<unrecognized>",
            "summary": summary_mode
            if summary_mode in (None, "auto", "concise", "detailed")
            else "<unrecognized>",
            "other_fields_present": bool(reasoning.keys() - {"effort", "summary"}),
        }
    else:
        result["reasoning_present"] = reasoning is not None
    return result


def inspect_sources(
    settings: Settings,
    project: AIProjectClient,
    *,
    phase: RecipePhase = RecipePhase.GENERATION,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for role in AgentRole:
        source = prepare_agent_source(
            settings,
            role,
            phase=phase if role == AgentRole.RECIPE_NUTRITION else RecipePhase.GENERATION,
        )
        with request_step("read_agent", role):
            try:
                agent = project.agents.get(source.name)
            except ResourceNotFoundError as error:
                if error.status_code != 404:
                    raise
                result[role.value] = {"exists": False}
                continue
        number = agent.versions.latest.version
        if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", number):
            raise ConfigurationError("Foundry returned an invalid source version.")
        with request_step("read_version", role):
            version = project.agents.get_version(source.name, number)
        report: dict[str, object] = {
            "exists": True,
            "name": source.name,
            "version": number,
            "name_matches": version.name == source.name,
            "status": (
                version.status
                if version.status in (None, "active", "inactive", "failed", "creating")
                else "<unrecognized>"
            ),
            "draft": version.draft if version.draft in (True, False, None) else "<unrecognized>",
            "definition_matches": version.definition.as_dict() == source.definition.as_dict(),
            "definition_difference_paths": definition_difference_paths(
                source.definition.as_dict(), version.definition.as_dict()
            ),
            "metadata_mismatches": [
                key for key, value in source.metadata.items() if version.metadata.get(key) != value
            ],
        }
        if role == AgentRole.RECIPE_NUTRITION:
            report["capabilities"] = source_capabilities(version.definition.as_dict())
            tools = version.definition.as_dict().get("tools", [])
            report["retrieval_only_filter_encoding"] = (
                isinstance(tools, list)
                and len(tools) > 0
                and isinstance(tools[0], dict)
                and tools[0].get("allowed_tools") == {"tool_names": [RECIPE_KNOWLEDGE_TOOL]}
            )
        result[role.value] = report
    return result


def verify_identity(settings: Settings, role: AgentRole, agent: AgentDetails) -> None:
    if agent.state != "enabled" or agent.instance_identity is None:
        raise IdentityError(f"The {role.value} agent must have an enabled instance identity.")
    identity = agent.instance_identity
    try:
        UUID(identity.principal_id)
        UUID(identity.client_id)
    except (ValueError, TypeError, AttributeError):
        raise IdentityError(f"The {role.value} agent identity is invalid.") from None
    if identity.principal_id.lower() == (settings.app_workload_principal_id or "").lower():
        raise IdentityError("An agent identity must differ from the App Service identity.")
    pinned = (
        settings.foundry_orchestrator_agent_principal_id
        if role == AgentRole.ORCHESTRATOR
        else settings.foundry_recipe_nutrition_agent_principal_id
    )
    if pinned is not None and pinned.lower() != identity.principal_id.lower():
        raise IdentityError(f"The {role.value} agent is bound to the wrong identity.")


def verify_version(source: AgentSource, version: AgentVersionDetails) -> str:
    if (
        version.name != source.name
        or not isinstance(version.version, str)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", version.version)
        or version.status != "active"
        or version.draft
        or version.definition.as_dict() != source.definition.as_dict()
        or any(version.metadata.get(key) != value for key, value in source.metadata.items())
    ):
        raise ConfigurationError(
            "An agent version does not match the reviewed source. "
            "Inspect it before publishing; bootstrap never overwrites existing definitions."
        )
    return version.version


def bootstrap(settings: Settings, project: AIProjectClient) -> dict[AgentRole, BootstrappedAgent]:
    sources = {role: prepare_agent_source(settings, role) for role in AgentRole}
    if len({source.name for source in sources.values()}) != len(AgentRole):
        raise ConfigurationError("Configure distinct runtime names for both agents.")
    versions: dict[AgentRole, str] = {}
    for role, source in sources.items():
        with request_step("read_agent", role):
            try:
                agent = project.agents.get(source.name)
            except ResourceNotFoundError as error:
                if error.status_code != 404:
                    raise
                continue
        verify_identity(settings, role, agent)
        version = agent.versions.latest.version
        if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", version):
            raise ConfigurationError("Foundry returned an invalid source version.")
        with request_step("read_version", role):
            versions[role] = verify_version(
                source, project.agents.get_version(source.name, version)
            )
    if len(versions) == len(AgentRole):
        with request_step("resolve_identities"):
            resolve_agent_identities(settings, project.agents)

    for role, source in sources.items():
        if role not in versions:
            with request_step("create_version", role):
                created = project.agents.create_version(
                    agent_name=source.name,
                    definition=source.definition,
                    metadata=source.metadata,
                )
            versions[role] = verify_version(source, created)
            with request_step("read_agent", role):
                verify_identity(settings, role, project.agents.get(source.name))

    with request_step("resolve_identities"):
        identities = resolve_agent_identities(settings, project.agents)
    for role, source in sources.items():
        with request_step("read_agent", role):
            verify_identity(settings, role, project.agents.get(source.name))
        with request_step("read_version", role):
            verify_version(source, project.agents.get_version(source.name, versions[role]))
    return {
        identity.role: BootstrappedAgent(
            identity.agent_name, versions[identity.role], identity.principal_id
        )
        for identity in identities
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--approve-dev-bootstrap", action="store_true")
    mode.add_argument(
        "--inspect", action="store_true", help="Compare source read-only; never create."
    )
    parser.add_argument(
        "--phase", choices=[phase.value for phase in RecipePhase], default="generation"
    )
    args = parser.parse_args()
    if not args.inspect and args.phase != RecipePhase.GENERATION:
        parser.error("--phase is only supported with --inspect.")
    configure_safe_telemetry()
    try:
        settings = load_settings()
        if settings.environment != "dev" or settings.foundry_project_endpoint is None:
            raise ConfigurationError("Select the approved dev Foundry project.")
        # Build both definitions before opening a client or creating either agent.
        for role in AgentRole:
            prepare_agent_source(settings, role)
        with (
            AzureCliCredential() as credential,
            AIProjectClient(
                endpoint=str(settings.foundry_project_endpoint),
                credential=credential,
                api_version="v1",
                retry_total=0,
            ) as project,
        ):
            if args.inspect:
                print(
                    json.dumps(
                        inspect_sources(settings, project, phase=RecipePhase(args.phase)), indent=2
                    )
                )
                return
            result = bootstrap(settings, project)
    except (AzureError, ConfigurationError, IdentityError) as error:
        detail = (
            str(error)
            if isinstance(error, (ConfigurationError, IdentityError))
            else azure_failure_detail(error)
        )
        if args.inspect:
            raise SystemExit(
                "Dev agent inspection failed. " + detail + " No Azure changes were requested."
            ) from None
        raise SystemExit(
            "Dev agent bootstrap failed. "
            + detail
            + " Creation may have partially completed; inspect Foundry before rerunning."
            + " No agents were deleted, no permissions were granted, and no release was activated."
        ) from None
    print("Both dev source agents verified; app invocation remains unchanged.")
    print(
        json.dumps(
            {
                role.value: {"name": agent.name, "version": agent.version}
                for role, agent in result.items()
            },
            indent=2,
        )
    )
    print("Verified Entra Agent principal IDs (for owner-managed tool permissions):")
    for role, agent in result.items():
        print(f"{role.value}: {agent.principal_id}")
    print("Review these source pins before enabling deploy/agents.dev.json.")


if __name__ == "__main__":
    main()
