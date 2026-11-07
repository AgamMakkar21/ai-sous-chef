import asyncio
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call
from uuid import UUID

import httpx
import httpx2
import pytest
from azure.ai.projects.models import AgenticIdentityPreviewCredentials, Connection
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from openai import BadRequestError
from openai.types.responses import Response
from pydantic import ValidationError
from sous_chef import check_recipe_knowledge
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.check_recipe_knowledge import (
    KnowledgeCheckError,
    _content_block_diagnostics,
    _fixed_query_schema,
    _grounding_diagnostics,
    check,
    diagnose_search_grounding,
    inspect_grounding_config,
    inspect_tool_schema,
    summarize_tool_schema,
)
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.recipes import RecipeToolError
from sous_chef.recipe_definition import RecipePhase
from sous_chef.services.recipe_indexing import curated_excerpt, search_document
from sous_chef.settings import ConfigurationError
from test_agent_bootstrap import Harness, config
from test_agent_identity import RECIPE
from test_foundry_gateway import response_body
from test_recipe_indexing import recipe_fixture


def response(**overrides: Any) -> Response:
    call = {
        "id": "mcp_synthetic",
        "type": "mcp_call",
        "server_label": "curated_recipes",
        "name": "knowledge_base_retrieve",
        "arguments": '{"query_variants":["rice dinner"]}',
        "output": json.dumps(
            [{"ref_id": "0", "grounding": curated_excerpt(recipe_fixture()).model_dump_json()}]
        ),
        "error": None,
    }
    return Response.model_validate(
        response_body(
            output=[call],
            usage={
                "input_tokens": 400,
                "output_tokens": 200,
                "total_tokens": 600,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            **overrides,
        )
    )


def setup() -> tuple[Harness, MagicMock]:
    harness = Harness(config())
    for role in AgentRole:
        harness.seed(
            role,
            phase=RecipePhase.RETRIEVAL
            if role == AgentRole.RECIPE_NUTRITION
            else RecipePhase.GENERATION,
        )
    knowledge = RecipeKnowledgeConfig.from_settings(harness.config)
    harness.project.connections = Mock()
    harness.project.connections.get.return_value = Connection(
        name=knowledge.connection_name,
        id="synthetic-connection",
        type="RemoteTool_Preview",
        target=knowledge.mcp_endpoint,
        is_default=False,
        credentials=AgenticIdentityPreviewCredentials(),
        metadata={},
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value = response()
    harness.project.get_openai_client.return_value = client
    return harness, client


def run(harness: Harness, *, principal: str = RECIPE[0], version: str = "7") -> tuple[UUID, int]:
    return check(harness.config, harness.project, version=version, principal_id=UUID(principal))


def test_one_pinned_agentic_retrieval_validates_provenance_without_writes() -> None:
    harness, client = setup()
    operation_id, count = run(harness)
    assert count == 1
    client.responses.create.assert_called_once()
    arguments = client.responses.create.call_args.kwargs
    assert arguments["extra_headers"] == {"X-Client-Request-Id": str(operation_id)}
    assert arguments["extra_body"]["agent_reference"] == {
        "type": "agent_reference",
        "name": "recipe-agent",
        "version": "7",
    }
    assert arguments["max_output_tokens"] == 1500
    assert arguments["extra_body"]["max_tool_calls"] == 1
    assert arguments["extra_body"]["store"] is False
    assert '{"query_variants":["rice dinner"]}' in arguments["input"]
    assert (
        not {"tools", "tool_choice", "parallel_tool_calls", "text", "instructions"}
        & arguments["extra_body"].keys()
    )
    assert harness.versions["recipe-agent"].definition.as_dict()["tools"][0]["allowed_tools"] == {
        "tool_names": ["knowledge_base_retrieve"]
    }
    assert harness.project.get_openai_client.call_args.kwargs["max_retries"] == 0
    harness.project.connections.get.assert_called_once_with(
        "fixture-mcp", include_credentials=False
    )
    harness.project.agents.create_version.assert_not_called()
    harness.project.agents.delete.assert_not_called()
    assert harness.project.connections.mock_calls == [
        call.get("fixture-mcp", include_credentials=False)
    ]


@pytest.mark.parametrize("fault", ["none", "discovery"])
def test_pinned_dev_check_uses_runtime_envelope_allowance_without_relaxing_discovery(
    fault: str,
) -> None:
    from sous_chef.services.context import _encoding

    harness, client = setup()
    document = search_document(recipe_fixture())
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    [
                        {
                            "ref_id": 0,
                            "content": document["grounding"],
                        }
                    ]
                ),
            },
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ref_id": 0,
                        "sourceData": {
                            name: document[name]
                            for name in (
                                "grounding",
                                "source_id",
                                "title",
                                "source_url",
                                "recipe_json",
                            )
                        },
                        "untrusted_metadata": "private padding " * 2000,
                    }
                ),
            },
        ],
    }
    output = json.dumps(result)
    tokens = len(_encoding().encode(output, disallowed_special=()))
    assert 16000 < len(output) < 65536 and 4000 < tokens < 16000
    data = response().model_dump()
    data["output"][0]["output"] = output
    data["usage"]["input_tokens"] = tokens + 1000
    if fault == "discovery":
        data["output"].insert(
            0,
            {
                "id": "list_synthetic",
                "type": "mcp_list_tools",
                "server_label": "curated_recipes",
                "tools": [
                    {
                        "name": "knowledge_base_retrieve",
                        "input_schema": {},
                        "description": "private metadata " * 2001,
                    }
                ],
            },
        )
    client.responses.create.return_value = Response.model_validate(data)
    if fault == "none":
        assert run(harness)[1] == 1
    else:
        with pytest.raises(KnowledgeCheckError) as caught:
            run(harness)
        assert "runtime envelope budget" in str(caught.value)
        assert "private" not in str(caught.value)
    client.responses.create.assert_called_once()
    harness.project.agents.create_version.assert_not_called()


