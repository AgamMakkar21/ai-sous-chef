import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from azure.ai.projects import AIProjectClient
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.orchestrator import CoordinationPrompt, CoordinationRequest
from sous_chef.publish_orchestrator import publish
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry
from test_agent_identity import APP, ORCH, RECIPE
from test_foundry_gateway import Harness, response_body, settings


def function_output(
    name: str = "recipe_specialist", arguments: str | None = None
) -> dict[str, Any]:
    return {
        "id": "fc_synthetic",
        "type": "function_call",
        "call_id": "call_synthetic",
        "name": name,
        "arguments": arguments
        or json.dumps(
            {
                "requirements": "A mild rice dinner",
                "time_budget_minutes": 30,
                "use_web": False,
            }
        ),
        "status": "completed",
    }


def body(*output: dict[str, Any]) -> dict[str, Any]:
    return response_body(
        output=list(output),
        usage={
            "input_tokens": 400,
            "output_tokens": 200,
            "total_tokens": 600,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 100},
        },
    )


def request(**changes: Any) -> CoordinationRequest:
    return CoordinationRequest(
        request=CoordinationPrompt(
            role=AgentRole.ORCHESTRATOR, prompt="private bounded cooking context"
        ),
        **changes,
    )


def coordination_settings() -> Settings:
    return settings(
        foundry_orchestrator_agent_principal_id=ORCH[0],
        foundry_recipe_nutrition_agent_principal_id=RECIPE[0],
        app_workload_principal_id=APP,
    )


def test_native_function_handoff_stateless_result_and_identity_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    harness = Harness(body=body(function_output()))
    config = coordination_settings()
    gateway = FoundryGateway(config, client_factory=harness.factory)
    initial = request()
    step = asyncio.run(gateway.coordinate(initial))
    assert step.call is not None
    assert step.call.arguments.time_budget_minutes == 30
    assert step.usage.output_tokens == 200  # Includes the 100 reasoning tokens.
    first = json.loads(harness.requests[0].content)
    assert first["agent_reference"] == {
        "type": "agent_reference",
        "name": "orchestrator-agent",
        "version": "2",
    }
    assert "model" not in first
    assert first["store"] is False and first["parallel_tool_calls"] is False
    assert [tool["name"] for tool in first["tools"]] == ["recipe_specialist"]
    result = json.dumps({"status": "candidate", "candidate": {"title": "Synthetic"}})
    continuation = CoordinationRequest(
        request=initial.request, previous_call=step.call, tool_output=result
    )
    asyncio.run(gateway.coordinate(continuation))
    second = json.loads(harness.requests[1].content)
    assert second["input"] == [
        {"role": "user", "content": initial.request.prompt},
        {
            "type": "function_call",
            "call_id": step.call.call_id,
            "name": "recipe_specialist",
            "arguments": step.call.arguments.model_dump_json(),
        },
        {"type": "function_call_output", "call_id": step.call.call_id, "output": result},
    ]
    assert "previous_response_id" not in second and "conversation" not in second
    assert "private" not in caplog.text and "Synthetic" not in caplog.text
    events = [json.loads(record.getMessage()) for record in caplog.records]
    event = next(item for item in events if item["event"] == "orchestrator_invoked")
    assert event["agent_role"] == "orchestrator"
    assert event["agent_principal_id"] == ORCH[0]
    assert event["agent_name"] == "orchestrator-agent" and event["agent_version"] == "2"
    assert event["trace_id"] == initial.request.operation_id.hex


def test_structured_clarification_is_not_a_function_call() -> None:
    message = response_body()["output"][0]
    message["content"][0]["text"] = json.dumps(
        {"status": "clarification", "explanation": "Which recipe did you mean?"}
    )
    harness = Harness(body=body(message))
    gateway = FoundryGateway(
        coordination_settings(),
        client_factory=harness.factory,
    )
    result = asyncio.run(gateway.coordinate(request()))
    assert result.call is None and result.answer is not None
    assert result.answer.status == "clarification"


def test_opaque_reasoning_is_bounded_and_round_tripped_without_plaintext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    reasoning = {
        "id": "rs_synthetic",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "private hidden summary"}],
        "encrypted_content": "opaque-encrypted-state",
    }
    harness = Harness(body=body(reasoning, function_output()))
    gateway = FoundryGateway(coordination_settings(), client_factory=harness.factory)
    initial = request()
    step = asyncio.run(gateway.coordinate(initial))
    assert step.call is not None
    assert "opaque-encrypted-state" not in repr(step.call)
    asyncio.run(
        gateway.coordinate(
            CoordinationRequest(
                request=initial.request,
                previous_call=step.call,
                tool_output='{"status":"candidate"}',
            )
        )
    )
    second = json.loads(harness.requests[1].content)
    assert second["input"][1] == {
        "id": "rs_synthetic",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-encrypted-state",
    }
    assert "private hidden summary" not in json.dumps(second)
    assert "opaque-encrypted-state" not in caplog.text
    assert "private hidden summary" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        body(function_output("cosmos_read")),
        body(function_output(arguments='{"requirements":"private"}')),
        body(function_output(arguments="private-invalid-json")),
        body(function_output(), function_output()),
        body({**function_output(), "status": "in_progress"}),
        body(),
        body(
            {"id": "rs_synthetic", "type": "reasoning", "summary": []},
            function_output(),
        ),
        response_body(output=[function_output()]),  # No provider usage.
        body(
            function_output(
                arguments=json.dumps(
                    {"requirements": "x", "time_budget_minutes": 0, "use_web": False}
                )
            )
        ),
    ],
)
def test_invalid_or_unaccounted_actions_fail_closed(response: dict[str, Any]) -> None:
    harness = Harness(body=response)
    gateway = FoundryGateway(
        coordination_settings(),
        client_factory=harness.factory,
    )
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.coordinate(request()))
    assert caught.value.code == FoundryFailure.INVALID_RESULT
    assert len(harness.requests) == 1
    assert "private" not in str(caught.value)


