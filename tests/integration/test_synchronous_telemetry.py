"""Real application/SDK composition, with synthetic transports and in-memory telemetry."""

import asyncio
import json
import platform
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from azure.core.pipeline.transport import AsyncHttpResponse, HttpRequest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sous_chef.adapters import cosmos, credentials
from sous_chef.app import create_app
from sous_chef.domain.cosmos import CosmosError, HistoryQuery, PageRequest
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry, trace_id
from sous_chef.tracing import Operation, Telemetry, operation
from test_chat_api import send
from test_conversation_api import conversation_settings as conversation_settings
from test_conversation_api import create
from test_cosmos_repository import ALICE, RECIPES, SyntheticCredential, Transport, recipe, settings
from test_cosmos_repository import harness as harness
from test_grocery_api import add
from test_profile_api import onboard
from test_recipe_chat import RecipeChatHarness
from test_recipe_chat import recipe_settings as recipe_settings
from test_recipe_chat import recipe_transport as recipe_transport

CANARY = "TRACE_PRIVATE_CANARY@example.invalid"


@pytest.fixture
def capture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader]]:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    resource = Resource({"service.name": "synthetic-acceptance"})
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    meters = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
    telemetry = Telemetry(provider, meters)
    yield telemetry, exporter, reader
    telemetry.shutdown()


