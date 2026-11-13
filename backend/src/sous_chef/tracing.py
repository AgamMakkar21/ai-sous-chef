"""Application-owned spans and metrics: no automatic argument/exception capture."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

P = ParamSpec("P")
T = TypeVar("T")
_active: ContextVar[Telemetry | None] = ContextVar("synchronous_telemetry", default=None)


@dataclass
class _Result:
    outcome: str = "ok"
    category: ErrorCategory | None = None


_result: ContextVar[_Result | None] = ContextVar("telemetry_result", default=None)


@dataclass
class _CosmosObservation:
    charge: float = 0
    throttles: int = 0
    responses: set[str] = field(default_factory=set)


_cosmos: ContextVar[_CosmosObservation | None] = ContextVar("cosmos_observation", default=None)


class Operation(StrEnum):
    HTTP = "http.request"
    CONTEXT = "context.build"
    ORCHESTRATOR = "agent.orchestrator"
    SPECIALIST = "agent.recipe_nutrition"
    FOUNDRY = "foundry.responses"
    USDA = "tool.usda"
    USDA_SEARCH = "tool.usda.search"
    USDA_PROFILE = "tool.usda.profile"
    RETRIEVAL = "tool.foundry_iq"
    SAFETY = "recipe.safety"
    PERSISTENCE = "chat.persistence"
    COSMOS = "cosmos.operation"
    FEEDBACK = "feedback.persist"
    LEARNING = "learning.process"
    EXTRACTION = "learning.extract"
    PROJECTION = "learning.project"
    EVALUATION = "evaluation.run"


class Telemetry:
    def __init__(
        self,
        provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        close_credential: Callable[[], None] | None = None,
    ) -> None:
        self.provider = provider
        self.meter_provider = meter_provider
        self._close_credential = close_credential
        self._closed = False
        self.tracer = (
            provider.get_tracer("sous_chef.synchronous") if provider else trace.NoOpTracer()
        )
        meter = meter_provider.get_meter("sous_chef.synchronous") if meter_provider else None
        self.duration = (
            meter.create_histogram("asc.operation.duration", unit="ms") if meter else None
        )
        self.calls = meter.create_counter("asc.operation.count") if meter else None
        self.tokens = meter.create_counter("asc.model.tokens") if meter else None
        self.ru = meter.create_counter("asc.cosmos.request_charge", unit="{RU}") if meter else None
        self.throttles = meter.create_counter("asc.cosmos.throttle.count") if meter else None
        self.learning_events = meter.create_counter("asc.learning.events") if meter else None
        self.learning_lag = (
            meter.create_histogram("asc.learning.processing_lag", unit="ms") if meter else None
        )
        self.learning_batches = (
            meter.create_counter("asc.learning.batch.documents") if meter else None
        )

    @classmethod
    def from_settings(cls, settings: Settings, *, service_name: str = "ai-sous-chef") -> Telemetry:
        if not settings.telemetry_enabled:
            return cls()
        connection = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if not connection:
            raise ConfigurationError(
                "ASC_TELEMETRY_ENABLED requires APPLICATIONINSIGHTS_CONNECTION_STRING."
            )
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        from sous_chef.adapters.credentials import build_credential

        # A private provider cannot admit unreviewed SDK instrumentation or resource detectors.
        os.environ["OTEL_SDK_DISABLED"] = "false"
        resource = Resource(
            {
                "service.name": service_name,
                "service.instance.id": uuid4().hex,
                "device.id": "ai-sous-chef",
                "deployment.environment.name": settings.environment,
            }
        )
        with ExitStack() as cleanup:
            credential = build_credential(settings)
            cleanup.callback(credential.close)
            options = {
                "connection_string": connection,
                "credential": credential,
                "disable_offline_storage": True,
                "timeout": 5.0,
                "read_timeout": 5.0,
                "retry_total": 0,
            }
            try:
                exporter = AzureMonitorTraceExporter(**options)
                cleanup.callback(exporter.shutdown)
                metric_exporter = AzureMonitorMetricExporter(**options)
                cleanup.callback(metric_exporter.shutdown)
            except ValueError:
                raise ConfigurationError(
                    "The Application Insights telemetry configuration is invalid."
                ) from None
            provider = TracerProvider(
                resource=resource,
                sampler=TraceIdRatioBased(settings.telemetry_sample_ratio),
                shutdown_on_exit=False,
            )
            cleanup.callback(provider.shutdown)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            meters = MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
                shutdown_on_exit=False,
            )
            cleanup.callback(meters.shutdown)
            telemetry = cls(provider, meters, close_credential=credential.close)
            cleanup.pop_all()
            return telemetry

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.provider is not None:
                self.provider.shutdown()
        finally:
            try:
                if self.meter_provider is not None:
                    self.meter_provider.shutdown()
            finally:
                if self._close_credential is not None:
                    self._close_credential()


def fail(category: ErrorCategory) -> None:
    result = _result.get()
    if result is not None:
        result.outcome, result.category = "error", category
    span = trace.get_current_span()
    span.set_attribute("error.type", category.value)
    span.set_status(trace.StatusCode.ERROR)


@contextmanager
def operation(
    name: Operation, *, root: bool = False, links: Sequence[trace.Link] = ()
) -> Iterator[trace.Span]:
    telemetry = _active.get()
    tracer = telemetry.tracer if telemetry is not None else trace.NoOpTracer()
    started = monotonic()
    result = _Result()
    result_token = _result.set(result)
    cosmos_token = _cosmos.set(_CosmosObservation()) if name == Operation.COSMOS else None
    with tracer.start_as_current_span(
        name.value,
        context=Context() if root else None,
        links=links,
        kind=(
            trace.SpanKind.CONSUMER
            if name == Operation.LEARNING
            else trace.SpanKind.SERVER
            if root
            else trace.SpanKind.CLIENT
            if name
            in {
                Operation.FOUNDRY,
                Operation.EXTRACTION,
                Operation.COSMOS,
                Operation.USDA_SEARCH,
                Operation.USDA_PROFILE,
            }
            else trace.SpanKind.INTERNAL
        ),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("asc.operation", name.value)
        if trace_id.get() is not None:
            span.set_attribute("asc.correlation_id", trace_id.get() or "")
        try:
            yield span
        except BaseException as error:
            try:
                category = ErrorCategory(getattr(error, "code", ""))
            except ValueError:
                category = (
                    ErrorCategory.OPERATION_TIMEOUT
                    if isinstance(error, TimeoutError)
                    else ErrorCategory.OPERATION_CANCELLED
                    if isinstance(error, asyncio.CancelledError)
                    else ErrorCategory.INTERNAL_ERROR
                )
            fail(category)
            if isinstance(error, asyncio.CancelledError):
                result.outcome = "cancelled"
            raise
        finally:
            span.set_attribute("asc.result", result.outcome)
            if telemetry is not None:
                attributes = {"asc.operation": name.value, "asc.result": result.outcome}
                if result.category is not None:
                    attributes["error.type"] = result.category.value
                if telemetry.duration is not None:
                    telemetry.duration.record((monotonic() - started) * 1000, attributes)
                if telemetry.calls is not None:
                    telemetry.calls.add(1, attributes)
            if cosmos_token is not None:
                _cosmos.reset(cosmos_token)
            _result.reset(result_token)


def observed(
    name: Operation,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Coroutine[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(function)
        async def run(*args: P.args, **kwargs: P.kwargs) -> T:
            with operation(name):
                return await function(*args, **kwargs)

        return run

    return decorate


def foundry_trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if _active.get() is not None:
        TraceContextTextMapPropagator().inject(headers)
    return headers


def current_traceparent() -> str | None:
    return foundry_trace_headers().get("traceparent")


def trace_links(parent: object) -> tuple[trace.Link, ...]:
    if not isinstance(parent, str) or not re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", parent
    ):
        return ()
    context = TraceContextTextMapPropagator().extract({"traceparent": parent}, context=Context())
    span_context = trace.get_current_span(context).get_span_context()
    return (
        (trace.Link(span_context, {"asc.link.type": "feedback_write"}),)
        if span_context.is_valid
        else ()
    )


def learning_event(
    event: Event,
    *,
    category: ErrorCategory | None = None,
    attempt: int | None = None,
    generation: int | None = None,
    contributions: int | None = None,
    insights: int | None = None,
) -> None:
    operational_event(event, category=category, attempt=attempt, generation=generation)
    span = trace.get_current_span()
    span.set_attribute("asc.learning.result", event.value)
    for name, value in (
        ("attempt", attempt),
        ("generation", generation),
        ("contribution_count", contributions),
        ("insight_count", insights),
    ):
        if type(value) is int and 0 <= value <= 2**63 - 1:
            span.set_attribute(f"asc.learning.{name}", value)
    telemetry = _active.get()
    if telemetry is not None and telemetry.learning_events is not None:
        attributes = {"asc.learning.event": event.value}
        if category is not None:
            attributes["error.type"] = category.value
        telemetry.learning_events.add(1, attributes)


def learning_batch(counts: Mapping[str, int]) -> None:
    telemetry = _active.get()
    if telemetry is not None and telemetry.learning_batches is not None:
        for state in ("received", "ignored", "dispatched", "invalid", "failed", "quarantined"):
            telemetry.learning_batches.add(counts[state], {"asc.learning.delivery_state": state})


def processing_lag(observed_at: datetime) -> None:
    lag = max(0, (datetime.now(UTC) - observed_at).total_seconds() * 1000)
    trace.get_current_span().set_attribute("asc.learning.processing_lag_ms", lag)
    telemetry = _active.get()
    if telemetry is not None and telemetry.learning_lag is not None:
        telemetry.learning_lag.record(lag)


def model_usage(input_tokens: int, output_tokens: int) -> None:
    span = trace.get_current_span()
    telemetry = _active.get()
    for direction, count in (("input", input_tokens), ("output", output_tokens)):
        if type(count) is int and 0 <= count <= 2**63 - 1:
            span.set_attribute(f"gen_ai.usage.{direction}_tokens", count)
            if telemetry is not None and telemetry.tokens is not None:
                telemetry.tokens.add(count, {"gen_ai.token.type": direction})


def cosmos_response(headers: Mapping[str, str], response: object) -> None:
    observation = _cosmos.get()
    if observation is None:
        return
    try:
        charge = float(headers.get("x-ms-request-charge", "0"))
    except (ValueError, TypeError, OverflowError):
        return
    if not isfinite(charge) or charge < 0:
        return
    activity = headers.get("x-ms-activity-id")
    if activity is not None and re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", activity
    ):
        if activity in observation.responses:
            return
        observation.responses.add(activity)
    observation.charge += charge
    trace.get_current_span().set_attribute("asc.cosmos.request_charge", observation.charge)
    telemetry = _active.get()
    if telemetry is not None and telemetry.ru is not None:
        telemetry.ru.add(charge)


def cosmos_throttled() -> None:
    observation = _cosmos.get()
    if observation is None:
        return
    observation.throttles += 1
    trace.get_current_span().set_attribute("asc.cosmos.throttle_count", observation.throttles)
    telemetry = _active.get()
    if telemetry is not None and telemetry.throttles is not None:
        telemetry.throttles.add(1)
