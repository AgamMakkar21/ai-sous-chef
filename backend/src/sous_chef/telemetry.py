"""Allowlisted operational logging; raw diagnostic content is never emitted."""

import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from uuid import uuid4

from azure.core.settings import settings as azure_settings
from opentelemetry import trace

from sous_chef.settings import ConfigurationError

trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
error_category: ContextVar["ErrorCategory | None"] = ContextVar("error_category", default=None)

CAPTURE_DISABLED_ENV = {
    "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED": "false",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
    "OTEL_SDK_DISABLED": "true",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
}


class ErrorCategory(StrEnum):
    NOT_FOUND = "not_found"
    HTTP_ERROR = "http_error"
    MALFORMED_JSON = "malformed_json"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    FRONTEND_UNAVAILABLE = "frontend_unavailable"
    UNAUTHENTICATED = "unauthenticated"
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"
    CSRF_FAILED = "csrf_failed"
    FOUNDRY_AUTHENTICATION = "foundry_authentication"
    FOUNDRY_THROTTLED = "foundry_throttled"
    FOUNDRY_TIMEOUT = "foundry_timeout"
    FOUNDRY_INVALID_RESULT = "foundry_invalid_result"
    FOUNDRY_UNAVAILABLE = "foundry_unavailable"
    FOUNDRY_REJECTED = "foundry_rejected"
    COSMOS_AUTHENTICATION = "cosmos_authentication"
    COSMOS_FORBIDDEN = "cosmos_forbidden"
    COSMOS_THROTTLED = "cosmos_throttled"
    COSMOS_TIMEOUT = "cosmos_timeout"
    COSMOS_UNAVAILABLE = "cosmos_unavailable"
    COSMOS_INVALID_RESULT = "cosmos_invalid_result"
    COSMOS_REJECTED = "cosmos_rejected"
    REVISION_CONFLICT = "revision_conflict"
    DUPLICATE_RESOURCE = "duplicate_resource"
    RECORD_TOO_LARGE = "record_too_large"
    BODY_TOO_LARGE = "body_too_large"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    ONBOARDING_REQUIRED = "onboarding_required"
    PRECONDITION_REQUIRED = "precondition_required"


class Event(StrEnum):
    REQUEST_COMPLETED = "request_completed"
    REQUEST_FAILED = "request_failed"
    READINESS_UNAVAILABLE = "readiness_dependency_unavailable"
    READINESS_TIMEOUT = "readiness_deadline_exceeded"
    FOUNDRY_COMPLETED = "foundry_completed"
    FOUNDRY_FAILED = "foundry_failed"
    FOUNDRY_CANCELLED = "foundry_cancelled"
    COSMOS_FAILED = "cosmos_failed"


@dataclass(frozen=True)
class _OperationalEvent:
    event: Event
    trace_id: str | None
    category: ErrorCategory | None
    status: int | None
    duration_ms: float | None

    def serialize(self) -> str:
        fields: dict[str, str | int | float] = {"event": self.event.value}
        if self.trace_id is not None:
            fields["trace_id"] = self.trace_id
        if self.category is not None:
            fields["error_category"] = self.category.value
        if self.status is not None:
            fields["status"] = self.status
        if self.duration_ms is not None:
            fields["duration_ms"] = round(self.duration_ms, 2)
        return json.dumps(fields, separators=(",", ":"), allow_nan=False)


def operational_event(
    event: Event,
    *,
    category: ErrorCategory | None = None,
    status: int | None = None,
    duration_ms: float | None = None,
) -> None:
    if status is not None and not 100 <= status <= 599:
        raise ValueError("Operational status must be an HTTP status code.")
    if duration_ms is not None and (not isfinite(duration_ms) or duration_ms < 0):
        raise ValueError("Operational duration must be finite and nonnegative.")
    level = (
        logging.ERROR
        if event in {Event.REQUEST_FAILED, Event.FOUNDRY_FAILED, Event.COSMOS_FAILED}
        else logging.INFO
    )
    if event in {Event.READINESS_UNAVAILABLE, Event.READINESS_TIMEOUT}:
        level = logging.WARNING
    logging.getLogger("sous_chef.operational").log(
        level, _OperationalEvent(event, trace_id.get(), category, status, duration_ms)
    )


def new_trace_id() -> str:
    return uuid4().hex


class _RecordFields(dict[str, object]):
    def __setitem__(self, key: str, value: object) -> None:
        # Logger.makeRecord adds arbitrary `extra` fields after calling the record factory.
        if key in self:
            super().__setitem__(key, value)


class _SuppressAccessLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


_access_log_filter = _SuppressAccessLogs()


def _safe_record(
    name: str,
    level: int,
    pathname: str,
    lineno: int,
    msg: object,
    args: object,
    exc_info: object,
    func: str | None = None,
    sinfo: str | None = None,
    **kwargs: object,
) -> logging.LogRecord:
    # Sanitize at creation, before any handler (including SDK/server handlers) sees the record.
    # Arbitrary messages, formatting arguments, filenames and tracebacks are not an allowlist.
    if isinstance(msg, _OperationalEvent):
        message = msg.serialize()
        source = "sous_chef.operational"
    else:
        source = (
            "sdk"
            if name.startswith(("azure", "openai", "httpx", "httpcore", "opentelemetry"))
            else "runtime"
        )
        message = json.dumps({"event": "diagnostic", "source": source}, separators=(",", ":"))
    record = logging.LogRecord(source, level, "", 0, message, (), None, None, None)
    record.threadName = None
    record.processName = None
    record.taskName = None
    record.__dict__ = _RecordFields(record.__dict__)
    return record


def configure_safe_telemetry() -> None:
    """Apply a process-wide MVP policy before constructing any SDK clients."""
    logging.setLogRecordFactory(_safe_record)
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = True
    # dictConfig may re-enable the logger after app construction; the filter survives it.
    if _access_log_filter not in access_logger.filters:
        access_logger.addFilter(_access_log_filter)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name, value in CAPTURE_DISABLED_ENV.items():
        os.environ[name] = value
    azure_settings.tracing_enabled = False
    if not isinstance(
        trace.get_tracer_provider(), (trace.ProxyTracerProvider, trace.NoOpTracerProvider)
    ):
        raise ConfigurationError(
            "Automatic tracing is not permitted in the MVP. "
            "Start plain Uvicorn without an OpenTelemetry/Application Insights tracing agent."
        )
