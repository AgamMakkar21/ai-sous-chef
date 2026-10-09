import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sous_chef.adapters.readiness import LocalReadinessProbe
from sous_chef.app import create_app
from sous_chef.domain.readiness import DependencyUnavailable
from sous_chef.settings import ConfigurationError, Settings


def test_liveness_has_only_public_status(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_explicit_local_readiness(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


class UnavailableProbe:
    async def check(self) -> None:
        raise DependencyUnavailable("private-endpoint private-token")


class SlowProbe:
    async def check(self) -> None:
        await asyncio.sleep(60)


class BrokenProbe:
    async def check(self) -> None:
        raise RuntimeError("private-endpoint private-token")


@pytest.mark.parametrize("probe", [UnavailableProbe(), SlowProbe()])
def test_unavailable_or_timed_out_dependencies_are_safe(
    local_settings: Settings,
    probe: UnavailableProbe | SlowProbe,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(environment="local", adapter_mode="local", readiness_timeout_seconds=0.02)
    with TestClient(create_app(settings, readiness_probe=probe)) as test_client:
        response = test_client.get("/ready")
        assert test_client.get("/health").json() == {"status": "ok"}
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "dependency_unavailable"
    assert response.json()["traceId"] == response.headers["x-trace-id"]
    assert len(response.json()["traceId"]) == 32
    assert "private-" not in response.text
    assert "private-" not in caplog.text
    assert "readiness_" in caplog.text


def test_unexpected_errors_are_not_reported_as_readiness_success(
    local_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(
        create_app(local_settings, readiness_probe=BrokenProbe()), raise_server_exceptions=False
    ) as test_client:
        response = test_client.get("/ready")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "private-" not in response.text
    assert "private-" not in caplog.text
    assert response.json()["traceId"] in caplog.text


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_hosted_scaffold_is_not_ready_without_live_adapters(environment: str) -> None:
    settings = Settings(
        environment=environment,
        adapter_mode="live",
        cosmos_endpoint="https://cosmos.example.invalid/",
        cosmos_database="test",
        foundry_project_endpoint="https://foundry.example.invalid/api/projects/test",
    )
    with TestClient(create_app(settings)) as test_client:
        assert test_client.get("/health").status_code == 200
        assert test_client.get("/ready").status_code == 503
        assert test_client.get("/docs").status_code == 404
        assert test_client.get("/openapi.json").status_code == 404
    with pytest.raises(ConfigurationError, match="cannot run"):
        create_app(settings, readiness_probe=LocalReadinessProbe())


def test_missing_frontend_does_not_claim_to_be_a_working_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 503
    assert response.json()["code"] == "frontend_unavailable"


def test_built_frontend_and_assets_use_the_same_server(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html><body>frontend fixture</body></html>")
    (tmp_path / "assets" / "test.js").write_text("console.log('fixture');")
    settings = Settings(environment="local", adapter_mode="local", static_dir=tmp_path)
    with TestClient(create_app(settings)) as test_client:
        assert "frontend fixture" in test_client.get("/").text
        assert test_client.get("/assets/test.js").status_code == 200
        assert test_client.get("/health").json() == {"status": "ok"}
        response = test_client.get("/api/v1/not-implemented")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert test_client.get("/../.env.local").status_code == 404


def test_unknown_routes_have_safe_problem_responses(client: TestClient) -> None:
    response = client.get("/not-implemented")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["errors"] == []


def test_http_exception_details_are_not_exposed(local_settings: Settings) -> None:
    app = create_app(local_settings)

    @app.get("/test-http-error")
    def test_error() -> None:
        raise HTTPException(status_code=503, detail="private-token private-endpoint")

    with TestClient(app) as test_client:
        response = test_client.get("/test-http-error")
    assert response.status_code == 503
    assert "private-" not in response.text


class ValidationFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ('{"count":"private-input"}', 422, "invalid_input"),
        ('{"count":1,"private-property":"private-input"}', 422, "invalid_input"),
        ('{"count":private-input}', 400, "malformed_json"),
    ],
)
def test_validation_errors_never_echo_input(
    local_settings: Settings, body: str, status: int, code: str
) -> None:
    app = create_app(local_settings)

    @app.post("/test-validation")
    def validate(payload: ValidationFixture) -> dict[str, int]:
        return {"count": payload.count}

    with TestClient(app) as test_client:
        response = test_client.post(
            "/test-validation", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["errors"] == []
    assert "private-" not in response.text
