import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from sous_chef.settings import ConfigurationError
from sous_chef.telemetry import Event, configure_safe_telemetry, operational_event, trace_id


def test_preinitialized_tracing_blocks_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())
    with pytest.raises(ConfigurationError, match="Automatic tracing is not permitted"):
        configure_safe_telemetry()


@pytest.mark.parametrize("logger_name", ["openai", "azure.ai.projects", "uvicorn.error", "httpx"])
def test_arbitrary_messages_exceptions_and_extra_fields_are_discarded(
    logger_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    configure_safe_telemetry()
    try:
        raise RuntimeError("PRIVATE_CANARY_response")
    except RuntimeError:
        logging.getLogger(logger_name).error(
            "PRIVATE_CANARY_prompt: %s",
            "PRIVATE_CANARY_tool_arguments",
            exc_info=True,
            stack_info=True,
            extra={"email": "PRIVATE_CANARY_email"},
        )
    assert caplog.records
    assert "PRIVATE_CANARY" not in caplog.text
    for record in caplog.records:
        assert "PRIVATE_CANARY" not in str(record.__dict__)
        assert set(json.loads(record.getMessage())) == {"event", "source"}


@pytest.mark.parametrize("status", [99, 600])
def test_invalid_log_status_is_rejected(status: int) -> None:
    with pytest.raises(ValueError, match="status"):
        operational_event(Event.REQUEST_COMPLETED, status=status)


@pytest.mark.parametrize("duration", [-1.0, float("nan"), float("inf")])
def test_invalid_log_duration_is_rejected(duration: float) -> None:
    with pytest.raises(ValueError, match="duration"):
        operational_event(Event.REQUEST_COMPLETED, duration_ms=duration)


def test_request_context_is_reset_after_requests(client: TestClient) -> None:
    client.get("/health")
    assert trace_id.get() is None


def test_access_logging_is_disabled() -> None:
    configure_safe_telemetry()
    assert logging.getLogger("uvicorn.access").disabled is True