@pytest.mark.parametrize("fault", ["target", "auth", "identity", "version", "source", "phase"])
def test_invalid_target_or_binding_stops_before_model_invocation(fault: str) -> None:
    harness, client = setup()
    principal = RECIPE[0]
    version = "7"
    if fault == "target":
        harness.project.connections.get.return_value.target = "https://other.invalid"
    elif fault == "auth":
        harness.project.connections.get.return_value.credentials.type = "AAD"
    elif fault == "identity":
        principal = "11111111-2222-3333-4444-555555555555"
    elif fault == "version":
        version = "latest"
    elif fault == "phase":
        harness.versions["recipe-agent"].metadata["asc_recipe_phase"] = "generation"
    else:
        harness.versions["recipe-agent"].metadata = {}
    with pytest.raises((ConfigurationError, IdentityError)):
        run(harness, principal=principal, version=version)
    client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    ["incomplete", "usage", "no-call", "wrong-tool", "query", "empty", "tool-error", "schema"],
)
def test_invalid_or_failed_retrieval_never_becomes_success_and_is_not_replayed(fault: str) -> None:
    harness, client = setup()
    data = response().model_dump()
    call = data["output"][0]
    if fault == "incomplete":
        data["status"] = "incomplete"
    elif fault == "usage":
        data["usage"] = None
    elif fault == "no-call":
        data["output"] = []
    elif fault == "wrong-tool":
        call["name"] = "execute_query"
    elif fault == "query":
        call["arguments"] = '{"query_variants":["other"],"query_variants":["rice dinner"]}'
    elif fault == "empty":
        call["output"] = "[]"
    elif fault == "tool-error":
        call["error"] = {"type": "http_error", "code": 403, "message": "private provider failure"}
    else:
        call["output"] = '[{"ref_id":"0","grounding":"private invalid recipe"}]'
    client.responses.create.return_value = Response.model_validate(data)
    with pytest.raises((KnowledgeCheckError, RecipeToolError)) as caught:
        run(harness)
    assert "private" not in str(caught.value)
    client.responses.create.assert_called_once()


def test_cli_requires_explicit_approval_before_any_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["check_recipe_knowledge"])
    client = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", client)
    with pytest.raises(SystemExit) as caught:
        check_recipe_knowledge.main()
    assert caught.value.code == 2
    client.assert_not_called()


def test_cli_hides_provider_content_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_recipe_knowledge",
            "--approve-dev-check",
            "--version",
            "7",
            "--principal-id",
            RECIPE[0],
        ],
    )
    monkeypatch.setattr(check_recipe_knowledge, "load_settings", lambda: config())
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", MagicMock())
    monkeypatch.setattr(check_recipe_knowledge, "AzureCliCredential", MagicMock())
    failure = HttpResponseError("private endpoint and provider response")
    failure.status_code = 403
    monkeypatch.setattr(check_recipe_knowledge, "check", Mock(side_effect=failure))
    assert check_recipe_knowledge.main() == 1
    output = capsys.readouterr().out
    assert "HTTP 403" in output and "Do not automatically retry" in output
    assert "private" not in output
    assert "Validated agentic MCP retrieval" not in output


def test_cli_reports_only_validated_count_and_correlation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness, _ = setup()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_recipe_knowledge",
            "--approve-dev-check",
            "--version",
            "7",
            "--principal-id",
            RECIPE[0],
        ],
    )
    monkeypatch.setattr(check_recipe_knowledge, "load_settings", lambda: harness.config)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = harness.project
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", factory)
    monkeypatch.setattr(check_recipe_knowledge, "AzureCliCredential", MagicMock())
    assert check_recipe_knowledge.main() == 0
    output = capsys.readouterr().out
    assert "sources=1; operation_id=" in output
    assert "App Service workload and full chat remain unverified" in output
    assert "Synthetic" not in output and "synthetic.services.ai.azure.com" not in output
    assert factory.call_args.kwargs["api_version"] == "v1"
    assert factory.call_args.kwargs["retry_total"] == 0


