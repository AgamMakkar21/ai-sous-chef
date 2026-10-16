import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sous_chef.adapters.readiness import LocalReadinessProbe
from sous_chef.api.health import create_health_router
from sous_chef.services.readiness import ReadinessService
from sous_chef.settings import ConfigurationError


def test_health_and_readiness_report_packaged_commit_without_changing_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = "a" * 40
    monkeypatch.setenv("ASC_DEPLOYMENT_COMMIT", "b" * 40)
    path = MagicMock()
    release = path.parents.__getitem__.return_value.__truediv__.return_value
    release.is_file.return_value = True
    release.read_text.return_value = json.dumps({"commit": sha})
    with patch("sous_chef.api.health.Path", return_value=path):
        router = create_health_router(ReadinessService(LocalReadinessProbe(), 1))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for endpoint, body in (
            ("/health", {"status": "ok"}),
            ("/ready", {"status": "ready"}),
        ):
            response = client.get(endpoint)
            assert response.json() == body
            assert response.headers["x-asc-commit"] == sha
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("commit", ["latest", "a" * 39, "a" * 40 + "\r\nX-Header: bad", None])
def test_invalid_packaged_commit_fails_instead_of_reporting_another_release(
    commit: str | None,
) -> None:
    path = MagicMock()
    release = path.parents.__getitem__.return_value.__truediv__.return_value
    release.is_file.return_value = True
    release.read_text.return_value = json.dumps({"commit": commit})
    with patch("sous_chef.api.health.Path", return_value=path):
        with pytest.raises(ConfigurationError, match="invalid commit"):
            create_health_router(ReadinessService(LocalReadinessProbe(), 1))
