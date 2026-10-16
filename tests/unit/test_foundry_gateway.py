import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from openai import AsyncOpenAI
from pydantic import ValidationError
from sous_chef.adapters import foundry_gateway
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.check_foundry_gateway import check
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import (
    FoundryFailure,
    FoundryGatewayError,
    FoundryRequest,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry, trace_id


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "dev",
        "adapter_mode": "live",
        "cosmos_endpoint": "https://cosmos.example.invalid",
        "cosmos_database": "test",
        "foundry_project_endpoint": "https://project.example.invalid/api/projects/test",
        "foundry_orchestrator_deployment": "orchestrator-model",
        "foundry_recipe_nutrition_deployment": "recipe-model",
        "foundry_extraction_deployment": "extraction-model",
        "foundry_orchestrator_agent_name": "orchestrator-agent",
        "foundry_recipe_nutrition_agent_name": "recipe-agent",
        "foundry_orchestrator_agent_version": "2",
        "foundry_recipe_nutrition_agent_version": "3",
    }
    values.update(overrides)
    return Settings(**values)


def response_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_synthetic",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "managed-agent-model",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ready", "annotations": []}],
            }
        ],
    }
    body.update(overrides)
    return body


class Harness:
    def __init__(
        self, status: int = 200, body: Any = None, *, hang: bool = False, fault: str = ""
    ) -> None:
        self.status = status
        self.body = response_body() if body is None else body
        self.hang = hang
        self.fault = fault
        self.requests: list[httpx2.Request] = []
        self.closed = False
        self.cancelled = False
        self.client_closed = False

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fault == "timeout":
            raise httpx2.ReadTimeout("private-timeout", request=request)
        if self.fault == "connection":
            raise httpx2.ConnectError("private-connection", request=request)
        if self.fault == "invalid_json":
            return httpx2.Response(
                200, content="private-not-json", headers={"content-type": "application/json"}
            )
        try:
            if self.hang:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return httpx2.Response(self.status, json=self.body)

    @asynccontextmanager
    async def factory(self, config: Settings) -> AsyncIterator[AsyncOpenAI]:
        client = AsyncOpenAI(
            api_key="synthetic",
            base_url=str(config.foundry_project_endpoint) + "/openai/v1",
            max_retries=0,
            _strict_response_validation=True,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)),
        )
        try:
            async with client:
                yield client
        finally:
            self.closed = True
            self.client_closed = client.is_closed()


@pytest.mark.parametrize("role", list(AgentRole))
def test_pinned_managed_agent_request_and_typed_result(role: AgentRole) -> None:
    harness = Harness()
    request = FoundryRequest(role=role, prompt="synthetic private prompt", max_output_tokens=100)
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    previous_trace = trace_id.get()
    result = asyncio.run(gateway.invoke(request))
    assert result.text == "ready"
    assert result.operation_id == request.operation_id
    assert result.role == role
    assert result.response_id == "resp_synthetic"
    assert trace_id.get() == previous_trace
    call = harness.requests[0]
    body = json.loads(call.content)
    assert body == {
        "input": request.prompt,
        "max_output_tokens": 100,
        "agent_reference": {
            "type": "agent_reference",
            "name": "orchestrator-agent" if role == AgentRole.ORCHESTRATOR else "recipe-agent",
            "version": "2" if role == AgentRole.ORCHESTRATOR else "3",
        },
    }
    assert call.headers["X-Client-Request-Id"] == str(request.operation_id)
    assert call.url.path.endswith("/api/projects/test/openai/v1/responses")
    assert harness.closed and harness.client_closed
    assert "private" not in repr(request)
    assert "ready" not in repr(result)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, FoundryFailure.AUTHENTICATION),
        (403, FoundryFailure.AUTHENTICATION),
        (429, FoundryFailure.THROTTLED),
        (408, FoundryFailure.TIMEOUT),
        (504, FoundryFailure.TIMEOUT),
        (500, FoundryFailure.UNAVAILABLE),
        (503, FoundryFailure.UNAVAILABLE),
        (400, FoundryFailure.REJECTED),
        (404, FoundryFailure.REJECTED),
    ],
)
def test_http_failures_are_distinct_safe_and_never_retried(
    status: int, code: FoundryFailure
) -> None:
    harness = Harness(status, {"error": {"message": "secret-private-service-detail"}})
    request = FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="secret-private-prompt")
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(request))
    assert caught.value.code == code
    assert caught.value.operation_id == request.operation_id
    assert caught.value.__cause__ is None
    assert "secret-private" not in str(caught.value)
    assert len(harness.requests) == 1
    assert harness.closed and harness.client_closed