@pytest.mark.parametrize(
    ("code", "param"),
    [
        ("invalid_request_error", "tools"),
        ("invalid_request_error", "private field"),
        ("private code", "private field"),
        ({"private": "code"}, ["private field"]),
    ],
)
def test_cli_exposes_only_allowlisted_provider_identifiers(
    code: object,
    param: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_recipe_knowledge",
            "--approve-dev-check",
            "--version",
            "7",
            "--principal-id",
            RECIPE[0],
        ],
    )
    monkeypatch.setattr(check_recipe_knowledge, "load_settings", lambda: config())
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", MagicMock())
    monkeypatch.setattr(check_recipe_knowledge, "AzureCliCredential", MagicMock())
    failure = BadRequestError(
        "private provider message",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://private.invalid")),
        body={
            "code": code,
            "param": param,
            "message": "private provider message",
        },
    )
    monkeypatch.setattr(check_recipe_knowledge, "check", Mock(side_effect=failure))
    assert check_recipe_knowledge.main() == 1
    output = capsys.readouterr().out
    assert "private" not in output
    assert ("Code:" in output) == (code == "invalid_request_error")
    assert ("Parameter:" in output) == (param == "tools")


def test_schema_metadata_discovery_never_invokes_a_model_or_retrieval_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=AccessToken("private-synthetic-token", 1))
    credential_factory = Mock(return_value=credential)
    monkeypatch.setattr(check_recipe_knowledge, "AsyncAzureCliCredential", credential_factory)
    http_factory = MagicMock()
    http = http_factory.return_value.__aenter__.return_value
    monkeypatch.setattr("sous_chef.check_recipe_knowledge.httpx.AsyncClient", http_factory)
    transport = MagicMock()
    transport.return_value.__aenter__.return_value = (MagicMock(), MagicMock(), Mock())
    monkeypatch.setattr(check_recipe_knowledge, "streamable_http_client", transport)
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(
        return_value=ListToolsResult(
            tools=[
                Tool(
                    name="knowledge_base_retrieve",
                    description="private server description",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query_variants": {
                                "type": "array",
                                "description": "private",
                                "default": ["private"],
                                "examples": [["private"]],
                                "items": {"type": "string", "enum": ["private"]},
                            }
                        },
                        "required": ["query_variants"],
                    },
                )
            ]
        )
    )
    session_factory = MagicMock()
    session_factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(check_recipe_knowledge, "ClientSession", session_factory)
    project = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", project)
    summary = asyncio.run(inspect_tool_schema(config(agents_enabled=False)))
    assert summary["input_schema"] == {
        "type": "object",
        "required": ["query_variants"],
        "properties": {
            "query_variants": {
                "type": "array",
                "required": [],
                "properties": {},
                "items": {"type": "string", "required": [], "properties": {}},
            }
        },
    }
    assert "private" not in json.dumps(summary)
    credential_factory.assert_called_once_with(process_timeout=10)
    credential.get_token.assert_awaited_once_with("https://search.azure.com/.default")
    credential.__aexit__.assert_awaited_once()
    assert http_factory.call_args.kwargs["follow_redirects"] is False
    transport.assert_called_once_with(
        RecipeKnowledgeConfig.from_settings(config()).mcp_endpoint, http_client=http
    )
    session.initialize.assert_awaited_once()
    session.list_tools.assert_awaited_once()
    session.call_tool.assert_not_called()
    project.assert_not_called()


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"properties": {"private@example.invalid": {"type": "string"}}},
        {"required": "private"},
        {"properties": {f"field{index}": {} for index in range(33)}},
    ],
)
def test_unsupported_schema_metadata_is_not_printed(schema: object) -> None:
    with pytest.raises(KnowledgeCheckError) as caught:
        summarize_tool_schema(schema)
    assert "private" not in str(caught.value)


def test_schema_inspection_cli_does_not_enter_the_agent_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_recipe_knowledge", "--inspect-tool-schema"])
    monkeypatch.setattr(
        check_recipe_knowledge, "load_settings", lambda: config(agents_enabled=False)
    )
    inspection = AsyncMock(return_value={"tool": "knowledge_base_retrieve", "input_schema": {}})
    monkeypatch.setattr(check_recipe_knowledge, "inspect_tool_schema", inspection)
    invocation = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "check", invocation)
    assert check_recipe_knowledge.main() == 0
    assert "no model invocation or recipe retrieval" in capsys.readouterr().out
    inspection.assert_awaited_once()
    invocation.assert_not_called()


