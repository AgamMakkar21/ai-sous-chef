import asyncio
import json
import logging
import os
import socket
import threading
import time
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
import uvicorn
from azure.core.pipeline import PipelineContext, PipelineRequest
from azure.core.pipeline.policies import HttpLoggingPolicy, NetworkTraceLoggingPolicy
from azure.core.rest import HttpRequest
from azure.core.settings import settings as azure_settings
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sous_chef.app import create_app
from sous_chef.settings import Settings
from sous_chef.telemetry import CAPTURE_DISABLED_ENV, error_category, trace_id

CANARIES = {
    "email": "pii-canary@example.invalid",
    "allergies": "ALLERGY_CANARY_peanut",
    "preferences": "PREFERENCE_CANARY_spicy",
    "message": "MESSAGE_CANARY_dinner",
    "notes": "NOTE_CANARY_private",
}
PRIVATE = " ".join(CANARIES.values())


def assert_no_canaries(text: str) -> None:
    for value in CANARIES.values():
        assert value not in text


def request_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "sous_chef.operational"
        and json.loads(record.getMessage())["event"].startswith("request_")
    ]


class SensitivePayload(BaseModel):
    email: str
    allergies: str
    preferences: str
    message: str
    notes: str
    quantity: int


@pytest.mark.parametrize("failure", ["success", "exception", "http", "validation", "json", "404"])
def test_sensitive_content_never_enters_operational_logs(
    local_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    auth_headers: dict[str, str],
) -> None:
    app = create_app(local_settings)

    @app.post("/privacy/{private_path}")
    async def handle(payload: SensitivePayload, private_path: str) -> dict[str, str]:
        if failure == "exception":
            try:
                raise RuntimeError(PRIVATE)
            except RuntimeError:
                logging.getLogger("openai").exception(
                    "response/tool arguments: %s", payload, extra={"private_content": PRIVATE}
                )
                raise
        if failure == "http":
            raise HTTPException(503, detail=PRIVATE)
        logging.getLogger("sous_chef.anything").info("profile=%s", payload)
        return {"status": "ok"}

    body: dict[str, str | int] = {**CANARIES, "quantity": 1}
    if failure == "validation":
        body["quantity"] = PRIVATE
    content = json.dumps(body) if failure != "json" else '{"quantity":' + PRIVATE
    path = "/privacy/" + CANARIES["notes"] if failure != "404" else "/unknown/" + CANARIES["notes"]
    with TestClient(app) as client:
        response = client.post(
            path,
            params=CANARIES,
            headers={
                "Content-Type": "application/json",
                "X-Trace-ID": CANARIES["email"],
                **auth_headers,
                "Cookie": "private=" + CANARIES["message"],
            },
            content=content,
        )

    expected_status = {
        "success": 200,
        "exception": 500,
        "http": 503,
        "validation": 422,
        "json": 400,
        "404": 404,
    }[failure]
    assert response.status_code == expected_status
    request_id = response.headers["x-trace-id"]
    assert UUID(hex=request_id).version == 4
    events = request_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["trace_id"] == request_id
    assert event["status"] == expected_status
    assert set(event) <= {"event", "trace_id", "status", "duration_ms", "error_category"}
    assert isinstance(event["duration_ms"], (float, int)) and event["duration_ms"] >= 0
    if expected_status >= 400:
        assert event["event"] == "request_failed"
        assert event["error_category"] == response.json()["code"]
        assert response.json()["traceId"] == request_id
    else:
        assert event["event"] == "request_completed"
        assert "error_category" not in event
    assert_no_canaries(response.text + str(response.headers) + caplog.text)
    for record in caplog.records:
        assert_no_canaries(str(record.__dict__))
        assert record.exc_info is None
        assert record.stack_info is None


