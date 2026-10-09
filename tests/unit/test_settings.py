import os
from pathlib import Path

import pytest
from sous_chef.settings import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.upper().startswith("ASC_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("environment", [None, "production", "secret-invalid-value"])
def test_environment_must_be_explicit(
    monkeypatch: pytest.MonkeyPatch, environment: str | None
) -> None:
    if environment is not None:
        monkeypatch.setenv("ASC_ENVIRONMENT", environment)
    with pytest.raises(ConfigurationError, match="Set ASC_ENVIRONMENT") as error:
        load_settings()
    assert "secret-invalid-value" not in str(error.value)


def test_missing_adapter_mode_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASC_ENVIRONMENT", "local")
    with pytest.raises(ConfigurationError, match="ASC_ADAPTER_MODE"):
        load_settings()


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_local_doubles_are_forbidden_in_hosted_environments(environment: str) -> None:
    with pytest.raises(ConfigurationError, match="permitted only"):
        Settings(environment=environment, adapter_mode="local")


def test_live_settings_are_required() -> None:
    with pytest.raises(ConfigurationError, match="ASC_COSMOS_ENDPOINT"):
        Settings(environment="prod", adapter_mode="live")


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_selected_file_and_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str
) -> None:
    monkeypatch.setenv("ASC_ENVIRONMENT", environment)
    (tmp_path / f".env.{environment}").write_text(
        "ASC_ADAPTER_MODE=live\n"
        "ASC_COSMOS_ENDPOINT=https://cosmos.example.invalid/\n"
        "ASC_COSMOS_DATABASE=test-database\n"
        "ASC_FOUNDRY_PROJECT_ENDPOINT=https://foundry.example.invalid/api/projects/test\n"
        "ASC_READINESS_TIMEOUT_SECONDS=1\n"
    )
    (tmp_path / ".env").write_text("ASC_ADAPTER_MODE=local\n")
    monkeypatch.setenv("ASC_READINESS_TIMEOUT_SECONDS", "3")
    settings = load_settings()
    assert settings.environment == environment
    assert settings.adapter_mode == "live"
    assert settings.readiness_timeout_seconds == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ASC_ADAPTER_MODE", "secret-invalid-mode"),
        ("ASC_READINESS_TIMEOUT_SECONDS", "secret-invalid-number"),
        ("ASC_READINESS_TIMEOUT_SECONDS", "nan"),
        ("ASC_READINESS_TIMEOUT_SECONDS", "inf"),
        ("ASC_READINESS_TIMEOUT_SECONDS", "0"),
        ("ASC_READINESS_TIMEOUT_SECONDS", "11"),
        ("ASC_COSMOS_ENDPOINT", "secret-invalid-url"),
    ],
)
def test_invalid_values_are_not_exposed(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("ASC_ENVIRONMENT", "local")
    monkeypatch.setenv("ASC_ADAPTER_MODE", "local")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert name in str(error.value)
    assert "secret-invalid" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/",
        "https://user:private-password@example.invalid/",
        "https://example.invalid/?key=private-key",
        "https://example.invalid/#private-fragment",
    ],
)
def test_endpoint_credentials_and_non_https_are_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError, match="must use HTTPS") as error:
        Settings(environment="local", adapter_mode="local", cosmos_endpoint=url)
    assert "private-" not in str(error.value)


def test_unknown_file_setting_does_not_expose_its_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASC_ENVIRONMENT", "local")
    (tmp_path / ".env.local").write_text(
        "ASC_ADAPTER_MODE=local\nsecret-private-name=secret-private-value\n"
    )
    with pytest.raises(ConfigurationError, match="unrecognized configuration") as error:
        load_settings()
    assert "secret-private" not in str(error.value)