def test_request_spans_correlate_real_agent_tools_cosmos_and_persistence(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    harness: tuple[Transport, SyntheticCredential],
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry, exporter, reader = capture
    transport = harness[0]
    original_send = transport.send

    async def charged(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        response = await original_send(request, **kwargs)
        response.headers["x-ms-request-charge"] = "2.5"
        response.headers["x-ms-activity-id"] = str(uuid4())
        return response

    monkeypatch.setattr(transport, "send", charged)
    with TestClient(
        create_app(recipe_settings, telemetry=telemetry), headers=auth_headers
    ) as client:
        onboard(client)
        add(client, name="rice", quantity=100, unit="g")
        conversation = create(client)
        client.headers.update(
            {
                "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                "tracestate": f"private={CANARY}",
                "baggage": f"private={CANARY}",
                "X-Trace-ID": CANARY,
            }
        )
        exporter.clear()
        response = send(client, conversation, content=f"Make a 30-minute pantry recipe. {CANARY}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "completed"
        spans = exporter.get_finished_spans()
        by_name = {span.name: span for span in spans}
        assert {
            Operation.HTTP,
            Operation.CONTEXT,
            Operation.ORCHESTRATOR,
            Operation.SPECIALIST,
            Operation.FOUNDRY,
            Operation.USDA,
            Operation.USDA_SEARCH,
            Operation.USDA_PROFILE,
            Operation.SAFETY,
            Operation.PERSISTENCE,
            Operation.COSMOS,
        } <= by_name.keys()
        root = by_name[Operation.HTTP]
        assert root.kind == SpanKind.SERVER and root.parent is None
        assert root.context is not None
        assert root.context.trace_id != int("1" * 32, 16)
        assert root.attributes is not None
        assert root.attributes["asc.correlation_id"] == response.headers["x-trace-id"]
        route = root.attributes["http.route"]
        assert route == "/api/v1/conversations/{conversation_id}/turns"
        ids = {span.context.span_id for span in spans if span.context is not None}
        for span in spans:
            assert span.context is not None and span.context.trace_id == root.context.trace_id
            assert span.attributes is not None
            assert span.attributes["asc.correlation_id"] == response.headers["x-trace-id"]
            if span is not root:
                assert span.parent is not None and span.parent.span_id in ids
                assert root.start_time is not None and span.start_time is not None
                assert span.end_time is not None and root.end_time is not None
                assert root.start_time <= span.start_time <= span.end_time <= root.end_time
        parent = by_name[Operation.USDA].parent
        specialist = by_name[Operation.SPECIALIST].context
        assert (
            parent is not None and specialist is not None and parent.span_id == specialist.span_id
        )
        specialist_attributes = by_name[Operation.SPECIALIST].attributes
        assert (
            specialist_attributes is not None and specialist_attributes["asc.web.enabled"] is False
        )
        assert "web_iq.skipped" in {event.name for event in by_name[Operation.SPECIALIST].events}
        foundry = [span for span in spans if span.name == Operation.FOUNDRY]
        assert len(foundry) == len(recipe_transport.requests) == 3
        for request in recipe_transport.requests:
            context = TraceContextTextMapPropagator().extract(dict(request.headers))
            remote = get_current_span(context).get_span_context()
            assert remote.trace_id == root.context.trace_id
            assert remote.span_id in {span.context.span_id for span in foundry if span.context}
            assert "baggage" not in request.headers
            assert "tracestate" not in request.headers
        for span in foundry:
            assert span.attributes is not None
            count = span.attributes["gen_ai.usage.input_tokens"]
            assert isinstance(count, int) and count > 0
        assert any(
            span.attributes
            and isinstance(charge := span.attributes.get("asc.cosmos.request_charge"), float)
            and charge > 0
            for span in spans
            if span.name == Operation.COSMOS
        )
        metrics = reader.get_metrics_data()
        assert metrics is not None
        names = {
            metric.name
            for resource_metrics in metrics.resource_metrics
            for scope in resource_metrics.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "asc.operation.duration",
            "asc.operation.count",
            "asc.model.tokens",
            "asc.cosmos.request_charge",
        } <= names
        assert CANARY not in str(metrics)
    assert CANARY not in caplog.text + "".join(span.to_json() for span in spans)
    assert all(
        not span.events or all(event.name != "exception" for event in span.events) for span in spans
    )


@pytest.mark.parametrize(
    ("failure", "category", "status"),
    [
        ("timeout", "foundry_timeout", 504),
        ("invalid_json", "foundry_invalid_result", 502),
        ("safety", "recipe_ingredient_rejected", 200),
    ],
)
def test_failures_name_the_responsible_operation_without_recording_payloads(
    failure: str,
    category: str,
    status: int,
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    harness: tuple[Transport, SyntheticCredential],
    auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry, exporter, reader = capture
    with TestClient(
        create_app(recipe_settings, telemetry=telemetry), headers=auth_headers
    ) as client:
        onboard(client)
        conversation = create(client)
        exporter.clear()
        if failure == "safety":
            recipe_transport.reject_recipe = True
        else:
            recipe_transport.fail_at = "mcp"
            recipe_transport.response_fault = failure
        response = send(client, conversation, content=f"Rice dinner {CANARY}")
        assert response.status_code == status, response.text
        spans = exporter.get_finished_spans()
        responsible = Operation.SAFETY if failure == "safety" else Operation.FOUNDRY
        assert any(
            span.name == responsible
            and span.status.status_code == StatusCode.ERROR
            and span.attributes is not None
            and span.attributes.get("error.type") == category
            for span in spans
        )
        metrics = reader.get_metrics_data()
        assert metrics is not None
        assert any(
            point.attributes is not None and point.attributes.get("error.type") == category
            for resource in metrics.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "asc.operation.count" and isinstance(metric.data, Sum)
            for point in metric.data.data_points
        )
    serialized = "".join(span.to_json() for span in spans)
    assert CANARY not in serialized + caplog.text
    assert "private-timeout" not in serialized
    assert "garnish with peanut" not in serialized
    assert all(event.name != "exception" for span in spans for event in span.events)


@pytest.mark.parametrize("throttles", [1, 3])
def test_cosmos_accounts_for_recovered_and_exhausted_throttling(
    throttles: int,
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry, exporter, reader = capture
    transport = harness[0]
    original_send = transport.send

    async def charged(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        response = await original_send(request, **kwargs)
        response.headers["x-ms-request-charge"] = "2.5" if "/docs" in request.url else "0"
        response.headers["x-ms-activity-id"] = str(uuid4())
        return response

    monkeypatch.setattr(transport, "send", charged)

    async def run() -> None:
        with telemetry.activate():
            async with cosmos.open_cosmos(settings()) as repository:
                transport.faults["GET"].extend([429] * throttles)
                if throttles == 3:
                    with pytest.raises(CosmosError):
                        await repository.read(ALICE, RECIPES, str(uuid4()))
                else:
                    assert await repository.read(ALICE, RECIPES, str(uuid4())) is None

    asyncio.run(run())
    span = exporter.get_finished_spans()[-1]
    assert span.name == Operation.COSMOS and span.kind == SpanKind.CLIENT
    assert span.attributes is not None
    attempts = 3 if throttles == 3 else 2
    assert span.attributes["asc.cosmos.attempt_count"] == attempts
    assert span.attributes["asc.cosmos.throttle_count"] == throttles
    assert span.attributes["asc.cosmos.request_charge"] == attempts * 2.5
    assert span.attributes["asc.result"] == ("error" if throttles == 3 else "ok")
    metrics = reader.get_metrics_data()
    assert metrics is not None
    counts = {
        metric.name: sum(point.value for point in metric.data.data_points)
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if isinstance(metric.data, Sum)
    }
    assert counts["asc.cosmos.throttle.count"] == throttles
    assert counts["asc.cosmos.request_charge"] == attempts * 2.5
    assert "secret-private-service-body" not in "".join(
        finished.to_json() for finished in exporter.get_finished_spans()
    )


def test_cosmos_sums_each_query_page_once(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry, exporter, _ = capture
    transport = harness[0]
    original_send = transport.send

    async def charged(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        response = await original_send(request, **kwargs)
        response.headers["x-ms-request-charge"] = "2.5"
        response.headers["x-ms-activity-id"] = str(uuid4())
        return response

    monkeypatch.setattr(transport, "send", charged)
    calls = 0

    async def run() -> None:
        nonlocal calls
        with telemetry.activate():
            async with cosmos.open_cosmos(settings()) as repository:
                for _ in range(3):
                    await repository.create(ALICE, recipe())
                exporter.clear()
                before = len(transport.item_calls("POST"))
                transport.empty_query_pages = 1
                page = await repository.query(
                    ALICE, HistoryQuery(kind="recipe"), PageRequest(limit=2)
                )
                assert len(page.items) == 2
                calls = len(transport.item_calls("POST")) - before

    asyncio.run(run())
    assert calls == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].attributes is not None
    assert spans[0].attributes["asc.cosmos.request_charge"] == calls * 2.5


def test_concurrent_requests_have_separate_roots_and_restore_context(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
    local_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    telemetry, exporter, _ = capture
    app = create_app(local_settings, telemetry=telemetry)

    @app.get("/synthetic/{private}")
    async def endpoint(private: str) -> dict[str, bool]:
        with operation(Operation.CONTEXT):
            await asyncio.sleep(0)
        return {"ok": True}

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://synthetic.invalid",
            headers=auth_headers,
        ) as client:
            responses = await asyncio.gather(
                client.get(f"/synthetic/{CANARY}-first"),
                client.get(f"/synthetic/{CANARY}-second"),
            )
        assert all(response.status_code == 200 for response in responses)
        assert len({response.headers["x-trace-id"] for response in responses}) == 2
        assert trace_id.get() is None
        assert not get_current_span().get_span_context().is_valid

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    roots = {span.context.trace_id: span for span in spans if span.parent is None and span.context}
    assert len(roots) == 2
    for span in spans:
        assert span.context and span.attributes
        root = roots[span.context.trace_id]
        assert root.attributes
        assert span.attributes["asc.correlation_id"] == root.attributes["asc.correlation_id"]
    assert CANARY not in "".join(span.to_json() for span in spans)


@pytest.mark.parametrize("ratio", [0.0, 1.0])
def test_owned_azure_exporters_emit_no_host_identity_or_private_content(
    monkeypatch: pytest.MonkeyPatch,
    ratio: float,
) -> None:
    configure_safe_telemetry()
    from azure.monitor.opentelemetry.exporter import _utils
    from azure.monitor.opentelemetry.exporter._generated import AzureMonitorClient
    from azure.monitor.opentelemetry.exporter._generated.exporter.models import (
        ContextTagKeys,
        TelemetryItem,
        TrackResponse,
    )

    credential = Mock()
    monkeypatch.setattr(credentials, "build_credential", lambda _settings: credential)
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
        "IngestionEndpoint=https://synthetic.invalid/",
    )
    monkeypatch.setitem(_utils.azure_monitor_context, ContextTagKeys.AI_DEVICE_ID, CANARY)
    monkeypatch.setattr(platform, "node", lambda: CANARY)
    envelopes: list[dict[str, Any]] = []

    def track(self: AzureMonitorClient, body: list[TelemetryItem], **kwargs: Any) -> TrackResponse:
        envelopes.extend(item.as_dict() for item in body)
        return TrackResponse(items_received=len(body), items_accepted=len(body), errors=[])

    monkeypatch.setattr(AzureMonitorClient, "track", track)
    telemetry = Telemetry.from_settings(
        Settings(
            environment="local",
            adapter_mode="local",
            telemetry_enabled=True,
            telemetry_sample_ratio=ratio,
        )
    )
    try:
        with telemetry.activate(), operation(Operation.HTTP, root=True):
            pass
        assert telemetry.provider is not None and telemetry.provider.force_flush()
        assert telemetry.meter_provider is not None and telemetry.meter_provider.force_flush()
    finally:
        telemetry.shutdown()
    assert envelopes
    serialized = json.dumps(envelopes, default=str)
    assert CANARY not in serialized
    assert ("RequestData" in serialized) == bool(ratio)
    assert "asc.operation.count" in serialized
    assert all(item["tags"]["ai.device.id"] == "ai-sous-chef" for item in envelopes)
    credential.close.assert_called_once()


def test_cancelled_scope_has_a_safe_outcome_and_restores_its_parent(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    telemetry, exporter, _ = capture
    with telemetry.activate():
        with pytest.raises(asyncio.CancelledError):
            with operation(Operation.CONTEXT):
                raise asyncio.CancelledError(CANARY)
        with operation(Operation.CONTEXT):
            pass
    cancelled, successful = exporter.get_finished_spans()
    assert cancelled.attributes and successful.attributes
    assert cancelled.attributes["asc.result"] == "cancelled"
    assert cancelled.attributes["error.type"] == "operation_cancelled"
    assert successful.attributes["asc.result"] == "ok" and successful.parent is None
    assert not get_current_span().get_span_context().is_valid
    assert CANARY not in cancelled.to_json()


def test_partial_exporter_initialization_is_closed_without_revealing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_safe_telemetry()
    from azure.monitor.opentelemetry import exporter as azure_exporter

    credential, exporter = Mock(), Mock()
    monkeypatch.setattr(credentials, "build_credential", lambda _settings: credential)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CANARY)
    monkeypatch.setattr(azure_exporter, "AzureMonitorTraceExporter", lambda **_kwargs: exporter)
    monkeypatch.setattr(
        azure_exporter, "AzureMonitorMetricExporter", Mock(side_effect=ValueError(CANARY))
    )
    with pytest.raises(ConfigurationError) as caught:
        Telemetry.from_settings(
            Settings(environment="local", adapter_mode="local", telemetry_enabled=True)
        )
    assert CANARY not in str(caught.value)
    exporter.shutdown.assert_called_once()
    credential.close.assert_called_once()
