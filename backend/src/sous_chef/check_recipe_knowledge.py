"""Owner-approved dev grounding checks; not a workload or full-chat smoke."""

import argparse
import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError, HttpResponseError
from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ClientRequest
from openai import APIError, APIStatusError
from pydantic import ValidationError

from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.adapters.foundry_gateway import (
    parse_tool_arguments,
    validate_recipe_mcp_envelopes,
)
from sous_chef.adapters.foundry_iq import (
    SEARCH_API_VERSION,
    SEARCH_SCOPE,
    SEMANTIC_CONFIGURATION,
    RecipeKnowledgeConfig,
)
from sous_chef.bootstrap_agents import verify_identity, verify_version
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.recipes import CuratedRecipe, RecipeToolError
from sous_chef.publish_orchestrator import prepare_agent_source
from sous_chef.recipe_definition import (
    RECIPE_KNOWLEDGE_LABEL,
    RECIPE_KNOWLEDGE_TOOL,
    RecipePhase,
)
from sous_chef.services.recipe_grounding import (
    DEV_MCP_MAX_CONTEXT_TOKENS,
    DEV_MCP_MAX_OUTPUT_CHARACTERS,
    McpGroundingError,
    parse_mcp_grounding,
    recipe_knowledge_arguments,
)
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry

QUERY = "rice dinner"
DIAGNOSTIC_MAX_CHARACTERS = DEV_MCP_MAX_OUTPUT_CHARACTERS
DIAGNOSTIC_MAX_TOKENS = DEV_MCP_MAX_CONTEXT_TOKENS
_SAFE_ERROR_CODES = {
    "invalid_request",
    "invalid_payload",
    "invalid_request_error",
    "invalid_parameter",
    "invalid_value",
    "unsupported_parameter",
    "unsupported_value",
    "missing_required_parameter",
    "model_not_found",
    "DeploymentNotFound",
    "content_filter",
    "mcp_tool_fetch_error",
    "mcp_protocol_error",
}
_SAFE_ERROR_PARAMETERS = {
    "model",
    "agent_reference",
    "agent_reference.name",
    "agent_reference.version",
    "input",
    "max_output_tokens",
    "max_tool_calls",
    "parallel_tool_calls",
    "store",
    "text",
    "text.format",
    "tool_choice",
    "tools",
    "tools[0]",
    "tools[0].allowed_tools",
    "tools[0].server_url",
    "tools[0].project_connection_id",
    "tools[0].require_approval",
}


class KnowledgeCheckError(RuntimeError):
    """A safe, locally constructed check failure."""


def summarize_tool_schema(schema: object, *, depth: int = 0) -> dict[str, object]:
    """Report bounded field/type metadata, never descriptions, defaults or examples."""
    if not isinstance(schema, dict):
        raise KnowledgeCheckError("The MCP input schema is not an object.")
    if depth >= 3:
        return {"nested_schema": True}
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or len(properties) > 32
        or not isinstance(required, list)
        or len(required) > 32
        or any(
            not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is None
            for name in (*properties, *required)
        )
    ):
        raise KnowledgeCheckError("The MCP input schema has unsupported field metadata.")
    types = {"string", "object", "array", "integer", "number", "boolean", "null"}
    kind = schema.get("type")
    summary: dict[str, object] = {
        "type": kind if isinstance(kind, str) and kind in types else "unspecified",
        "required": required,
        "properties": {
            name: summarize_tool_schema(value, depth=depth + 1)
            for name, value in properties.items()
        },
    }
    if "$ref" in schema:
        summary["schema_reference_present"] = True
    if "items" in schema:
        summary["items"] = summarize_tool_schema(schema["items"], depth=depth + 1)
    return summary


