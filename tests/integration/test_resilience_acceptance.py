"""Bounded synthetic acceptance through production API, auth, persistence and agent adapters."""

import asyncio
import json
from collections.abc import Callable
from time import perf_counter
from uuid import uuid4

import httpx
import httpx2
import pytest
from conftest import AUTH_ISSUER
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sous_chef.adapters import foundry_gateway
from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.orchestrator import POLICY
from sous_chef.services.context import _encoding, context_tokens
from sous_chef.settings import Settings
from sous_chef.telemetry import trace_id
from sous_chef.tracing import Telemetry
from test_chat_api import documents, send
from test_conversation_api import BASE, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_mvp_acceptance import JourneyAgents
from test_profile_api import ME, onboard, preferences, signed_token
from test_recipe_chat import RecipeChatHarness
from test_recipe_chat import combined_settings as combined_settings
from test_recipe_chat import recipe_settings as recipe_settings
from test_recipe_chat import recipe_transport as recipe_transport
from test_synchronous_telemetry import capture as capture
from test_web_grounding import QUERY

from scripts.evaluate_resilience import (
    CONCURRENCY,
    CONTEXT_TOKENS,
    REQUESTS,
    LoadEvidence,
    LoadSample,
)

CANARY = "RESILIENCE_PRIVATE_CANARY@example.invalid"
type Capture = tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader]


def visible_input_tokens(request: httpx2.Request) -> int:
    value = json.loads(request.content)["input"]
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return len(_encoding().encode(text, disallowed_special=()))


@pytest.mark.parametrize("fault", ["timeout", "invalid_json"])
def test_web_failure_is_explicit_and_a_fresh_send_recovers(
    fault: str,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recipe_transport.arguments = recipe_transport.arguments.model_copy(
        update={"use_web": True, "web_query": QUERY}
    )
    with TestClient(create_app(recipe_settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        recipe_transport.fail_at = "web_search"
        recipe_transport.response_fault = fault
        response = send(client, conversation, content=f"Check current rice recalls. {CANARY}")
        assert response.status_code == (504 if fault == "timeout" else 502), response.text
        calls = len(recipe_transport.requests)
        assert (
            send(client, conversation, content=f"Check current rice recalls. {CANARY}").status_code
            == response.status_code
        )
        assert len(recipe_transport.requests) == calls
        failed = documents(harness[0], "turn")[0]
        assert failed["status"] in {"failed", "outcomeUnknown"}
        assert failed.get("recipe") is None and not failed.get("generatedRecipeId")
        recipe_transport.fail_at = ""
        recovered = send(client, create(client), content="Check current rice recalls.")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["recipe"]["webGrounding"] is not None
    assert CANARY not in caplog.text


def test_bounded_chat_load_measures_budgets_and_purges_synthetic_users(
    combined_settings: Settings,
    signing_key: rsa.RSAPrivateKey,
    harness: tuple[Transport, SyntheticCredential],
    capture: Capture,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = combined_settings.model_copy(update={"context_max_tokens": CONTEXT_TOKENS})
    router = JourneyAgents()
    agents: dict[str, JourneyAgents] = {}

    async def route(request: httpx2.Request) -> httpx2.Response:
        correlation = trace_id.get()
        assert correlation is not None
        if correlation not in agents:
            agents[correlation] = JourneyAgents()
        await asyncio.sleep(0.005)
        return await agents[correlation].handle(request)

    monkeypatch.setattr(router, "handle", route)
    monkeypatch.setattr(foundry_gateway, "_project_client", router.factory)
    users: list[AuthenticatedUser] = []
    samples: list[LoadSample] = []
    peak = 0
    active = 0
    cleanup_complete = False
    app = create_app(config, telemetry=capture[0])

    async def scenario() -> None:
        nonlocal peak, active, cleanup_complete
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://testserver"
            ) as client,
        ):
            targets: list[tuple[dict[str, str], str]] = []
            try:
                for index in range(REQUESTS):
                    subject = f"asc62-synthetic-{index}"
                    users.append(AuthenticatedUser.from_verified_identity(AUTH_ISSUER, subject))
                    headers = {"Authorization": "Bearer " + signed_token(signing_key, subject)}
                    current = await client.get(ME, headers=headers)
                    assert current.status_code == 200
                    initialized = await client.put(
                        ME + "/onboarding",
                        json=preferences(),
                        headers=headers | {"If-Match": current.headers["etag"]},
                    )
                    assert initialized.status_code == 200
                    created = await client.post(
                        BASE,
                        json={"title": "Synthetic bounded load"},
                        headers=headers | {"Idempotency-Key": str(uuid4())},
                    )
                    assert created.status_code == 201
                    targets.append((headers, created.json()["data"]["conversationId"]))
                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def request(headers: dict[str, str], conversation: str) -> None:
                    nonlocal active, peak
                    async with semaphore:
                        active += 1
                        peak = max(peak, active)
                        started = perf_counter()
                        try:
                            async with asyncio.timeout(POLICY.deadline_seconds + 5):
                                response = await client.post(
                                    f"{BASE}/{conversation}/turns",
                                    headers=headers | {"Idempotency-Key": "bounded-load"},
                                    json={"content": f"Make a 30-minute pantry recipe. {CANARY}"},
                                )
                            latency = (perf_counter() - started) * 1000
                            peer = agents.get(response.headers["x-trace-id"])
                            calls = peer.requests if peer else []
                            samples.append(
                                LoadSample(
                                    latency_ms=latency,
                                    status=response.status_code,
                                    model_calls=len(calls),
                                    context_tokens=context_tokens(peer.contexts[0])
                                    if peer and peer.contexts
                                    else 0,
                                    visible_input_tokens=sum(
                                        visible_input_tokens(call) for call in calls
                                    ),
                                )
                            )
                            if response.status_code == 200:
                                turn = response.json()["data"]
                                assert turn["status"] == "completed"
                                assert turn["recipe"]["nutrition"]["status"] == "known"
                                assert {row["kind"] for row in turn["recipe"]["sources"]} >= {
                                    "curated_recipe",
                                    "usda",
                                }
                        finally:
                            active -= 1

                results = await asyncio.gather(
                    *(request(*target) for target in targets), return_exceptions=True
                )
                assert not any(isinstance(result, BaseException) for result in results)
                assert len(documents(harness[0], "turn")) == REQUESTS
                assert len(documents(harness[0], "sendOperation")) == REQUESTS
            finally:
                async with open_cosmos(config, lifetime_seconds=60) as repository:
                    for user in users:
                        assert (await repository.purge_user(user, uuid4())).complete
                remaining = list(harness[0].documents.values())
                cleanup_complete = len(remaining) == 3 * len(users) and all(
                    row["recordKind"] in {"deletionTombstone", "deletionGuard"}
                    and row["state"] == "deleted"
                    and row["ttl"] == -1
                    for row in remaining
                )

    try:
        asyncio.run(scenario())
    finally:
        record_property(
            "asc62_load",
            {
                "samples": [sample.model_dump() for sample in samples],
                "peak_concurrency": peak,
                "cleanup_complete": cleanup_complete,
            },
        )
    evidence = LoadEvidence(
        samples=samples, peak_concurrency=peak, cleanup_complete=cleanup_complete
    )
    assert evidence.summary()["passed"], evidence.summary()
    serialized = "".join(span.to_json() for span in capture[1].get_finished_spans())
    for private in (CANARY, *(user.user_id for user in users)):
        assert private not in serialized + caplog.text
    assert CANARY not in json.dumps(list(harness[0].documents.values()))