@pytest.mark.parametrize("error_group", [False, True])
def test_schema_transport_rejection_withholds_provider_content(
    error_group: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = httpx.HTTPStatusError(
        "private provider response",
        request=httpx.Request("POST", "https://private.invalid"),
        response=httpx.Response(403),
    )
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=AccessToken("private-synthetic-token", 1))
    monkeypatch.setattr(
        check_recipe_knowledge, "AsyncAzureCliCredential", Mock(return_value=credential)
    )
    monkeypatch.setattr("sous_chef.check_recipe_knowledge.httpx.AsyncClient", MagicMock())
    transport = MagicMock()
    transport.return_value.__aenter__.side_effect = (
        ExceptionGroup("private SDK wrapper", [error]) if error_group else error
    )
    monkeypatch.setattr(check_recipe_knowledge, "streamable_http_client", transport)
    with pytest.raises(KnowledgeCheckError) as caught:
        asyncio.run(inspect_tool_schema(config()))
    assert "403" in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    ("arguments", "matches", "extra"),
    [
        ('{"query_variants":["rice dinner"],"extra":"private"}', True, True),
        ('{"query_variants":["private"]}', False, False),
        ('{"request":{"query":"private"}}', False, True),
        ('{"query":"rice dinner"}', False, True),
        ('{"query_variants":["rice dinner","private"]}', False, False),
        ('{"query_variants":["rice dinner","rice dinner"]}', False, False),
        ('{"query_variants":[]}', False, False),
        ('{"query_variants":"rice dinner"}', False, False),
        ('{"query_variants":[{"query":"rice dinner"}]}', False, False),
        ('{"query_variants":["rice dinner "]}', False, False),
    ],
)
def test_query_rejection_reports_only_boolean_diagnostics(
    arguments: str,
    matches: bool,
    extra: bool,
) -> None:
    harness, client = setup()
    data = response().model_dump()
    data["output"][0]["arguments"] = arguments
    client.responses.create.return_value = Response.model_validate(data)
    with pytest.raises(KnowledgeCheckError) as caught:
        run(harness)
    assert f"query_matches={matches}" in str(caught.value)
    assert f"extra_arguments={extra}" in str(caught.value)
    assert "private" not in str(caught.value)
    client.responses.create.assert_called_once()


def test_corrected_arguments_use_the_prior_verified_source_without_republication() -> None:
    harness, client = setup()
    stored = harness.versions["recipe-agent"]
    stored.version = "3"
    harness.project.agents.get_version.side_effect = None
    harness.project.agents.get_version.return_value = stored
    definition = stored.definition.as_dict()
    metadata = dict(stored.metadata)
    assert run(harness, version="3")[1] == 1
    assert (
        client.responses.create.call_args.kwargs["extra_body"]["agent_reference"]["version"] == "3"
    )
    assert stored.definition.as_dict() == definition and stored.metadata == metadata
    assert stored.metadata["asc_policy_version"] == "5"
    harness.project.agents.get_version.assert_called_once_with("recipe-agent", "3")
    harness.project.agents.create_version.assert_not_called()


@pytest.fixture
def metadata_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=AccessToken("synthetic-metadata-token", 1))
    monkeypatch.setattr(
        check_recipe_knowledge, "AsyncAzureCliCredential", Mock(return_value=credential)
    )
    monkeypatch.setattr("sous_chef.check_recipe_knowledge.httpx.AsyncClient", MagicMock())
    transport = MagicMock()
    transport.return_value.__aenter__.return_value = (MagicMock(), MagicMock(), Mock())
    monkeypatch.setattr(check_recipe_knowledge, "streamable_http_client", transport)
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock()
    session.send_request = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(check_recipe_knowledge, "ClientSession", session_factory)
    return session


@pytest.mark.parametrize("fault", ["missing", "duplicate", "pagination", "invalid-schema", "size"])
def test_invalid_tool_listing_stops_without_retrieval(
    fault: str, metadata_session: MagicMock
) -> None:
    tool = Tool(name="knowledge_base_retrieve", inputSchema={"type": "object"})
    tools = [tool]
    cursor = None
    if fault == "missing":
        tools = []
    elif fault == "duplicate":
        tools = [tool, tool]
    elif fault == "pagination":
        cursor = "private-page-token"
    elif fault == "invalid-schema":
        tool.inputSchema = {"required": ["private@example.invalid"]}
    else:
        tool.inputSchema = {
            "properties": {
                f"field{index}": {
                    "properties": {f"nested{nested}": {"type": "string"} for nested in range(32)}
                }
                for index in range(32)
            }
        }
    metadata_session.list_tools.return_value = ListToolsResult(tools=tools, nextCursor=cursor)
    with pytest.raises(KnowledgeCheckError) as caught:
        asyncio.run(inspect_tool_schema(config()))
    assert "private" not in str(caught.value)
    metadata_session.initialize.assert_awaited_once()
    metadata_session.list_tools.assert_awaited_once()
    metadata_session.call_tool.assert_not_called()
    metadata_session.send_request.assert_not_called()


