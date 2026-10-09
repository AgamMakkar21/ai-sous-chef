import os
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, HttpUrl, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """An actionable configuration error containing no supplied values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASC_", extra="forbid", frozen=True, hide_input_in_errors=True
    )

    environment: Literal["local", "dev", "prod"]
    adapter_mode: Literal["local", "live"]
    readiness_timeout_seconds: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)
    static_dir: Path = Path(__file__).parent / "static"
    cosmos_endpoint: HttpUrl | None = Field(default=None, repr=False)
    cosmos_database: str | None = Field(default=None, min_length=1, max_length=255, repr=False)
    foundry_project_endpoint: HttpUrl | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        if self.environment != "local" and self.adapter_mode == "local":
            raise ConfigurationError(
                "ASC_ADAPTER_MODE=local is permitted only with ASC_ENVIRONMENT=local. "
                "Use live adapters for dev and prod."
            )
        if self.adapter_mode == "live":
            missing = [
                name
                for name, value in (
                    ("ASC_COSMOS_ENDPOINT", self.cosmos_endpoint),
                    ("ASC_COSMOS_DATABASE", self.cosmos_database),
                    ("ASC_FOUNDRY_PROJECT_ENDPOINT", self.foundry_project_endpoint),
                )
                if value is None
            ]
            if missing:
                raise ConfigurationError("Set required live settings: " + ", ".join(missing) + ".")
        for endpoint in (self.cosmos_endpoint, self.foundry_project_endpoint):
            if endpoint is not None and (
                endpoint.scheme != "https"
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query is not None
                or endpoint.fragment is not None
            ):
                raise ConfigurationError(
                    "ASC_COSMOS_ENDPOINT and ASC_FOUNDRY_PROJECT_ENDPOINT must use HTTPS "
                    "without credentials, query strings or fragments."
                )
        return self


def load_settings() -> Settings:
    environment = os.environ.get("ASC_ENVIRONMENT")
    if environment not in {"local", "dev", "prod"}:
        raise ConfigurationError(
            "Set ASC_ENVIRONMENT to local, dev or prod before starting the app."
        )
    try:
        return Settings(_env_file=Path(f".env.{environment}"), _env_file_encoding="utf-8")
    except ValidationError as exc:
        fields = sorted(
            {
                "ASC_" + str(error["loc"][0]).upper()
                if error["loc"] and error["loc"][0] in Settings.model_fields
                else "unrecognized configuration"
                for error in exc.errors()
            }
        )
        raise ConfigurationError(
            "Missing or invalid settings: "
            + ", ".join(fields)
            + f". Check environment variables and .env.{environment}; "
            + f"see .env.{environment}.example."
        ) from None
