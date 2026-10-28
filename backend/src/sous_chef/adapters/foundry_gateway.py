"""Managed-agent gateway for New Foundry Responses (not classic Threads/Runs)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from json import JSONDecodeError
from time import monotonic

from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
from openai.types.responses import Response
from pydantic import ValidationError

from sous_chef.adapters.credentials import build_async_credential
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import (
    FoundryFailure,
    FoundryGatewayError,
    FoundryRequest,
    FoundryResult,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id

FOUNDRY_API_VERSION = "v1"


@dataclass(frozen=True)
class AgentTarget:
    name: str
    version: str


@asynccontextmanager
async def _project_client(settings: Settings) -> AsyncIterator[AsyncOpenAI]:
    async with build_async_credential(settings) as credential:
        async with AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint),
            credential=credential,
            api_version=FOUNDRY_API_VERSION,
            retry_total=0,
        ) as project:
            async with project.get_openai_client(
                max_retries=0,
                timeout=settings.foundry_invocation_timeout_seconds,
                _strict_response_validation=True,
            ) as client:
                yield client


ClientFactory = Callable[[Settings], AbstractAsyncContextManager[AsyncOpenAI]]


class FoundryGateway:
    """One bounded, non-retried invocation of a pinned managed agent.

    No conversation/previous-response IDs are accepted here: application history and
    user ownership remain backend responsibilities, not caller-supplied Foundry state.
    """

    def __init__(self, settings: Settings, *, client_factory: ClientFactory | None = None) -> None:
        if settings.adapter_mode != "live":
            raise ConfigurationError("The Foundry gateway requires ASC_ADAPTER_MODE=live.")
        if not settings.agents_enabled:
            raise ConfigurationError("Agent execution is disabled for this release.")
        required = {
            "ASC_FOUNDRY_PROJECT_ENDPOINT": settings.foundry_project_endpoint,
            "ASC_FOUNDRY_ORCHESTRATOR_AGENT_NAME": settings.foundry_orchestrator_agent_name,
            "ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION": settings.foundry_orchestrator_agent_version,
            "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_NAME": settings.foundry_recipe_nutrition_agent_name,
            "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION": (
                settings.foundry_recipe_nutrition_agent_version
            ),
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ConfigurationError("Set required gateway settings: " + ", ".join(missing) + ".")
        assert settings.foundry_orchestrator_agent_name is not None
        assert settings.foundry_recipe_nutrition_agent_name is not None
        assert settings.foundry_orchestrator_agent_version is not None
        assert settings.foundry_recipe_nutrition_agent_version is not None
        if settings.foundry_orchestrator_agent_name == settings.foundry_recipe_nutrition_agent_name:
            raise ConfigurationError("Configure different runtime agent names for the two roles.")
        self._targets = {
            AgentRole.ORCHESTRATOR: AgentTarget(
                settings.foundry_orchestrator_agent_name,
                settings.foundry_orchestrator_agent_version,
            ),
            AgentRole.RECIPE_NUTRITION: AgentTarget(
                settings.foundry_recipe_nutrition_agent_name,
                settings.foundry_recipe_nutrition_agent_version,
            ),
        }
        self._settings = settings
        self._factory = client_factory or _project_client

    async def invoke(self, request: FoundryRequest) -> FoundryResult:
        target = self._targets[request.role]
        started = monotonic()
        token = trace_id.set(request.operation_id.hex)
        try:
            try:
                async with asyncio.timeout(self._settings.foundry_invocation_timeout_seconds):
                    async with self._factory(self._settings) as client:
                        response = await client.responses.create(
                            input=request.prompt,
                            max_output_tokens=request.max_output_tokens,
                            extra_body={
                                "agent_reference": {
                                    "type": "agent_reference",
                                    "name": target.name,
                                    "version": target.version,
                                }
                            },
                            extra_headers={"X-Client-Request-Id": str(request.operation_id)},
                        )
                        result = self._result(request, response)
            except (TimeoutError, APITimeoutError):
                raise FoundryGatewayError(FoundryFailure.TIMEOUT, request) from None
            except ClientAuthenticationError:
                raise FoundryGatewayError(FoundryFailure.AUTHENTICATION, request) from None
            except (APIStatusError, HttpResponseError) as exc:
                raise FoundryGatewayError(self._http_failure(exc.status_code), request) from None
            except (APIConnectionError, ServiceRequestError, ServiceResponseError):
                raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request) from None
            except (APIResponseValidationError, JSONDecodeError, UnicodeDecodeError):
                raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
        except FoundryGatewayError as exc:
            operational_event(
                Event.FOUNDRY_FAILED,
                category=ErrorCategory(exc.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise
        except asyncio.CancelledError:
            operational_event(Event.FOUNDRY_CANCELLED, duration_ms=(monotonic() - started) * 1000)
            raise
        else:
            operational_event(Event.FOUNDRY_COMPLETED, duration_ms=(monotonic() - started) * 1000)
            return result
        finally:
            trace_id.reset(token)

    @staticmethod
    def _http_failure(status: int | None) -> FoundryFailure:
        if status in {401, 403}:
            return FoundryFailure.AUTHENTICATION
        if status == 429:
            return FoundryFailure.THROTTLED
        if status in {408, 504}:
            return FoundryFailure.TIMEOUT
        if status is None or status >= 500:
            return FoundryFailure.UNAVAILABLE
        return FoundryFailure.REJECTED

    @staticmethod
    def _result(request: FoundryRequest, response: Response) -> FoundryResult:
        if (
            response.status != "completed"
            or response.error is not None
            or response.incomplete_details is not None
            or any(
                part.type == "refusal"
                for item in response.output
                if item.type == "message"
                for part in item.content
            )
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        try:
            return FoundryResult(
                role=request.role,
                operation_id=request.operation_id,
                response_id=response.id,
                text=response.output_text,
            )
        except ValidationError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