@pytest.mark.parametrize("fault", ["timeout", "invalid-response", "nested-group", "programming"])
def test_metadata_errors_are_explicit_and_only_known_failures_are_sanitized(
    fault: str, metadata_session: MagicMock
) -> None:
    if fault == "invalid-response":
        with pytest.raises(ValidationError) as invalid:
            Tool.model_validate({"name": "knowledge_base_retrieve", "inputSchema": "private"})
        error: Exception = invalid.value
    elif fault == "nested-group":
        error = ExceptionGroup(
            "private transport wrapper",
            [ExceptionGroup("private nested wrapper", [TimeoutError("private provider data")])],
        )
    elif fault == "programming":
        error = ExceptionGroup("unexpected failure", [RuntimeError("programming defect")])
    else:
        error = TimeoutError("private provider data")
    metadata_session.list_tools.side_effect = error
    expected = ExceptionGroup if fault == "programming" else KnowledgeCheckError
    with pytest.raises(expected) as caught:
        asyncio.run(inspect_tool_schema(config()))
    if fault == "programming":
        assert caught.value is error
    else:
        assert "private" not in str(caught.value)
    metadata_session.list_tools.assert_awaited_once()
    metadata_session.call_tool.assert_not_called()


@pytest.mark.parametrize(
    "flags",
    [
        ["--approve-dev-check"],
        ["--approve-dev-check", "--version", "7"],
        ["--inspect-tool-schema", "--version", "7"],
        ["--inspect-tool-schema", "--principal-id", RECIPE[0]],
        ["--inspect-tool-schema", "--approve-dev-check"],
        ["--inspect-grounding-config", "--version", "7"],
        ["--inspect-grounding-config", "--inspect-tool-schema"],
        ["--approve-dev-search-diagnostic", "--version", "3"],
        ["--approve-dev-search-diagnostic", "--principal-id", RECIPE[0]],
        ["--approve-dev-search-diagnostic", "--approve-dev-check"],
        ["--approve-dev-search-diagnostic", "--inspect-grounding-config"],
    ],
)
def test_invalid_cli_mode_never_creates_an_administrative_client(
    flags: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_recipe_knowledge", *flags])
    project = Mock()
    credential = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", project)
    monkeypatch.setattr(check_recipe_knowledge, "AsyncAzureCliCredential", credential)
    with pytest.raises(SystemExit) as caught:
        check_recipe_knowledge.main()
    assert caught.value.code == 2
    project.assert_not_called()
    credential.assert_not_called()


@pytest.mark.parametrize("fault", ["none", "grounding", "semantic", "source", "models", "limits"])
def test_grounding_configuration_inspection_reads_only_three_metadata_definitions(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge = RecipeKnowledgeConfig.from_settings(config())
    index: dict[str, Any] = {
        "fields": [
            {"name": "grounding", "type": "Edm.String", "searchable": True, "retrievable": True}
        ],
        "semantic": {
            "configurations": [
                {
                    "name": "recipe-content",
                    "prioritizedFields": {"prioritizedContentFields": [{"fieldName": "grounding"}]},
                }
            ]
        },
    }
    source: dict[str, Any] = {
        "kind": "searchIndex",
        "searchIndexParameters": {
            "searchIndexName": knowledge.index_name,
            "semanticConfigurationName": "recipe-content",
        },
    }
    base: dict[str, Any] = {
        "knowledgeSources": [{"name": knowledge.knowledge_source_name}],
        "models": [],
        "retrievalReasoningEffort": {"kind": "minimal"},
        "outputMode": "extractiveData",
        "retrieveDefaults": {"maxOutputDocuments": 3, "maxOutputSizeInTokens": 5000},
    }
    if fault == "grounding":
        index["fields"] = []
    elif fault == "semantic":
        index["semantic"]["configurations"][0]["prioritizedFields"]["prioritizedContentFields"] = [
            {"fieldName": "private-content"}
        ]
    elif fault == "source":
        source["searchIndexParameters"]["searchIndexName"] = "private-index"
    elif fault == "models":
        base["models"] = [{"private": "provider-model"}]
    elif fault == "limits":
        base["retrieveDefaults"]["maxOutputDocuments"] = 200
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=AccessToken("synthetic-metadata-token", 1))
    monkeypatch.setattr(
        check_recipe_knowledge, "AsyncAzureCliCredential", Mock(return_value=credential)
    )
    http = MagicMock()
    http.get = AsyncMock(
        side_effect=[
            httpx.Response(
                200, json=value, request=httpx.Request("GET", "https://synthetic.invalid")
            )
            for value in (index, source, base)
        ]
    )
    http_factory = MagicMock()
    http_factory.return_value.__aenter__.return_value = http
    monkeypatch.setattr("sous_chef.check_recipe_knowledge.httpx.AsyncClient", http_factory)
    project, mcp = Mock(), Mock()
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", project)
    monkeypatch.setattr(check_recipe_knowledge, "streamable_http_client", mcp)
    metadata = asyncio.run(inspect_grounding_config(config(agents_enabled=False)))
    assert metadata["matches_expected_grounding_configuration"] == (fault == "none")
    assert "private" not in json.dumps(metadata)
    assert http.get.await_count == 3
    paths = [item.args[0].split("?")[0] for item in http.get.await_args_list]
    assert paths == [
        f"{knowledge.search_endpoint}/indexes/{knowledge.index_name}",
        f"{knowledge.search_endpoint}/knowledgesources/{knowledge.knowledge_source_name}",
        f"{knowledge.search_endpoint}/knowledgebases/{knowledge.knowledge_base_name}",
    ]
    http.post.assert_not_called()
    project.assert_not_called()
    mcp.assert_not_called()


def test_grounding_configuration_cli_never_enters_a_live_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_recipe_knowledge", "--inspect-grounding-config"])
    monkeypatch.setattr(
        check_recipe_knowledge, "load_settings", lambda: config(agents_enabled=False)
    )
    inspection = AsyncMock(return_value={"matches_expected_grounding_configuration": False})
    invocation = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "inspect_grounding_config", inspection)
    monkeypatch.setattr(check_recipe_knowledge, "check", invocation)
    assert check_recipe_knowledge.main() == 0
    assert "no model invocation or retrieval" in capsys.readouterr().out
    invocation.assert_not_called()