def test_real_azure_http_logging_policies_cannot_emit_request_content(
    local_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    create_app(local_settings)
    caplog.set_level(logging.DEBUG)
    request = HttpRequest(
        "POST", "https://example.invalid/" + CANARIES["email"], headers=CANARIES, json=CANARIES
    )
    context = PipelineContext(None, logging_enable=True)
    pipeline_request = PipelineRequest(request, context)
    policy = HttpLoggingPolicy()
    policy.allowed_header_names.update(CANARIES)
    policy.on_request(pipeline_request)
    NetworkTraceLoggingPolicy(logging_enable=True).on_request(pipeline_request)
    assert caplog.records
    assert all(json.loads(record.getMessage())["source"] == "sdk" for record in caplog.records)
    assert_no_canaries(caplog.text)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_content_capture_is_disabled_even_if_process_flags_enable_it(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    for name in CAPTURE_DISABLED_ENV:
        monkeypatch.setenv(name, "true")
    monkeypatch.setattr(azure_settings, "tracing_enabled", True)
    settings = Settings(
        environment=environment,
        adapter_mode="live",
        cosmos_endpoint="https://cosmos.example.invalid/",
        cosmos_database="test",
        foundry_project_endpoint="https://foundry.example.invalid/api/projects/test",
        foundry_orchestrator_deployment="orchestrator",
        foundry_recipe_nutrition_deployment="recipe-nutrition",
        foundry_extraction_deployment="extraction",
    )
    create_app(settings)
    assert all(os.environ[name] == value for name, value in CAPTURE_DISABLED_ENV.items())
    assert azure_settings.tracing_enabled() is False


def test_concurrent_requests_have_distinct_matching_ids(
    local_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    auth_headers: dict[str, str],
) -> None:
    app = create_app(local_settings)

    @app.get("/overlap")
    async def overlap() -> None:
        await asyncio.sleep(0.01)
        raise HTTPException(409, detail=PRIVATE)

    async def requests() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth_headers
        ) as client:

            async def request() -> httpx.Response:
                response = await client.get("/overlap")
                assert trace_id.get() is None
                assert error_category.get() is None
                return response

            return list(await asyncio.gather(*(request() for _ in range(8))))

    responses = asyncio.run(requests())
    ids = {response.headers["x-trace-id"] for response in responses}
    assert len(ids) == 8
    assert {event["trace_id"] for event in request_events(caplog)} == ids
    assert all(
        response.json()["traceId"] == response.headers["x-trace-id"] for response in responses
    )
    assert_no_canaries(caplog.text)


def test_streaming_failure_is_logged_without_a_second_response(
    local_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    auth_headers: dict[str, str],
) -> None:
    app = create_app(local_settings)

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"safe chunk"
            raise RuntimeError(PRIVATE)

        return StreamingResponse(body())

    with TestClient(app, headers=auth_headers) as client, pytest.raises(RuntimeError):
        client.get("/stream")
    events = request_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "request_failed"
    assert events[0]["status"] == 500
    assert events[0]["error_category"] == "internal_error"
    assert_no_canaries(caplog.text)


def test_live_server_streaming_errors_and_urls_are_safe(
    local_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    auth_headers: dict[str, str],
) -> None:
    app = create_app(local_settings)

    @app.get("/stream/{private_path}")
    async def stream(private_path: str) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"safe chunk"
            raise RuntimeError(PRIVATE)

        return StreamingResponse(body())

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="debug", access_log=True))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5, headers=auth_headers
            ) as client:
                assert client.get("/health").json() == {"status": "ok"}
                with client.stream(
                    "GET", "/stream/" + CANARIES["notes"], params=CANARIES
                ) as response:
                    request_id = response.headers["x-trace-id"]
                    with pytest.raises(httpx.RemoteProtocolError):
                        response.read()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()

    events = request_events(caplog)
    failed = [event for event in events if event["event"] == "request_failed"]
    assert len(failed) == 1
    assert failed[0]["trace_id"] == request_id
    assert failed[0]["error_category"] == "internal_error"
    captured = capsys.readouterr()
    assert_no_canaries(captured.out + captured.err + caplog.text)
    assert "Logging error" not in captured.err