@pytest.mark.parametrize(
    "body",
    [
        response_body(status="failed", error={"code": "server_error", "message": "private"}),
        response_body(status="incomplete"),
        response_body(status="cancelled"),
        response_body(status="queued"),
        response_body(error={"code": "server_error", "message": "private-error"}),
        response_body(incomplete_details={"reason": "max_output_tokens"}),
        response_body(output=[]),
        {},
        "not-a-response-object",
        response_body(output=None),
        response_body(id=""),
        response_body(id="x" * 257),
        response_body(output=[{"type": "message", "content": []}]),
        response_body(
            output=[
                {
                    "id": "msg_synthetic",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "refusal", "refusal": "private-refusal"},
                        {"type": "output_text", "text": "partial", "annotations": []},
                    ],
                }
            ]
        ),
        response_body(
            output=[
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "   ", "annotations": []}],
                }
            ]
        ),
    ],
)
def test_invalid_or_unfinished_results_never_look_successful(body: dict[str, Any]) -> None:
    harness = Harness(body=body)
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")))
    assert caught.value.code == FoundryFailure.INVALID_RESULT
    assert len(harness.requests) == 1


def test_total_deadline_cancels_io_and_closes_clients() -> None:
    harness = Harness(hang=True)
    gateway = FoundryGateway(
        settings(foundry_invocation_timeout_seconds=0.02), client_factory=harness.factory
    )

    async def run() -> None:
        started = asyncio.get_running_loop().time()
        with pytest.raises(FoundryGatewayError) as caught:
            await gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello"))
        assert caught.value.code == FoundryFailure.TIMEOUT
        assert asyncio.get_running_loop().time() - started < 0.5

    asyncio.run(run())
    assert harness.cancelled and harness.closed and harness.client_closed
    assert len(harness.requests) == 1


def test_caller_cancellation_propagates_and_closes_io_without_retry() -> None:
    harness = Harness(hang=True)
    gateway = FoundryGateway(settings(), client_factory=harness.factory)

    async def run() -> None:
        task = asyncio.create_task(
            gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello"))
        )
        async with asyncio.timeout(1):
            while not harness.requests:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert harness.cancelled and harness.closed and harness.client_closed
    assert len(harness.requests) == 1


def test_credential_failure_is_safe_and_does_not_invoke_agent() -> None:
    @asynccontextmanager
    async def failing_factory(config: Settings) -> AsyncIterator[AsyncOpenAI]:
        raise ClientAuthenticationError("private token details")
        yield  # pragma: no cover

    gateway = FoundryGateway(settings(), client_factory=failing_factory)
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")))
    assert caught.value.code == FoundryFailure.AUTHENTICATION
    assert "private" not in str(caught.value)


def test_real_sdk_factory_uses_project_token_scope_and_disables_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    scopes: list[str] = []
    closed: list[bool] = []

    class Credential:
        async def __aenter__(self) -> "Credential":
            return self

        async def __aexit__(self, *args: Any) -> None:
            closed.append(True)

        async def get_token(self, *requested_scopes: str, **kwargs: Any) -> AccessToken:
            scopes.extend(requested_scopes)
            return AccessToken("synthetic-token", 9999999999)

    monkeypatch.setattr(foundry_gateway, "build_async_credential", lambda _: Credential())
    original = AIProjectClient.get_openai_client
    options: list[dict[str, Any]] = []

    def get_client(project: Any, **kwargs: Any) -> AsyncOpenAI:
        options.append(kwargs.copy())
        kwargs["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(harness.handle))
        return original(project, **kwargs)

    monkeypatch.setattr(AIProjectClient, "get_openai_client", get_client)
    result = asyncio.run(
        FoundryGateway(settings()).invoke(
            FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")
        )
    )
    assert result.text == "ready"
    assert scopes == ["https://ai.azure.com/.default"]
    assert harness.requests[0].headers["Authorization"] == "Bearer synthetic-token"
    assert len(harness.requests) == 1
    assert options == [{"max_retries": 0, "timeout": 60, "_strict_response_validation": True}]
    assert closed == [True]


@pytest.mark.parametrize(
    "changes",
    [
        {"foundry_orchestrator_agent_name": None},
        {"foundry_recipe_nutrition_agent_version": None},
        {"foundry_recipe_nutrition_agent_name": "orchestrator-agent"},
        {"environment": "local", "adapter_mode": "local"},
        {"agents_enabled": False},
    ],
)
def test_gateway_configuration_fails_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        FoundryGateway(settings(**changes))


@pytest.mark.parametrize("seconds", [0, -1, 121, float("inf"), float("nan")])
def test_deadlines_must_be_positive_finite_and_bounded(seconds: float) -> None:
    with pytest.raises(ValidationError):
        settings(foundry_invocation_timeout_seconds=seconds)


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": ""},
        {"prompt": " "},
        {"prompt": "x" * 16001},
        {"max_output_tokens": 0},
        {"max_output_tokens": 8193},
        {"max_output_tokens": True},
        {"operation_id": "private-client-value"},
    ],
)
def test_invalid_request_rejected_before_invocation(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {"role": AgentRole.ORCHESTRATOR, "prompt": "hello"}
    values.update(changes)
    with pytest.raises(ValidationError):
        FoundryRequest(**values)


def test_failures_log_only_safe_category_and_operation_id(caplog: pytest.LogCaptureFixture) -> None:
    configure_safe_telemetry()
    request = FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="private-prompt")
    harness = Harness(429, {"error": {"message": "private-service-details"}})
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(FoundryGatewayError):
        asyncio.run(gateway.invoke(request))
    events = [json.loads(record.message) for record in caplog.records]
    failed = next(event for event in events if event["event"] == "foundry_failed")
    assert failed["error_category"] == "foundry_throttled"
    assert failed["trace_id"] == request.operation_id.hex
    assert "private" not in caplog.text
    assert "example.invalid" not in caplog.text