def test_checker_exposes_only_a_fixed_grounding_rejection_stage() -> None:
    harness, client = setup()
    data = response().model_dump()
    data["output"][0]["output"] = '[{"ref_id":"0","grounding":"private malformed recipe"}]'
    client.responses.create.return_value = Response.model_validate(data)
    with pytest.raises(KnowledgeCheckError) as caught:
        run(harness)
    assert "foundry_invalid_result; stage=recipe_schema" in str(caught.value)
    assert "private" not in str(caught.value)
    client.responses.create.assert_called_once()


def query_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["query_variants"],
        "properties": {"query_variants": {"type": "array", "items": {"type": "string"}}},
    }


@pytest.mark.parametrize(
    ("scope", "constraint", "supported"),
    [
        ("root", {"description": "private annotation"}, True),
        ("root", {"additionalProperties": False}, True),
        ("root", {"$ref": "https://private.invalid/schema"}, False),
        ("variants", {"minItems": 1, "maxItems": 1, "uniqueItems": True}, True),
        ("variants", {"minItems": 2}, False),
        ("variants", {"maxItems": 0}, False),
        ("variants", {"minItems": True}, False),
        ("variants", {"items": {"type": "number"}}, False),
        ("items", {"required": [], "properties": {}, "maxLength": 11}, True),
        ("items", {"minLength": 12}, False),
        ("items", {"maxLength": 10}, False),
        ("items", {"pattern": "private pattern"}, False),
    ],
)
def test_fixed_query_schema_rejects_unsupported_or_incompatible_constraints(
    scope: str,
    constraint: dict[str, Any],
    supported: bool,
) -> None:
    schema = query_schema()
    node = schema if scope == "root" else schema["properties"]["query_variants"]
    if scope == "items":
        node = node["items"]
    node.update(constraint)
    assert _fixed_query_schema(schema) is supported