@asynccontextmanager
async def _administrative_search_client(
    settings: Settings,
) -> AsyncIterator[tuple[RecipeKnowledgeConfig, httpx.AsyncClient]]:
    if settings.environment != "dev":
        raise ConfigurationError(
            "Administrative Search requests require the approved dev settings."
        )
    knowledge = RecipeKnowledgeConfig.from_settings(settings)
    try:
        async with asyncio.timeout(30):
            async with AsyncAzureCliCredential(process_timeout=10) as credential:
                token = await credential.get_token(SEARCH_SCOPE)
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token.token}"},
                    follow_redirects=False,
                    max_redirects=0,  # MCP follows redirects independently of the client flag.
                    timeout=httpx.Timeout(15, connect=5),
                ) as http:
                    yield knowledge, http
    except (
        httpx.HTTPError,
        McpError,
        TimeoutError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        status = (
            f" HTTP {error.response.status_code}."
            if isinstance(error, httpx.HTTPStatusError) and 100 <= error.response.status_code <= 599
            else ""
        )
        raise KnowledgeCheckError(
            f"Administrative Search request failed ({type(error).__name__}).{status}"
            " Provider content withheld."
        ) from None
    except ExceptionGroup as error:
        statuses: set[int] = set()

        def known_transport_errors(group: ExceptionGroup[Exception]) -> bool:
            for item in group.exceptions:
                if (
                    isinstance(item, httpx.HTTPStatusError)
                    and 100 <= item.response.status_code <= 599
                ):
                    statuses.add(item.response.status_code)
                if isinstance(item, ExceptionGroup):
                    if not known_transport_errors(item):
                        return False
                elif not isinstance(
                    item,
                    (
                        httpx.HTTPError,
                        McpError,
                        TimeoutError,
                        json.JSONDecodeError,
                        ValidationError,
                    ),
                ):
                    return False
            return True

        if not known_transport_errors(error):
            raise
        status = f" HTTP statuses: {', '.join(map(str, sorted(statuses)))}." if statuses else ""
        raise KnowledgeCheckError(
            f"Administrative Search request failed in its transport.{status}"
            " Provider content withheld. Do not change permissions based on this result."
        ) from None


async def inspect_tool_schema(settings: Settings) -> dict[str, object]:
    """Administrative tools/list only; not an agent-identity or retrieval check."""
    async with _administrative_search_client(settings) as (knowledge, http):
        async with (
            streamable_http_client(knowledge.mcp_endpoint, http_client=http) as (
                reader,
                writer,
                _,
            ),
            ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
    tools = [tool for tool in listing.tools if tool.name == RECIPE_KNOWLEDGE_TOOL]
    if len(tools) != 1 or listing.nextCursor is not None:
        raise KnowledgeCheckError("The MCP metadata did not contain one expected tool.")
    summary: dict[str, object] = {
        "tool": RECIPE_KNOWLEDGE_TOOL,
        "input_schema": summarize_tool_schema(tools[0].inputSchema),
    }
    if len(json.dumps(summary)) > 8000:
        raise KnowledgeCheckError("The MCP schema metadata exceeds the inspection output limit.")
    return summary


def _metadata_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise KnowledgeCheckError("Search configuration metadata has an unsupported object shape.")
    return value


def _metadata_objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 1000:
        raise KnowledgeCheckError("Search configuration metadata has an unsupported list shape.")
    return [_metadata_object(item) for item in value]


async def inspect_grounding_config(settings: Settings) -> dict[str, object]:
    """Read three resource definitions; never query documents or invoke an MCP tool."""
    async with _administrative_search_client(settings) as (knowledge, http):
        resources = []
        for path in (
            f"indexes/{knowledge.index_name}",
            f"knowledgesources/{knowledge.knowledge_source_name}",
            f"knowledgebases/{knowledge.knowledge_base_name}",
        ):
            response = await http.get(
                f"{knowledge.search_endpoint}/{path}?api-version={SEARCH_API_VERSION}"
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise KnowledgeCheckError("Search configuration inspection requires HTTP 200.")
            resources.append(_metadata_object(response.json()))
    index, source, base = resources
    grounding_fields = [
        field
        for field in _metadata_objects(index.get("fields"))
        if field.get("name") == "grounding"
    ]
    grounding = grounding_fields[0] if len(grounding_fields) == 1 else {}
    semantic = _metadata_object(index.get("semantic", {}))
    configurations = [
        item
        for item in _metadata_objects(semantic.get("configurations", []))
        if item.get("name") == SEMANTIC_CONFIGURATION
    ]
    prioritized = _metadata_object(
        configurations[0].get("prioritizedFields", {}) if len(configurations) == 1 else {}
    )
    source_parameters = _metadata_object(source.get("searchIndexParameters", {}))
    source_names = [
        item.get("name") for item in _metadata_objects(base.get("knowledgeSources", []))
    ]
    reasoning = _metadata_object(base.get("retrievalReasoningEffort", {}))
    defaults = _metadata_object(base.get("retrieveDefaults", {}))
    checks = {
        "grounding_field_present": len(grounding_fields) == 1,
        "grounding_field_is_string": grounding.get("type") == "Edm.String",
        "grounding_field_searchable": grounding.get("searchable") is True,
        "grounding_field_retrievable": grounding.get("retrievable") is True,
        "recipe_semantic_configuration_present": len(configurations) == 1,
        "semantic_content_is_grounding": prioritized.get("prioritizedContentFields")
        == [{"fieldName": "grounding"}],
        "source_is_search_index": source.get("kind") == "searchIndex",
        "source_targets_configured_index": source_parameters.get("searchIndexName")
        == knowledge.index_name,
        "source_uses_recipe_semantic_configuration": source_parameters.get(
            "semanticConfigurationName"
        )
        == SEMANTIC_CONFIGURATION,
        "base_uses_only_configured_source": source_names == [knowledge.knowledge_source_name],
        "base_has_no_models": base.get("models") == [],
        "base_uses_minimal_reasoning": reasoning.get("kind") == "minimal",
        "base_uses_extractive_output": base.get("outputMode") == "extractiveData",
        "result_limit_matches": type(defaults.get("maxOutputDocuments")) is int
        and defaults["maxOutputDocuments"] == knowledge.max_results,
        "output_token_limit_matches": type(defaults.get("maxOutputSizeInTokens")) is int
        and defaults["maxOutputSizeInTokens"] == knowledge.service_output_tokens,
    }
    return {"checks": checks, "matches_expected_grounding_configuration": all(checks.values())}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "unsupported"


def _grounding_diagnostics(
    output: str,
    knowledge: RecipeKnowledgeConfig,
    *,
    max_output_characters: int = 16000,
    max_context_tokens: int | None = None,
) -> dict[str, object]:
    """Expose fixed type/presence labels and validation codes, never content values."""
    from sous_chef.services.context import _encoding

    summary: dict[str, object] = {"output_characters": len(output)}
    if len(output) <= 65_536:
        summary["output_tokens"] = len(_encoding().encode(output, disallowed_special=()))
    try:
        grounding = parse_mcp_grounding(
            output,
            max_results=knowledge.max_results,
            max_context_tokens=(
                knowledge.max_context_tokens if max_context_tokens is None else max_context_tokens
            ),
            max_output_characters=max_output_characters,
        )
    except McpGroundingError as error:
        summary.update(
            grounding_status="rejected", failure_code=error.code.value, stage=error.stage
        )
    else:
        summary.update(grounding_status=grounding.status, validated_sources=len(grounding.recipes))
    if len(output) > 16000:
        return summary
    try:
        envelope = _metadata_object(json.loads(output))
        content = _metadata_objects(envelope.get("content"))
        summary["content_items"] = len(content)
        summary["mcp_is_error"] = envelope.get("isError") is True
        if len(content) != 1 or content[0].get("type") != "text":
            return summary
        text = content[0].get("text")
        if not isinstance(text, str):
            return summary
        chunks: object = json.loads(text)
    except (KnowledgeCheckError, ValueError, RecursionError):
        summary["content_json_valid"] = False
        return summary
    summary["content_json_valid"] = True
    summary["content_json_type"] = _json_type(chunks)
    if not isinstance(chunks, list):
        return summary
    summary["chunk_count"] = len(chunks)
    shapes: list[dict[str, object]] = []
    for chunk in chunks[: knowledge.max_results]:
        shape: dict[str, object] = {"chunk_type": _json_type(chunk)}
        shapes.append(shape)
        if not isinstance(chunk, dict):
            continue
        raw = chunk.get("grounding", chunk.get("content"))
        shape.update(
            ref_id_present="ref_id" in chunk,
            ref_id_type=_json_type(chunk.get("ref_id")),
            grounding_present="grounding" in chunk,
            grounding_type=_json_type(chunk.get("grounding")),
            content_field_present="content" in chunk,
            selected_payload_field="grounding" if "grounding" in chunk else "content",
        )
        if not isinstance(raw, str):
            continue
        shape["grounding_characters"] = len(raw)
        try:
            decoded: object = json.loads(raw)
        except (ValueError, RecursionError):
            shape["grounding_json_valid"] = False
        else:
            shape["grounding_json_valid"] = True
            shape["grounding_json_type"] = _json_type(decoded)
            if isinstance(decoded, dict):
                shape["source_present"] = "source" in decoded
                shape["snippet_present"] = "snippet" in decoded
                snippet = decoded.get("snippet")
                if isinstance(snippet, str):
                    shape["snippet_characters"] = len(snippet)
        try:
            CuratedRecipe.model_validate_json(raw)
        except ValidationError as error:
            approved = {
                "missing",
                "extra_forbidden",
                "json_invalid",
                "string_type",
                "string_too_long",
                "string_too_short",
                "string_pattern_mismatch",
                "value_error",
                "literal_error",
                "model_type",
                "model_attributes_type",
            }
            shape["recipe_schema_valid"] = False
            shape["recipe_validation_codes"] = sorted(
                {
                    entry["type"] if entry["type"] in approved else "other"
                    for entry in error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                }
            )
        else:
            shape["recipe_schema_valid"] = True
    summary["chunk_shapes"] = shapes
    return summary


def _fixed_query_schema(schema: dict[str, object]) -> bool:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {"query_variants"}:
        return False
    variants = properties["query_variants"]
    if not isinstance(variants, dict):
        return False
    items = variants.get("items")
    if not isinstance(items, dict):
        return False
    annotations = {"description", "title", "$schema", "$comment", "default", "examples"}
    for node, supported in (
        (schema, {"type", "required", "properties", "additionalProperties"}),
        (
            variants,
            {"type", "items", "required", "properties", "minItems", "maxItems", "uniqueItems"},
        ),
        (items, {"type", "required", "properties", "minLength", "maxLength"}),
    ):
        if set(node) - annotations - supported:
            return False
    if (
        schema.get("type") != "object"
        or schema.get("required") != ["query_variants"]
        or not isinstance(schema.get("additionalProperties", False), bool)
        or variants.get("type") != "array"
        or items.get("type") != "string"
        or not isinstance(variants.get("uniqueItems", False), bool)
        or any(
            node.get("required", []) != [] or node.get("properties", {}) != {}
            for node in (variants, items)
        )
    ):
        return False
    for node, minimum, maximum, length in (
        (variants, "minItems", "maxItems", 1),
        (items, "minLength", "maxLength", len(QUERY)),
    ):
        lower, upper = node.get(minimum, 0), node.get(maximum, length)
        if type(lower) is not int or type(upper) is not int or not 0 <= lower <= length <= upper:
            return False
    return True


def _diagnostic_json_shape(value: object, *, depth: int = 0) -> dict[str, object]:
    shape: dict[str, object] = {"type": _json_type(value)}
    if isinstance(value, str):
        shape["characters"] = len(value)
    elif isinstance(value, list):
        shape["items"] = len(value)
        if depth < 4:
            shape["sample_shapes"] = [
                _diagnostic_json_shape(item, depth=depth + 1) for item in value[:3]
            ]
    elif isinstance(value, dict):
        shape["field_count"] = len(value)
        if depth < 4:
            shape["known_fields"] = {
                name: _diagnostic_json_shape(value[name], depth=depth + 1)
                for name in (
                    "ref_id",
                    "grounding",
                    "content",
                    "recipe_json",
                    "sourceData",
                    "references",
                    "activity",
                    "response",
                    "text",
                    "type",
                    "source",
                    "snippet",
                )
                if name in value
            }
    return shape


def _content_block_diagnostics(
    result: CallToolResult,
    knowledge: RecipeKnowledgeConfig,
) -> list[dict[str, object]]:
    """Inspect bounded native blocks without treating any subset as accepted evidence."""
    budget = 65_536
    blocks: list[dict[str, object]] = []
    for index, content in enumerate(result.content[:4]):
        block: dict[str, object] = {"index": index, "type": content.type}
        blocks.append(block)
        if content.type != "text":
            continue
        block["text_characters"] = len(content.text)
        if len(content.text) > budget:
            block["inspection_omitted"] = True
            continue
        budget -= len(content.text)
        try:
            decoded: object = json.loads(content.text)
        except (ValueError, RecursionError):
            block["text_json_valid"] = False
        else:
            block["text_json_valid"] = True
            shape = _diagnostic_json_shape(decoded)
            block["text_json_shape"] = (
                shape
                if len(json.dumps(shape)) <= 4096
                else {"type": _json_type(decoded), "shape_omitted": True}
            )
        block["isolated_block_check"] = _grounding_diagnostics(
            json.dumps({"content": [{"type": "text", "text": content.text}]}), knowledge
        )
    return blocks


async def diagnose_search_grounding(settings: Settings) -> dict[str, object]:
    configuration = await inspect_grounding_config(settings)
    if configuration["matches_expected_grounding_configuration"] is not True:
        raise KnowledgeCheckError(
            "Search-only diagnostic blocked by configuration drift; no retrieval was called."
        )
    result: CallToolResult | None = None
    async with _administrative_search_client(settings) as (knowledge, http):
        async with (
            streamable_http_client(knowledge.mcp_endpoint, http_client=http) as (
                reader,
                writer,
                _,
            ),
            ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            tools = [tool for tool in listing.tools if tool.name == RECIPE_KNOWLEDGE_TOOL]
            if (
                len(tools) == 1
                and listing.nextCursor is None
                and _fixed_query_schema(tools[0].inputSchema)
            ):
                # SDK output-schema errors can embed recipe values before inspection.
                result = await session.send_request(
                    ClientRequest(
                        CallToolRequest(
                            params=CallToolRequestParams(
                                name=RECIPE_KNOWLEDGE_TOOL,
                                arguments=recipe_knowledge_arguments(QUERY),
                            )
                        )
                    ),
                    CallToolResult,
                    request_read_timeout_seconds=timedelta(seconds=15),
                )
    if result is None:
        raise KnowledgeCheckError(
            "Search-only diagnostic blocked by the MCP input contract; no retrieval was called."
        )
    output = result.model_dump_json(by_alias=True, exclude_none=True)
    diagnostics = _grounding_diagnostics(
        output,
        knowledge,
        max_output_characters=DIAGNOSTIC_MAX_CHARACTERS,
        max_context_tokens=DIAGNOSTIC_MAX_TOKENS,
    )
    runtime = _grounding_diagnostics(
        output,
        knowledge,
        max_output_characters=knowledge.mcp_max_output_characters,
        max_context_tokens=knowledge.mcp_max_context_tokens,
    )
    diagnostics["diagnostic_budget"] = {
        "max_output_characters": DIAGNOSTIC_MAX_CHARACTERS,
        "max_context_tokens": DIAGNOSTIC_MAX_TOKENS,
    }
    runtime_acceptance: dict[str, object] = {
        "max_output_characters": knowledge.mcp_max_output_characters,
        "max_context_tokens": knowledge.mcp_max_context_tokens,
    }
    for key in ("grounding_status", "failure_code", "stage"):
        if key in runtime:
            runtime_acceptance[key] = runtime[key]
    diagnostics["runtime_acceptance"] = runtime_acceptance
    diagnostics["content_items"] = len(result.content)
    diagnostics["mcp_is_error"] = result.isError
    diagnostics["mcp_structured_content_present"] = result.structuredContent is not None
    diagnostics["mcp_output_schema_present"] = tools[0].outputSchema is not None
    diagnostics["content_block_diagnostics"] = _content_block_diagnostics(result, knowledge)
    diagnostics["content_blocks_omitted"] = max(0, len(result.content) - 4)
    return diagnostics


def check(
    settings: Settings, project: AIProjectClient, *, version: str, principal_id: UUID
) -> tuple[UUID, int]:
    if settings.environment != "dev" or not re.fullmatch(r"[1-9][0-9]{0,9}", version):
        raise ConfigurationError("Select dev and an explicit numeric specialist source version.")
    phase = (
        RecipePhase.COMBINED
        if settings.foundry_recipe_nutrition_mode == "combined"
        else RecipePhase.RETRIEVAL
    )
    source = prepare_agent_source(settings, AgentRole.RECIPE_NUTRITION, phase=phase)
    knowledge = RecipeKnowledgeConfig.from_settings(settings)
    identities = resolve_agent_identities(settings, project.agents)
    identity = next(item for item in identities if item.role == AgentRole.RECIPE_NUTRITION)
    if identity.principal_id.lower() != str(principal_id):
        raise IdentityError("The specialist does not match the approved principal pin.")
    verify_identity(settings, AgentRole.RECIPE_NUTRITION, project.agents.get(source.name))
    if verify_version(source, project.agents.get_version(source.name, version)) != version:
        raise ConfigurationError(
            "The specialist read-back does not match the approved version pin."
        )
    connection = project.connections.get(knowledge.connection_name, include_credentials=False)
    if (
        connection.type not in ("RemoteTool", "RemoteTool_Preview")
        or connection.target != knowledge.mcp_endpoint
        or connection.credentials is None
        or connection.credentials.type
        not in ("AgenticIdentityToken", "AgenticIdentityToken_Preview")
    ):
        raise ConfigurationError(
            "The recipe connection must target the approved knowledge base using agentic identity."
        )
    expected_arguments = recipe_knowledge_arguments(QUERY)
    serialized_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    operation_id = uuid4()
    with project.get_openai_client(
        max_retries=0,
        timeout=settings.foundry_invocation_timeout_seconds,
        _strict_response_validation=True,
    ) as client:
        response = client.responses.create(
            input=(
                "This is a recipe retrieval connectivity check with no personal context. "
                f"Call {RECIPE_KNOWLEDGE_TOOL} once with exactly {serialized_arguments}. "
                "Do not use web search. Follow the published response format after retrieval. "
                "The generated draft is not presented or used as an accepted recipe."
            ),
            extra_headers={"X-Client-Request-Id": str(operation_id)},
            extra_body={
                "agent_reference": {
                    "type": "agent_reference",
                    "name": source.name,
                    "version": version,
                },
                "max_tool_calls": 1,
                "store": False,
            },
        )
    if (
        response.status != "completed"
        or response.error is not None
        or response.incomplete_details is not None
        or response.usage is None
    ):
        raise KnowledgeCheckError(
            "The bounded specialist invocation did not complete successfully."
        )
    calls = [item for item in response.output if item.type == "mcp_call"]
    listings = [item for item in response.output if item.type == "mcp_list_tools"]
    if (
        len(calls) != 1
        or len(listings) > 1
        or any(
            item.type not in {"reasoning", "message", "mcp_list_tools", "mcp_call"}
            for item in response.output
        )
        or any(
            item.server_label != RECIPE_KNOWLEDGE_LABEL or item.error is not None
            for item in listings
        )
    ):
        raise KnowledgeCheckError("The response did not contain exactly one approved MCP call.")
    try:
        validate_recipe_mcp_envelopes(
            [item.model_dump_json() for item in listings],
            [item.model_dump_json() for item in calls],
            max_tool_result_tokens=knowledge.mcp_max_context_tokens,
        )
    except ValueError:
        raise KnowledgeCheckError(
            "Recipe MCP response exceeds the dev runtime envelope budget."
        ) from None
    call = calls[0]
    if call.server_label != RECIPE_KNOWLEDGE_LABEL or call.name != RECIPE_KNOWLEDGE_TOOL:
        raise KnowledgeCheckError("The specialist called an unexpected tool.")
    if call.error is not None:
        status = (
            f" HTTP {call.error.code}."
            if call.error.type == "http_error" and 100 <= call.error.code <= 599
            else ""
        )
        raise KnowledgeCheckError("The recipe MCP call failed." + status)
    try:
        arguments = parse_tool_arguments(call.arguments)
    except ValueError:
        raise KnowledgeCheckError("The MCP call contained invalid tool-argument JSON.") from None
    if arguments != expected_arguments:
        raise KnowledgeCheckError(
            "The MCP call did not use the fixed public query. "
            f"query_variants_present={'query_variants' in arguments}; "
            f"query_matches={arguments.get('query_variants') == [QUERY]}; "
            f"extra_arguments={bool(arguments.keys() - expected_arguments.keys())}. "
            "Argument values withheld."
        )
    if call.output is None:
        raise KnowledgeCheckError("The recipe MCP call returned no output.")
    try:
        grounding = parse_mcp_grounding(
            call.output,
            max_results=knowledge.max_results,
            max_context_tokens=knowledge.mcp_max_context_tokens,
            max_output_characters=knowledge.mcp_max_output_characters,
        )
    except McpGroundingError as error:
        raise KnowledgeCheckError(
            f"Recipe MCP grounding rejected: {error.code.value}; stage={error.stage}. "
            "Provider content withheld. Do not automatically retry."
        ) from None
    if grounding.status != "available":
        raise KnowledgeCheckError("Retrieval returned no validated curated recipe sources.")
    return operation_id, len(grounding.recipes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--approve-dev-check", action="store_true")
    mode.add_argument("--inspect-tool-schema", action="store_true")
    mode.add_argument("--inspect-grounding-config", action="store_true")
    mode.add_argument("--approve-dev-search-diagnostic", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--principal-id", type=UUID)
    args = parser.parse_args()
    if args.approve_dev_check and (args.version is None or args.principal_id is None):
        parser.error("A live check requires --version and --principal-id.")
    if (
        args.inspect_tool_schema
        or args.inspect_grounding_config
        or args.approve_dev_search_diagnostic
    ) and (args.version is not None or args.principal_id is not None):
        parser.error(
            "Administrative Search modes use the operator identity, not agent version pins."
        )
    configure_safe_telemetry()
    try:
        settings = load_settings()
        if settings.environment != "dev" or settings.foundry_project_endpoint is None:
            raise ConfigurationError("Select the approved dev Foundry project.")
        if args.inspect_tool_schema:
            print(json.dumps(asyncio.run(inspect_tool_schema(settings)), indent=2))
            print("Administrative tool metadata only; no model invocation or recipe retrieval.")
            return 0
        if args.inspect_grounding_config:
            print(json.dumps(asyncio.run(inspect_grounding_config(settings)), indent=2))
            print("Administrative configuration metadata only; no model invocation or retrieval.")
            return 0
        if args.approve_dev_search_diagnostic:
            diagnostics = asyncio.run(diagnose_search_grounding(settings))
            print(json.dumps(diagnostics, indent=2))
            print(
                "Search-only diagnostic; administrator identity, not the specialist identity. "
                "Dev recipe-retrieval allowance; production limits are unchanged. "
                "No Foundry agent/GPT invocation. Do not repeat this attempt."
            )
            return 0 if diagnostics.get("grounding_status") == "available" else 1
        with (
            AzureCliCredential() as credential,
            AIProjectClient(
                endpoint=str(settings.foundry_project_endpoint),
                credential=credential,
                api_version="v1",
                retry_total=0,
            ) as project,
        ):
            operation_id, count = check(
                settings, project, version=args.version, principal_id=args.principal_id
            )
    except (ConfigurationError, IdentityError, KnowledgeCheckError, RecipeToolError) as error:
        print(f"Recipe knowledge check failed: {error}")
        return 1
    except (AzureError, APIError) as error:
        status = (
            f" HTTP {error.status_code}."
            if isinstance(error, (HttpResponseError, APIStatusError))
            and isinstance(error.status_code, int)
            and 100 <= error.status_code <= 599
            else " No HTTP status was available."
        )
        identifiers = ""
        if isinstance(error, APIError):
            if isinstance(error.code, str) and error.code in _SAFE_ERROR_CODES:
                identifiers += f" Code: {error.code}."
            if isinstance(error.param, str) and error.param in _SAFE_ERROR_PARAMETERS:
                identifiers += f" Parameter: {error.param}."
        print(
            f"Recipe knowledge check failed ({type(error).__name__}).{status}"
            + identifiers
            + " Provider response content withheld. Do not automatically retry."
        )
        return 1
    print(f"Validated agentic MCP retrieval; sources={count}; operation_id={operation_id}.")
    print(
        "Administrator-initiated test only; App Service workload and full chat remain unverified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
