"""Managed-agent gateway for New Foundry Responses (not classic Threads/Runs)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from time import monotonic
from typing import TypeVar

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
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import (
    FoundryFailure,
    FoundryGatewayError,
    FoundryRequest,
    FoundryResult,
)
from sous_chef.domain.orchestrator import (
    POLICY,
    CoordinationRequest,
    CoordinationStep,
    OpaqueReasoning,
    OrchestratorAnswer,
    SpecialistArguments,
    SpecialistCall,
    TokenUsage,
)
from sous_chef.domain.recipes import (
    GroundedRecipeGenerationRequest,
    RecipeDraft,
    RecipeGenerationRequest,
    RecipeGrounding,
    RecipeToolError,
    WebCitation,
    WebDraft,
    WebGrounding,
    WebGroundingRequest,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id

FOUNDRY_API_VERSION = "v1"
Result = TypeVar("Result")


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
        return await self._execute(request, self._result)

    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
        if request.request.role != AgentRole.ORCHESTRATOR:
            raise ConfigurationError("Only the Orchestrator can coordinate the specialist.")
        self._require_agent_pins()
        return await self._execute(request.request, self._coordination_result, request)

    def _require_agent_pins(self) -> None:
        if any(
            principal is None
            for principal in (
                self._settings.foundry_orchestrator_agent_principal_id,
                self._settings.foundry_recipe_nutrition_agent_principal_id,
                self._settings.app_workload_principal_id,
            )
        ):
            raise ConfigurationError("Specialist coordination requires agent and workload pins.")

    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]:
        self._require_agent_pins()
        return await self._execute(request, self._recipe_result)

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        self._require_agent_pins()
        return await self._execute(request, self._grounded_recipe_result)

    async def ground_web(self, request: WebGroundingRequest) -> tuple[WebGrounding, TokenUsage]:
        self._require_agent_pins()
        return await self._execute(request, self._web_result)

    async def _execute(
        self,
        request: FoundryRequest,
        parse: Callable[[FoundryRequest, Response], Result],
        coordination: CoordinationRequest | None = None,
    ) -> Result:
        target = self._targets[request.role]
        started = monotonic()
        token = trace_id.set(request.operation_id.hex)
        try:
            if coordination is not None:
                operational_event(
                    Event.ORCHESTRATOR_INVOKED,
                    agent_name=target.name,
                    agent_version=target.version,
                    agent_principal_id=self._settings.foundry_orchestrator_agent_principal_id,
                )
            elif isinstance(request, (RecipeGenerationRequest, WebGroundingRequest)):
                operational_event(
                    Event.RECIPE_INVOKED,
                    agent_name=target.name,
                    agent_version=target.version,
                    agent_principal_id=self._settings.foundry_recipe_nutrition_agent_principal_id,
                )
            try:
                async with asyncio.timeout(self._settings.foundry_invocation_timeout_seconds):
                    if coordination is not None:
                        self._check_coordination_input(coordination)
                    elif isinstance(request, WebGroundingRequest):
                        self._check_web_input(request)
                    elif isinstance(request, RecipeGenerationRequest):
                        self._check_recipe_input(
                            request,
                            body=self._grounded_recipe_body()
                            if isinstance(request, GroundedRecipeGenerationRequest)
                            else None,
                            reserved_tool_tokens=self._settings.foundry_iq_max_context_tokens
                            if isinstance(request, GroundedRecipeGenerationRequest)
                            else 0,
                        )
                    async with self._factory(self._settings) as client:
                        body: dict[str, object] = {
                            "agent_reference": {
                                "type": "agent_reference",
                                "name": target.name,
                                "version": target.version,
                            }
                        }
                        if coordination is not None:
                            body.update(self._coordination_body(coordination))
                        elif isinstance(request, WebGroundingRequest):
                            body.update(self._web_body())
                        elif isinstance(request, GroundedRecipeGenerationRequest):
                            body.update(self._grounded_recipe_body())
                        elif isinstance(request, RecipeGenerationRequest):
                            body.update(self._recipe_body())
                        response = await client.responses.create(
                            input=request.prompt,
                            max_output_tokens=request.max_output_tokens,
                            extra_body=body,
                            extra_headers={"X-Client-Request-Id": str(request.operation_id)},
                        )
                        self._completed(request, response)
                        result = parse(request, response)
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
            except RecipeToolError as exc:
                raise FoundryGatewayError(exc.code, request) from None
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
    def _web_body() -> dict[str, object]:
        from sous_chef.recipe_definition import (
            WEB_GROUNDING_INSTRUCTIONS,
            web_output_format,
            web_search_tool,
        )

        return {
            "instructions": WEB_GROUNDING_INSTRUCTIONS,
            "tools": [web_search_tool().as_dict()],
            "tool_choice": "required",
            "max_tool_calls": 1,
            "parallel_tool_calls": False,
            "store": False,
            "include": ["web_search_call.action.sources"],
            "text": {"format": web_output_format().as_dict()},
        }

    @staticmethod
    def _check_web_input(request: WebGroundingRequest) -> None:
        from sous_chef.services.context import _encoding

        serialized = request.prompt + json.dumps(FoundryGateway._web_body())
        tokens = len(_encoding().encode(serialized, disallowed_special=())) + 64
        if tokens > request.max_input_tokens:
            raise FoundryGatewayError(FoundryFailure.REJECTED, request)

    @staticmethod
    def _web_result(request: FoundryRequest, response: Response) -> tuple[WebGrounding, TokenUsage]:
        from sous_chef.services.context import _encoding

        assert isinstance(request, WebGroundingRequest)
        calls = [item for item in response.output if item.type == "web_search_call"]
        messages = [item for item in response.output if item.type == "message"]
        if (
            len(calls) != 1
            or len(messages) != 1
            or any(
                item.type not in {"reasoning", "message", "web_search_call"}
                for item in response.output
            )
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        call = calls[0]
        if call.status == "failed":
            raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request)
        try:
            usage = response.usage
            action = call.action
            message = messages[0]
            if (
                usage is None
                or usage.input_tokens > request.max_input_tokens
                or usage.output_tokens > request.max_output_tokens
                or call.status != "completed"
                or action.type != "search"
                or (action.queries if action.queries is not None else [action.query])
                != [request.prompt]
                or action.query not in (None, request.prompt)
                or message.status != "completed"
                or len(message.content) != 1
                or message.content[0].type != "output_text"
                or len(message.content[0].annotations) > 10
                or len(response.output_text) > 6000
                or len(action.sources or []) > 20
            ):
                raise ValueError
            raw_evidence = call.model_dump_json() + message.model_dump_json()
            if (
                len(_encoding().encode(raw_evidence, disallowed_special=()))
                > POLICY.max_tool_result_tokens
            ):
                raise ValueError
            draft = WebDraft.model_validate_json(response.output_text)
            if not draft.claims:
                raise FoundryGatewayError(FoundryFailure.NO_RESULTS, request)
            searched = {source.url for source in action.sources or []}
            native: dict[str, str] = {}
            for annotation in message.content[0].annotations:
                if (
                    annotation.type != "url_citation"
                    or not 0
                    <= annotation.start_index
                    < annotation.end_index
                    <= len(response.output_text)
                ):
                    raise ValueError
                native[annotation.url] = annotation.title
            urls = tuple(dict.fromkeys(claim.source_url for claim in draft.claims))
            if not set(urls) <= searched & native.keys():
                raise ValueError
            grounding = WebGrounding(
                claims=draft.claims,
                citations=tuple(WebCitation(url=url, title=native[url]) for url in urls),
                retrieved_at=datetime.now(UTC),
            )
            if (
                len(_encoding().encode(grounding.model_dump_json(), disallowed_special=()))
                > POLICY.max_tool_result_tokens
            ):
                raise ValueError
            return grounding, TokenUsage(
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
            )
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

    @staticmethod
    def _recipe_body() -> dict[str, object]:
        from sous_chef.recipe_definition import recipe_output_format

        return {
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
            "store": False,
            "text": {"format": recipe_output_format().as_dict()},
        }

    def _grounded_recipe_body(self) -> dict[str, object]:
        from sous_chef.recipe_definition import recipe_knowledge_tool

        config = RecipeKnowledgeConfig.from_settings(self._settings)
        body = self._recipe_body()
        body.update(
            tools=[recipe_knowledge_tool(config.mcp_endpoint, config.connection_name).as_dict()],
            tool_choice="required",
            max_tool_calls=1,
        )
        return body

    @staticmethod
    def _check_recipe_input(
        request: RecipeGenerationRequest,
        *,
        body: dict[str, object] | None = None,
        reserved_tool_tokens: int = 0,
    ) -> None:
        from sous_chef.recipe_definition import RECIPE_INSTRUCTIONS
        from sous_chef.services.context import _encoding

        encoding = _encoding()
        serialized = (
            RECIPE_INSTRUCTIONS
            + request.prompt
            + json.dumps(FoundryGateway._recipe_body() if body is None else body)
        )
        tokens = len(encoding.encode(serialized, disallowed_special=())) + 64 + reserved_tool_tokens
        tool_tokens = len(encoding.encode(request.grounding_json, disallowed_special=()))
        if tokens > request.max_input_tokens or tool_tokens > POLICY.max_tool_result_tokens:
            raise FoundryGatewayError(FoundryFailure.REJECTED, request)

    @staticmethod
    def _recipe_result(
        request: FoundryRequest, response: Response
    ) -> tuple[RecipeDraft, TokenUsage]:
        assert isinstance(request, RecipeGenerationRequest)
        try:
            usage = response.usage
            actions = [item for item in response.output if item.type != "reasoning"]
            if (
                usage is None
                or usage.input_tokens > request.max_input_tokens
                or usage.output_tokens > request.max_output_tokens
                or len(actions) != 1
                or actions[0].type != "message"
                or actions[0].status != "completed"
                or len(actions[0].content) != 1
                or actions[0].content[0].type != "output_text"
            ):
                raise ValueError
            return (
                RecipeDraft.model_validate_json(response.output_text),
                TokenUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            )
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

    def _grounded_recipe_result(
        self, request: FoundryRequest, response: Response
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        from sous_chef.recipe_definition import RECIPE_KNOWLEDGE_LABEL, RECIPE_KNOWLEDGE_TOOL
        from sous_chef.services.recipe_grounding import parse_mcp_grounding

        assert isinstance(request, GroundedRecipeGenerationRequest)
        calls = [item for item in response.output if item.type == "mcp_call"]
        allowed = {"reasoning", "message", "mcp_list_tools", "mcp_call"}
        if (
            len(calls) != 1
            or any(item.type not in allowed for item in response.output)
            or any(
                item.server_label != RECIPE_KNOWLEDGE_LABEL or item.error is not None
                for item in response.output
                if item.type == "mcp_list_tools"
            )
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        call = calls[0]
        if call.server_label != RECIPE_KNOWLEDGE_LABEL or call.name != RECIPE_KNOWLEDGE_TOOL:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        if call.error is not None:
            code = (
                self._http_failure(call.error.code)
                if call.error.type == "http_error"
                else FoundryFailure.UNAVAILABLE
            )
            raise FoundryGatewayError(code, request)
        if call.output is None or json.loads(call.arguments) != {"query": request.retrieval_query}:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        grounding = parse_mcp_grounding(
            call.output,
            max_results=self._settings.foundry_iq_max_results,
            max_context_tokens=self._settings.foundry_iq_max_context_tokens,
        )
        if grounding.status == "empty":
            raise FoundryGatewayError(FoundryFailure.NO_RESULTS, request)
        recipe_response = response.model_copy(
            update={
                "output": [
                    item for item in response.output if item.type in {"reasoning", "message"}
                ]
            }
        )
        draft, usage = self._recipe_result(request, recipe_response)
        return draft, grounding, usage

    @staticmethod
    def _check_coordination_input(request: CoordinationRequest) -> None:
        from sous_chef.orchestrator_definition import ORCHESTRATOR_INSTRUCTIONS
        from sous_chef.services.context import _encoding

        body = FoundryGateway._coordination_body(request)
        body.setdefault("input", request.request.prompt)
        serialized = ORCHESTRATOR_INSTRUCTIONS + json.dumps(body, ensure_ascii=False)
        encoding = _encoding()
        tokens = len(encoding.encode(serialized, disallowed_special=())) + 64
        tool_tokens = len(encoding.encode(request.tool_output or "", disallowed_special=()))
        if tokens > request.max_input_tokens or tool_tokens > POLICY.max_tool_result_tokens:
            raise FoundryGatewayError(FoundryFailure.REJECTED, request.request)

    @staticmethod
    def _coordination_body(request: CoordinationRequest) -> dict[str, object]:
        from sous_chef.orchestrator_definition import specialist_tool

        body: dict[str, object] = {
            "tools": [specialist_tool().as_dict()],
            "parallel_tool_calls": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        call = request.previous_call
        if call is not None:
            body["input"] = [
                {"role": "user", "content": request.request.prompt},
                *[
                    {"type": "reasoning", "summary": [], **item.model_dump()}
                    for item in call.reasoning
                ],
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": "recipe_specialist",
                    "arguments": call.arguments.model_dump_json(),
                },
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": request.tool_output,
                },
            ]
        return body

    @staticmethod
    def _coordination_result(request: FoundryRequest, response: Response) -> CoordinationStep:
        usage = response.usage
        if usage is None:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        try:
            accounting = TokenUsage(
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
            )
            # Reasoning items are not exposed as explanations or tool calls.
            actions = [item for item in response.output if item.type != "reasoning"]
            if len(actions) != 1:
                raise ValueError
            action = actions[0]
            if action.type in {"function_call", "message"} and action.status not in {
                None,
                "completed",
            }:
                raise ValueError
            if action.type == "function_call":
                if action.name != "recipe_specialist":
                    raise ValueError
                return CoordinationStep(
                    usage=accounting,
                    call=SpecialistCall(
                        call_id=action.call_id,
                        arguments=SpecialistArguments.model_validate_json(action.arguments),
                        reasoning=tuple(
                            OpaqueReasoning(
                                id=item.id,
                                encrypted_content=item.encrypted_content or "",
                            )
                            for item in response.output
                            if item.type == "reasoning"
                        ),
                    ),
                )
            if action.type != "message":
                raise ValueError
            return CoordinationStep(
                usage=accounting,
                answer=OrchestratorAnswer.model_validate_json(response.output_text),
            )
        except (ValueError, ValidationError):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

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
    def _completed(request: FoundryRequest, response: Response) -> None:
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

    @staticmethod
    def _result(request: FoundryRequest, response: Response) -> FoundryResult:
        try:
            return FoundryResult(
                role=request.role,
                operation_id=request.operation_id,
                response_id=response.id,
                text=response.output_text,
            )
        except ValidationError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