@pytest.mark.parametrize(
    "fault", ["none", "configuration", "schema", "missing", "duplicate", "pagination", "timeout"]
)
def test_search_only_diagnostic_is_guarded_and_never_replays_a_tool_call(
    fault: str,
    metadata_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = AsyncMock(
        return_value={"matches_expected_grounding_configuration": fault != "configuration"}
    )
    monkeypatch.setattr(check_recipe_knowledge, "inspect_grounding_config", inspection)
    schema = query_schema()
    if fault == "schema":
        schema["properties"]["query_variants"]["minItems"] = 2
    tool = Tool(name="knowledge_base_retrieve", inputSchema=schema)
    metadata_session.list_tools.return_value = ListToolsResult(
        tools=[] if fault == "missing" else [tool, tool] if fault == "duplicate" else [tool],
        nextCursor="private-cursor" if fault == "pagination" else None,
    )
    metadata_session.send_request = AsyncMock(
        return_value=CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        [
                            {
                                "ref_id": "0",
                                "grounding": curated_excerpt(recipe_fixture()).model_dump_json(),
                            }
                        ]
                    ),
                )
            ]
        ),
        side_effect=TimeoutError("private timeout") if fault == "timeout" else None,
    )
    project = Mock()
    monkeypatch.setattr(check_recipe_knowledge, "AIProjectClient", project)
    if fault == "none":
        metadata = asyncio.run(diagnose_search_grounding(config(agents_enabled=False)))
        assert metadata["grounding_status"] == "available"
    else:
        with pytest.raises(KnowledgeCheckError) as caught:
            asyncio.run(diagnose_search_grounding(config(agents_enabled=False)))
        assert "private" not in str(caught.value)
    inspection.assert_awaited_once()
    if fault in {"none", "timeout"}:
        metadata_session.send_request.assert_awaited_once()
        request, result_type = metadata_session.send_request.await_args.args
        assert result_type is CallToolResult
        assert request.model_dump(mode="json", by_alias=True, exclude_none=True) == {
            "method": "tools/call",
            "params": {
                "name": "knowledge_base_retrieve",
                "arguments": {"query_variants": ["rice dinner"]},
            },
        }
        assert (
            metadata_session.send_request.await_args.kwargs[
                "request_read_timeout_seconds"
            ].total_seconds()
            == 15
        )
    else:
        metadata_session.send_request.assert_not_called()
    metadata_session.call_tool.assert_not_called()
    project.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["malformed-json", "object-grounding", "missing-source", "long-snippet"]
)
def test_search_diagnostics_do_not_print_untrusted_recipe_values(
    kind: str,
    metadata_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_recipe_knowledge", "--approve-dev-search-diagnostic"])
    monkeypatch.setattr(
        check_recipe_knowledge, "load_settings", lambda: config(agents_enabled=False)
    )
    monkeypatch.setattr(
        check_recipe_knowledge,
        "inspect_grounding_config",
        AsyncMock(return_value={"matches_expected_grounding_configuration": True}),
    )
    metadata_session.list_tools.return_value = ListToolsResult(
        tools=[Tool(name="knowledge_base_retrieve", inputSchema=query_schema())]
    )
    recipe = json.loads(curated_excerpt(recipe_fixture()).model_dump_json())
    if kind == "missing-source":
        recipe.pop("source")
        recipe["private-field"] = "private secret"
    elif kind == "long-snippet":
        recipe["snippet"] = "private recipe " * 120
    raw: object = (
        "private malformed JSON"
        if kind == "malformed-json"
        else recipe
        if kind == "object-grounding"
        else json.dumps(recipe)
    )
    metadata_session.send_request = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text=json.dumps([{"ref_id": "0", "grounding": raw}]))]
        )
    )
    assert check_recipe_knowledge.main() == 1
    output = capsys.readouterr().out
    assert "private" not in output
    assert '"grounding_status": "rejected"' in output and '"stage":' in output
    assert "No Foundry agent/GPT invocation" in output
    metadata_session.send_request.assert_awaited_once()


@pytest.mark.parametrize("length", [16000, 16001, 65536, 65537])
def test_search_diagnostic_size_cutoffs_preserve_acceptance_limits(length: int) -> None:
    output = "x" * length
    metadata = _grounding_diagnostics(output, RecipeKnowledgeConfig.from_settings(config()))
    assert metadata["output_characters"] == length
    assert ("output_tokens" in metadata) == (length <= 65536)
    assert metadata["stage"] == ("output_json" if length == 16000 else "output_size")
    assert ("content_json_valid" in metadata) == (length == 16000)


@pytest.mark.parametrize("kind", ["empty", "many", "tool-error", "invalid-content"])
def test_native_mcp_diagnostics_report_only_bounded_structural_metadata(kind: str) -> None:
    chunks = [] if kind == "empty" else [{"ref_id": 0, "content": "private text"}]
    if kind == "many":
        chunks *= 4
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(chunks))],
        isError=kind == "tool-error",
        structuredContent={"private-key": "private value"},
    )
    metadata = _grounding_diagnostics(
        result.model_dump_json(by_alias=True, exclude_none=True),
        RecipeKnowledgeConfig.from_settings(config()),
    )
    assert "private" not in json.dumps(metadata)
    assert metadata["content_json_valid"] is True
    assert metadata["chunk_count"] == len(chunks)
    shapes = metadata["chunk_shapes"]
    assert isinstance(shapes, list) and len(shapes) <= 3
    if shapes:
        assert shapes[0]["grounding_present"] is False
        assert shapes[0]["ref_id_type"] == "number"
        assert shapes[0]["selected_payload_field"] == "content"
    assert (
        metadata.get("stage")
        == {
            "empty": None,
            "many": "chunk_count",
            "tool-error": "mcp_tool_error",
            "invalid-content": "recipe_schema",
        }[kind]
    )


