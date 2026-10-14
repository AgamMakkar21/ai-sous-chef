from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential

from sous_chef.adapters.credentials import build_credential
from sous_chef.settings import ConfigurationError, Settings


class ModelRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    RECIPE_NUTRITION = "recipe_nutrition"
    EXTRACTION = "extraction"


class FoundryInvocationError(RuntimeError):
    """A Foundry call failed. The message names the role and error type, never content."""


class _Responses(Protocol):
    def create(self, *, model: str, input: str, max_output_tokens: int) -> Any: ...


class _OpenAIClient(Protocol):
    @property
    def responses(self) -> _Responses: ...


@dataclass(frozen=True)
class FoundryDeployments:
    orchestrator: str
    recipe_nutrition: str
    extraction: str

    def for_role(self, role: ModelRole) -> str:
        return {
            ModelRole.ORCHESTRATOR: self.orchestrator,
            ModelRole.RECIPE_NUTRITION: self.recipe_nutrition,
            ModelRole.EXTRACTION: self.extraction,
        }[role]

    @classmethod
    def from_settings(cls, settings: Settings) -> "FoundryDeployments":
        names = (
            settings.foundry_orchestrator_deployment,
            settings.foundry_recipe_nutrition_deployment,
            settings.foundry_extraction_deployment,
        )
        if any(name is None for name in names):
            raise ConfigurationError("Set all ASC_FOUNDRY_*_DEPLOYMENT names.")
        orchestrator, recipe_nutrition, extraction = names
        assert orchestrator and recipe_nutrition and extraction
        return cls(orchestrator, recipe_nutrition, extraction)


def _project_openai_client(endpoint: str, credential: TokenCredential) -> _OpenAIClient:
    client = AIProjectClient(endpoint=endpoint, credential=credential)
    openai_client: _OpenAIClient = client.get_openai_client()
    return openai_client


class FoundryModelClient:
    """Calls the configured New Foundry deployments through the project's Responses API."""

    def __init__(
        self,
        settings: Settings,
        openai_client_factory: Callable[[str], _OpenAIClient] | None = None,
    ) -> None:
        if settings.foundry_project_endpoint is None:
            raise ConfigurationError("Set ASC_FOUNDRY_PROJECT_ENDPOINT.")
        self._endpoint = str(settings.foundry_project_endpoint)
        self._deployments = FoundryDeployments.from_settings(settings)
        if openai_client_factory is None:
            credential = build_credential(settings)
            openai_client_factory = partial(_project_openai_client, credential=credential)
        self._factory = openai_client_factory
        self._client: _OpenAIClient | None = None

    @property
    def deployments(self) -> FoundryDeployments:
        return self._deployments

    def invoke(self, role: ModelRole, prompt: str, max_output_tokens: int = 64) -> str:
        deployment = self._deployments.for_role(role)
        try:
            if self._client is None:
                self._client = self._factory(self._endpoint)
            response = self._client.responses.create(
                model=deployment, input=prompt, max_output_tokens=max_output_tokens
            )
            text: str = response.output_text
        except Exception as exc:
            raise FoundryInvocationError(
                f"Foundry call for {role.value} failed: {type(exc).__name__}."
            ) from None
        return text

    def check_all(self) -> dict[ModelRole, str]:
        """One tiny call per deployment, to catch wiring and authorization mistakes."""
        return {role: self.invoke(role, "Reply with the single word: ready.") for role in ModelRole}