def test_operation_ids_are_independent_by_default() -> None:
    first = FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")
    second = FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")
    assert first.operation_id != second.operation_id
    pinned = uuid4()
    assert (
        FoundryRequest(
            role=AgentRole.ORCHESTRATOR, prompt="hello", operation_id=pinned
        ).operation_id
        == pinned
    )


@pytest.mark.parametrize(
    ("fault", "code"),
    [("connection", FoundryFailure.UNAVAILABLE), ("timeout", FoundryFailure.TIMEOUT)],
)
def test_network_failures_are_safe_and_not_retried(fault: str, code: FoundryFailure) -> None:
    harness = Harness(fault=fault)
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")))
    assert caught.value.code == code
    assert len(harness.requests) == 1
    assert harness.client_closed
    assert "private" not in str(caught.value)


def test_malformed_json_is_an_invalid_result() -> None:
    harness = Harness(fault="invalid_json")
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")))
    assert caught.value.code == FoundryFailure.INVALID_RESULT
    assert "private" not in str(caught.value)
    assert len(harness.requests) == 1


def test_deadline_includes_client_and_credential_setup() -> None:
    cleaned: list[bool] = []

    @asynccontextmanager
    async def slow_factory(config: Settings) -> AsyncIterator[AsyncOpenAI]:
        try:
            await asyncio.Event().wait()
            raise AssertionError("Client setup should have been cancelled.")
            yield  # pragma: no cover
        finally:
            cleaned.append(True)

    gateway = FoundryGateway(
        settings(foundry_invocation_timeout_seconds=0.02), client_factory=slow_factory
    )
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(FoundryRequest(role=AgentRole.ORCHESTRATOR, prompt="hello")))
    assert caught.value.code == FoundryFailure.TIMEOUT
    assert cleaned == [True]


def test_largest_allowed_request_and_output_bound() -> None:
    request = FoundryRequest(
        role=AgentRole.ORCHESTRATOR, prompt="x" * 16000, max_output_tokens=8192
    )
    body = response_body()
    body["output"][0]["content"][0]["text"] = "x" * 131072
    harness = Harness(body=body)
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    assert len(asyncio.run(gateway.invoke(request)).text) == 131072
    body["output"][0]["content"][0]["text"] += "x"
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.invoke(request))
    assert caught.value.code == FoundryFailure.INVALID_RESULT


@pytest.mark.parametrize(
    ("environment", "approval"), [("dev", None), ("local", "1"), ("prod", "1")]
)
def test_smoke_refuses_unapproved_or_nondev_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: str,
    approval: str | None,
) -> None:
    config = settings(environment=environment)
    monkeypatch.setattr("sous_chef.check_foundry_gateway.load_settings", lambda: config)
    if approval is None:
        monkeypatch.delenv("ASC_APPROVE_FOUNDRY_SMOKE", raising=False)
    else:
        monkeypatch.setenv("ASC_APPROVE_FOUNDRY_SMOKE", approval)
    assert asyncio.run(check()) == 1
    assert "FAILED" in capsys.readouterr().out


def test_approved_smoke_calls_both_agents_without_printing_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = settings()
    harness = Harness()
    gateway = FoundryGateway(config, client_factory=harness.factory)
    monkeypatch.setattr("sous_chef.check_foundry_gateway.load_settings", lambda: config)
    monkeypatch.setattr("sous_chef.check_foundry_gateway.FoundryGateway", lambda _: gateway)
    monkeypatch.setenv("ASC_APPROVE_FOUNDRY_SMOKE", "1")
    assert asyncio.run(check()) == 0
    output = capsys.readouterr().out
    assert "OK orchestrator" in output and "OK recipe_nutrition" in output
    assert "ready" not in output and "example.invalid" not in output
    assert len(harness.requests) == 2