@pytest.mark.parametrize(
    "fault", ["none", "http-error", "redirect", "output-schema", "multi-block", "expanded-valid"]
)
def test_real_mcp_sdk_sends_exactly_one_fixed_search_call(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=AccessToken("synthetic-metadata-token", 1))
    monkeypatch.setattr(
        check_recipe_knowledge, "AsyncAzureCliCredential", Mock(return_value=credential)
    )
    monkeypatch.setattr(
        check_recipe_knowledge,
        "inspect_grounding_config",
        AsyncMock(return_value={"matches_expected_grounding_configuration": True}),
    )
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        payload = json.loads(request.content)
        requests.append(payload)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-search", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "knowledge_base_retrieve", "inputSchema": query_schema()}]}
            if fault == "output-schema":
                result["tools"][0]["outputSchema"] = {"type": "array"}
        else:
            assert method == "tools/call"
            if fault == "http-error":
                return httpx.Response(500, text="private provider response")
            if fault == "redirect":
                return httpx.Response(
                    307, headers={"Location": str(request.url.copy_with(path="/replayed"))}
                )
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            [
                                {
                                    "ref_id": "0",
                                    "grounding": curated_excerpt(
                                        recipe_fixture()
                                    ).model_dump_json(),
                                }
                            ]
                        ),
                    }
                ]
            }
            if fault == "output-schema":
                result["structuredContent"] = {"private-key": "private value"}
                result["content"][0]["text"] = '[{"ref_id":"0","grounding":"private malformed"}]'
            if fault == "multi-block":
                result["content"].extend(
                    [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "references": [
                                        {"sourceData": {"recipe_json": "private payload " * 1500}}
                                    ]
                                }
                            ),
                        },
                        {"type": "text", "text": "private non-JSON text"},
                        {"type": "text", "text": '{"private-key":"private value"}'},
                    ]
                )
            if fault == "expanded-valid":
                document = search_document(recipe_fixture())
                result["content"] = [
                    {
                        "type": "text",
                        "text": json.dumps(
                            [
                                {
                                    "ref_id": 0,
                                    "content": document["grounding"],
                                }
                            ]
                        ),
                    },
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ref_id": 0,
                                "sourceData": {
                                    name: document[name]
                                    for name in (
                                        "grounding",
                                        "source_id",
                                        "title",
                                        "source_url",
                                        "recipe_json",
                                    )
                                },
                                "private-metadata": "private padding " * 1500,
                            }
                        ),
                    },
                ]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "sous_chef.check_recipe_knowledge.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs),
    )
    if fault in {"http-error", "redirect"}:
        with pytest.raises(KnowledgeCheckError) as caught:
            asyncio.run(diagnose_search_grounding(config(agents_enabled=False)))
        assert "private" not in str(caught.value)
    else:
        metadata = asyncio.run(diagnose_search_grounding(config(agents_enabled=False)))
        assert metadata["grounding_status"] == (
            "rejected" if fault in {"output-schema", "multi-block"} else "available"
        )
        assert metadata["mcp_output_schema_present"] == (fault == "output-schema")
        assert metadata["diagnostic_budget"] == {
            "max_output_characters": 65536,
            "max_context_tokens": 16000,
        }
        assert "private" not in json.dumps(metadata)
        if fault == "multi-block":
            assert metadata["stage"] == "reference_count"
            runtime = metadata["runtime_acceptance"]
            assert isinstance(runtime, dict) and runtime["stage"] == "reference_count"
            assert metadata["content_items"] == 4
            blocks = metadata["content_block_diagnostics"]
            assert isinstance(blocks, list) and len(blocks) == 4
            assert blocks[0]["isolated_block_check"]["grounding_status"] == "available"
            assert blocks[1]["text_json_valid"] is True
            assert (
                blocks[1]["text_json_shape"]["known_fields"]["references"]["sample_shapes"][0][
                    "known_fields"
                ]["sourceData"]["known_fields"]["recipe_json"]["characters"]
                == 24000
            )
            assert blocks[2]["text_json_valid"] is False
            assert blocks[3]["text_json_shape"]["known_fields"] == {}
        if fault == "expanded-valid":
            runtime = metadata["runtime_acceptance"]
            assert isinstance(runtime, dict)
            assert runtime["grounding_status"] == "available" and "stage" not in runtime
            assert runtime["max_output_characters"] == 65536
            assert runtime["max_context_tokens"] == 16000
    calls = [request for request in requests if request["method"] == "tools/call"]
    assert len(calls) == 1
    assert calls[0]["params"] == {
        "name": "knowledge_base_retrieve",
        "arguments": {"query_variants": ["rice dinner"]},
    }


@pytest.mark.parametrize("fault", ["budget", "blocks", "shape"])
def test_content_block_metadata_is_bounded_without_relaxing_acceptance(fault: str) -> None:
    text = '"private"'
    if fault == "budget":
        text = json.dumps("x" * 65534)
    elif fault == "shape":
        value: object = "private value"
        for _ in range(4):
            value = {name: value for name in ("text", "type", "source", "snippet")}
        text = json.dumps(value)
    result = CallToolResult(
        content=[
            TextContent(type="text", text=text),
            *[TextContent(type="text", text='"private"') for _ in range(4)],
        ]
    )
    metadata = _content_block_diagnostics(result, RecipeKnowledgeConfig.from_settings(config()))
    assert len(metadata) == 4
    assert "private" not in json.dumps(metadata)
    if fault == "budget":
        assert metadata[0]["text_json_valid"] is True
        assert metadata[1]["inspection_omitted"] is True
        assert "isolated_block_check" not in metadata[1]
    elif fault == "shape":
        assert metadata[0]["text_json_shape"] == {"type": "object", "shape_omitted": True}
    assert (
        _grounding_diagnostics(
            result.model_dump_json(by_alias=True, exclude_none=True),
            RecipeKnowledgeConfig.from_settings(config()),
        )["grounding_status"]
        == "rejected"
    )
