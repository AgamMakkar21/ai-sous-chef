import os
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, HttpUrl, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """An actionable configuration error containing no supplied values."""


_DEPLOYMENT_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_GUID = r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
_SEARCH_NAME = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
_PROJECT_RESOURCE_ID = (
    r"^/subscriptions/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"/resourceGroups/[A-Za-z0-9._()-]+"
    r"/providers/Microsoft\.CognitiveServices/accounts/[A-Za-z0-9-]+"
    r"/projects/[A-Za-z0-9._-]+$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASC_", extra="forbid", frozen=True, hide_input_in_errors=True
    )

    environment: Literal["local", "dev", "prod"]
    adapter_mode: Literal["local", "live"]
    readiness_timeout_seconds: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)
    static_dir: Path = Path(__file__).parent / "static"
    auth_issuer: HttpUrl | None = Field(default=None, repr=False)
    auth_jwks_uri: HttpUrl | None = Field(default=None, repr=False)
    auth_client_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
        repr=False,
    )
    auth_origin: HttpUrl | None = Field(default=None, repr=False)
    cosmos_endpoint: HttpUrl | None = Field(default=None, repr=False)
    cosmos_database: str | None = Field(default=None, min_length=1, max_length=255, repr=False)
    foundry_project_endpoint: HttpUrl | None = Field(default=None, repr=False)
    foundry_project_resource_id: str | None = Field(
        default=None, pattern=_PROJECT_RESOURCE_ID, repr=False
    )
    foundry_iq_search_endpoint: HttpUrl | None = Field(default=None, repr=False)
    foundry_iq_index_name: str | None = Field(
        default=None, min_length=3, max_length=63, pattern=_SEARCH_NAME, repr=False
    )
    foundry_iq_knowledge_source_name: str | None = Field(
        default=None, min_length=3, max_length=63, pattern=_SEARCH_NAME, repr=False
    )
    foundry_iq_knowledge_base_name: str | None = Field(
        default=None, min_length=3, max_length=63, pattern=_SEARCH_NAME, repr=False
    )
    foundry_iq_connection_name: str | None = Field(
        default=None, min_length=3, max_length=33, pattern=_SEARCH_NAME, repr=False
    )
    foundry_orchestrator_deployment: str | None = Field(
        default=None, pattern=_DEPLOYMENT_NAME, repr=False
    )
    foundry_recipe_nutrition_deployment: str | None = Field(
        default=None, pattern=_DEPLOYMENT_NAME, repr=False
    )
    foundry_extraction_deployment: str | None = Field(
        default=None, pattern=_DEPLOYMENT_NAME, repr=False
    )
    foundry_orchestrator_agent_name: str | None = Field(
        default=None, pattern=_DEPLOYMENT_NAME, repr=False
    )
    foundry_recipe_nutrition_agent_name: str | None = Field(
        default=None, pattern=_DEPLOYMENT_NAME, repr=False
    )
    foundry_orchestrator_agent_principal_id: str | None = Field(
        default=None, pattern=_GUID, repr=False
    )
    foundry_recipe_nutrition_agent_principal_id: str | None = Field(
        default=None, pattern=_GUID, repr=False
    )
    app_workload_principal_id: str | None = Field(default=None, pattern=_GUID, repr=False)

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        auth_values = (self.auth_issuer, self.auth_jwks_uri, self.auth_client_id, self.auth_origin)
        if any(value is not None for value in auth_values):
            if any(value is None for value in auth_values):
                raise ConfigurationError(
                    "Set ASC_AUTH_ISSUER, ASC_AUTH_JWKS_URI, "
                    "ASC_AUTH_CLIENT_ID and ASC_AUTH_ORIGIN."
                )
            for endpoint in (self.auth_issuer, self.auth_jwks_uri):
                if endpoint is not None and (
                    endpoint.scheme != "https"
                    or not (endpoint.host or "").endswith(".ciamlogin.com")
                    or endpoint.port != 443
                    or endpoint.username is not None
                    or endpoint.password is not None
                    or endpoint.query is not None
                    or endpoint.fragment is not None
                ):
                    raise ConfigurationError(
                        "Authentication endpoints must use HTTPS ciamlogin.com."
                    )
            if self.auth_origin is not None and (
                (self.auth_origin.scheme != "https" and self.environment != "local")
                or self.auth_origin.username is not None
                or self.auth_origin.password is not None
                or self.auth_origin.path != "/"
                or self.auth_origin.query is not None
                or self.auth_origin.fragment is not None
            ):
                raise ConfigurationError("ASC_AUTH_ORIGIN must be an HTTPS origin without a path.")
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
                    (
                        "ASC_FOUNDRY_ORCHESTRATOR_DEPLOYMENT",
                        self.foundry_orchestrator_deployment,
                    ),
                    (
                        "ASC_FOUNDRY_RECIPE_NUTRITION_DEPLOYMENT",
                        self.foundry_recipe_nutrition_deployment,
                    ),
                    ("ASC_FOUNDRY_EXTRACTION_DEPLOYMENT", self.foundry_extraction_deployment),
                )
                if value is None
            ]
            if missing:
                raise ConfigurationError("Set required live settings: " + ", ".join(missing) + ".")
        pinned = [
            value.lower()
            for value in (
                self.foundry_orchestrator_agent_principal_id,
                self.foundry_recipe_nutrition_agent_principal_id,
                self.app_workload_principal_id,
            )
            if value is not None
        ]
        if len(pinned) != len(set(pinned)):
            raise ConfigurationError(
                "Orchestrator, Recipe + Nutrition and App Service principal IDs must be distinct."
            )
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
        endpoint = self.foundry_iq_search_endpoint
        if endpoint is not None and (
            endpoint.scheme != "https"
            or not (endpoint.host or "").endswith(".search.windows.net")
            or endpoint.port != 443
            or endpoint.path not in (None, "/")
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query is not None
            or endpoint.fragment is not None
        ):
            raise ConfigurationError(
                "ASC_FOUNDRY_IQ_SEARCH_ENDPOINT must be an HTTPS search.windows.net "
                "service origin without credentials, query strings or fragments."
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