def test_coordination_requires_attribution_and_preflights_input_budget() -> None:
    harness = Harness(body=body(function_output()))
    gateway = FoundryGateway(settings(), client_factory=harness.factory)
    with pytest.raises(ConfigurationError):
        asyncio.run(gateway.coordinate(request()))
    gateway = FoundryGateway(
        coordination_settings(),
        client_factory=harness.factory,
    )
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(gateway.coordinate(request(max_input_tokens=1)))
    assert caught.value.code == FoundryFailure.REJECTED
    assert harness.requests == []


def test_large_character_context_is_allowed_only_within_the_token_limit() -> None:
    harness = Harness(body=body(function_output()))
    gateway = FoundryGateway(coordination_settings(), client_factory=harness.factory)
    initial = CoordinationRequest(
        request=CoordinationPrompt(role=AgentRole.ORCHESTRATOR, prompt="rice " * 4000)
    )
    asyncio.run(gateway.coordinate(initial))
    assert len(json.loads(harness.requests[0].content)["input"]) > 16000
    with pytest.raises(FoundryGatewayError):
        asyncio.run(gateway.coordinate(request(max_input_tokens=100)))
    assert len(harness.requests) == 1


def test_aggregate_function_output_is_bounded_before_invocation() -> None:
    harness = Harness(body=body(function_output()))
    gateway = FoundryGateway(coordination_settings(), client_factory=harness.factory)
    initial = request()
    step = asyncio.run(gateway.coordinate(initial))
    assert step.call is not None
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(
            gateway.coordinate(
                CoordinationRequest(
                    request=initial.request,
                    previous_call=step.call,
                    tool_output="a " * 4001,
                )
            )
        )
    assert caught.value.code == FoundryFailure.REJECTED
    assert len(harness.requests) == 1


def project() -> Mock:
    client = Mock(spec=AIProjectClient, agents=Mock())
    identities = {
        "orchestrator-agent": ORCH,
        "recipe-agent": RECIPE,
    }
    client.agents.get.side_effect = lambda name: SimpleNamespace(
        instance_identity=SimpleNamespace(
            principal_id=identities[name][0], client_id=identities[name][1]
        ),
        state="enabled",
    )
    client.agents.create_version.return_value = SimpleNamespace(version="7")
    return client


def test_publication_creates_reproducible_source_version_under_existing_identity() -> None:
    client = project()
    config = settings(app_workload_principal_id=APP)
    assert publish(config, client) == "7"
    invocation = client.agents.create_version.call_args
    assert invocation.kwargs["agent_name"] == "orchestrator-agent"
    definition = invocation.kwargs["definition"].as_dict()
    assert definition["model"] == config.foundry_orchestrator_deployment
    assert definition["tools"][0]["name"] == "recipe_specialist"
    assert invocation.kwargs["metadata"]["asc_policy_version"] == "1"
    client.agents.create.assert_not_called()
    assert config.agents_enabled  # No settings or release manifest mutation.


@pytest.mark.parametrize("environment", ["local", "prod"])
def test_publication_refuses_non_dev_targets(environment: str) -> None:
    config = settings(environment=environment)
    client = project()
    with pytest.raises(ConfigurationError):
        publish(config, client)
    client.agents.create_version.assert_not_called()


def test_publication_refuses_shared_or_disabled_identities() -> None:
    client = project()
    client.agents.get.side_effect = lambda name: SimpleNamespace(
        instance_identity=SimpleNamespace(principal_id=ORCH[0], client_id=ORCH[1]),
        state="enabled",
    )
    with pytest.raises(IdentityError):
        publish(settings(app_workload_principal_id=APP), client)
    client.agents.create_version.assert_not_called()
    client = project()
    client.agents.get.side_effect = lambda name: SimpleNamespace(
        instance_identity=SimpleNamespace(
            principal_id=ORCH[0] if name == "orchestrator-agent" else RECIPE[0],
            client_id=ORCH[1] if name == "orchestrator-agent" else RECIPE[1],
        ),
        state="disabled",
    )
    with pytest.raises(IdentityError):
        publish(settings(app_workload_principal_id=APP), client)
    client.agents.create_version.assert_not_called()
