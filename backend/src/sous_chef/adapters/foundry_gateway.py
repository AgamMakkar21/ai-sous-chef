"""Managed-agent gateway for New Foundry Responses (not classic Threads/Runs)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from time import monotonic
from typing import TypeVar

from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials_async import AsyncTokenCredential
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
    omit,
)
from openai.types.responses import Response
from opentelemetry import trace
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
    CoordinationRequest,
    CoordinationStep,
    OpaqueReasoning,
    OrchestratorAnswer,
    SpecialistArguments,
    SpecialistCall,
    TokenUsage,
)
from sous_chef.domain.recipes import (
    CalorieEstimate,
    CalorieGroundingRequest,
    CombinedRecipeDraft,
    CombinedRecipeGenerationRequest,
    CombinedRecipeOutput,
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
from sous_chef.recipe_definition import RecipePhase
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id
from sous_chef.tracing import Operation, foundry_trace_headers, model_usage, observed

FOUNDRY_API_VERSION = "v1"
MAX_TOOL_ARGUMENT_CHARACTERS = 32_768
MAX_MCP_ENVELOPE_CHARACTERS = 65_536
Result = TypeVar("Result")


def validate_recipe_mcp_envelopes(
    listings: Sequence[str],
    calls: Sequence[str],
    *,
    max_tool_result_tokens: int | None = None,
) -> None:
    if sum(map(len, (*listings, *calls))) > MAX_MCP_ENVELOPE_CHARACTERS:
        raise ValueError("Recipe MCP response exceeds the runtime envelope budget.")


def parse_tool_arguments(value: str) -> dict[str, object]:
    def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        fields: dict[str, object] = {}
        for key, item in pairs:
            if key in fields:
                raise ValueError("Duplicate tool argument.")
            fields[key] = item
        return fields

    if len(value) > MAX_TOOL_ARGUMENT_CHARACTERS:
        raise ValueError("Tool arguments exceed the transport limit.")
    try:
        parsed: object = json.loads(value, object_pairs_hook=unique_fields)
    except RecursionError:
        raise ValueError("Tool arguments exceed the nesting limit.") from None
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be an object.")
    return parsed


@dataclass(frozen=True)
class AgentTarget:
    name: str
    version: str


@asynccontextmanager
async def _project_client(
    settings: Settings, *, credential: AsyncTokenCredential | None = None
) -> AsyncIterator[AsyncOpenAI]:
    async with credential if credential is not None else build_async_credential(settings) as active:
        async with AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint),
            credential=active,
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
            "ASC_FOUNDRY_RECIPE_NUTRITION_RETRIEVAL_AGENT_VERSION": (
                settings.foundry_recipe_nutrition_retrieval_agent_version
            ),
            "ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION": (
                settings.foundry_recipe_nutrition_web_agent_version
            ),
        }
        if settings.foundry_recipe_nutrition_mode == "combined":
            required.pop("ASC_FOUNDRY_RECIPE_NUTRITION_RETRIEVAL_AGENT_VERSION")
            required.pop("ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION")
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
        self._recipe_targets = {
            RecipePhase.GENERATION: self._targets[AgentRole.RECIPE_NUTRITION],
            RecipePhase.COMBINED: self._targets[AgentRole.RECIPE_NUTRITION],
        }
        if settings.foundry_recipe_nutrition_calorie_agent_version is not None:
            self._recipe_targets[RecipePhase.CALORIE] = AgentTarget(
                settings.foundry_recipe_nutrition_agent_name,
                settings.foundry_recipe_nutrition_calorie_agent_version,
            )
        if settings.foundry_recipe_nutrition_mode == "phased":
            assert settings.foundry_recipe_nutrition_retrieval_agent_version is not None
            assert settings.foundry_recipe_nutrition_web_agent_version is not None
            self._recipe_targets.update(
                {
                    RecipePhase.RETRIEVAL: AgentTarget(
                        settings.foundry_recipe_nutrition_agent_name,
                        settings.foundry_recipe_nutrition_retrieval_agent_version,
                    ),
                    RecipePhase.WEB: AgentTarget(
                        settings.foundry_recipe_nutrition_agent_name,
                        settings.foundry_recipe_nutrition_web_agent_version,
                    ),
                }
            )
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
        if isinstance(request, GroundedRecipeGenerationRequest):
            raise ConfigurationError("Grounded requests must use their grounded recipe route.")
        self._require_recipe_mode("phased")
        self._require_agent_pins()
        return await self._execute(request, self._recipe_result)

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        if isinstance(request, CombinedRecipeGenerationRequest):
            raise ConfigurationError("Combined requests must use the combined recipe route.")
        self._require_recipe_mode("phased")
        self._require_agent_pins()
        return await self._execute(request, self._grounded_recipe_result)

    def _require_recipe_mode(self, mode: str) -> None:
        if self._settings.foundry_recipe_nutrition_mode != mode:
            raise ConfigurationError("The requested recipe route does not match its source mode.")

    async def generate_combined_recipe(
        self,
        request: CombinedRecipeGenerationRequest,
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]:
        self._require_recipe_mode("combined")
        self._require_agent_pins()
        return await self._execute(request, self._combined_recipe_result)

    async def ground_web(
        self, request: WebGroundingRequest
    ) -> tuple[WebGrounding | None, TokenUsage]:
        self._require_recipe_mode("phased")
        self._require_agent_pins()
        return await self._execute(request, self._web_result)

    async def estimate_calories(
        self, request: CalorieGroundingRequest
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]:
        self._require_agent_pins()
        if not request.use_web and RecipePhase.CALORIE not in self._recipe_targets:
            raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request)
        return await self._execute(request, self._calorie_result)

    @observed(Operation.FOUNDRY)
    async def _execute(
        self,
        request: FoundryRequest,
        parse: Callable[[FoundryRequest, Response], Result],
        coordination: CoordinationRequest | None = None,
    ) -> Result:
        target = (
            self._recipe_targets[RecipePhase.CALORIE]
            if isinstance(request, CalorieGroundingRequest) and not request.use_web
            else self._recipe_targets[RecipePhase.COMBINED]
            if isinstance(request, CombinedRecipeGenerationRequest)
            or (
                isinstance(request, CalorieGroundingRequest)
                and self._settings.foundry_recipe_nutrition_mode == "combined"
            )
            else self._recipe_targets[RecipePhase.WEB]
            if isinstance(request, WebGroundingRequest)
            else self._recipe_targets[RecipePhase.RETRIEVAL]
            if isinstance(request, GroundedRecipeGenerationRequest)
            else self._targets[request.role]
        )
        span = trace.get_current_span()
        span.set_attribute("gen_ai.provider.name", "azure.ai.foundry")
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", target.name)
        span.set_attribute("asc.agent.version", target.version)
        model = (
            self._settings.foundry_orchestrator_deployment
            if request.role == AgentRole.ORCHESTRATOR
            else self._settings.foundry_recipe_nutrition_deployment
        )
        if model is not None:
            span.set_attribute("gen_ai.request.model", model)
        span.set_attribute(
            "asc.foundry.phase",
            "combined"
            if isinstance(request, CombinedRecipeGenerationRequest)
            else "web"
            if isinstance(request, WebGroundingRequest)
            else "retrieval"
            if isinstance(request, GroundedRecipeGenerationRequest)
            else "generation"
            if isinstance(request, RecipeGenerationRequest)
            else "coordination",
        )
        started = monotonic()
        token = trace_id.set(trace_id.get() or request.operation_id.hex)
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
                        elif isinstance(request, CalorieGroundingRequest) and not request.use_web:
                            body.update(self._recipe_body())
                        elif isinstance(request, WebGroundingRequest):
                            body.update(self._web_body())
                        elif isinstance(request, GroundedRecipeGenerationRequest):
                            body.update(
                                self._combined_recipe_body(request)
                                if isinstance(request, CombinedRecipeGenerationRequest)
                                else self._grounded_recipe_body()
                            )
                        elif isinstance(request, RecipeGenerationRequest):
                            body.update(self._recipe_body())
                        response = await client.responses.create(
                            input=request.prompt,
                            max_output_tokens=request.max_output_tokens
                            if request.max_output_tokens is not None
                            else omit,
                            extra_body=body,
                            extra_headers={
                                "X-Client-Request-Id": str(request.operation_id),
                                **foundry_trace_headers(),
                            },
                        )
                        if response.usage is not None:
                            model_usage(response.usage.input_tokens, response.usage.output_tokens)
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
        return {
            "max_tool_calls": 1,
            "store": False,
            "include": ["web_search_call.action.sources"],
        }

    @staticmethod
    def _web_result(
        request: FoundryRequest, response: Response
    ) -> tuple[WebGrounding | None, TokenUsage]:
        draft, native, usage = FoundryGateway._web_evidence(request, response)
        assert isinstance(request, WebGroundingRequest)
        if draft.calorie_estimates:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        if not draft.claims:
            if request.allow_empty:
                return None, usage
            raise FoundryGatewayError(FoundryFailure.NO_RESULTS, request)
        urls = tuple(dict.fromkeys(claim.source_url for claim in draft.claims))
        return (
            WebGrounding(
                claims=draft.claims,
                citations=tuple(WebCitation(url=url, title=native[url]) for url in urls),
                retrieved_at=datetime.now(UTC),
            ),
            usage,
        )

    def _calorie_result(
        self, request: FoundryRequest, response: Response
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]:
        assert isinstance(request, CalorieGroundingRequest)
        draft, _, usage = self._web_evidence(
            request,
            response,
            combined=self._settings.foundry_recipe_nutrition_mode == "combined" and request.use_web,
        )
        ids = [item.ingredient_line_id for item in draft.calorie_estimates]
        if (
            draft.claims
            or len(ids) != len(set(ids))
            or not set(ids) <= set(request.ingredient_line_ids)
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        return draft.calorie_estimates, usage

    @staticmethod
    def _web_evidence(
        request: FoundryRequest, response: Response, *, combined: bool = False
    ) -> tuple[WebDraft, dict[str, str], TokenUsage]:

        assert isinstance(request, WebGroundingRequest)
        calls = [item for item in response.output if item.type == "web_search_call"]
        messages = [item for item in response.output if item.type == "message"]
        listings = [item for item in response.output if item.type == "mcp_list_tools"]
        use_web = not isinstance(request, CalorieGroundingRequest) or request.use_web
        allowed = {"reasoning", "message", "web_search_call"}
        if combined:
            allowed.add("mcp_list_tools")
        if (
            len(calls) != int(use_web)
            or len(messages) != 1
            or len(listings) > 1
            or any(item.type not in allowed for item in response.output)
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        call = calls[0] if calls else None
        if (
            call is not None
            and call.status == "failed"
            and not isinstance(request, CalorieGroundingRequest)
        ):
            raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request)
        try:
            if listings:
                from sous_chef.recipe_definition import RECIPE_KNOWLEDGE_LABEL

                if any(
                    item.server_label != RECIPE_KNOWLEDGE_LABEL or item.error is not None
                    for item in listings
                ):
                    raise ValueError
                validate_recipe_mcp_envelopes(
                    [item.model_dump_json() for item in listings],
                    [],
                )
            usage = response.usage
            action = call.action if call is not None else None
            message = messages[0]
            if (
                usage is None
                or (
                    call is not None
                    and (
                        call.status
                        not in (
                            {"completed", "failed"}
                            if isinstance(request, CalorieGroundingRequest)
                            else {"completed"}
                        )
                        or action is None
                        or action.type != "search"
                        or (action.queries if action.queries is not None else [action.query])
                        != [
                            request.query
                            if isinstance(request, CalorieGroundingRequest)
                            else request.prompt
                        ]
                        or action.query
                        not in (
                            None,
                            request.query
                            if isinstance(request, CalorieGroundingRequest)
                            else request.prompt,
                        )
                    )
                )
                or message.status != "completed"
                or len(message.content) != 1
                or message.content[0].type != "output_text"
                or len(message.content[0].annotations)
                > (30 if isinstance(request, CalorieGroundingRequest) else 10)
                or len(response.output_text)
                > (65536 if isinstance(request, CalorieGroundingRequest) else 16000)
                or (
                    action is not None
                    and action.type == "search"
                    and len(action.sources or [])
                    > (30 if isinstance(request, CalorieGroundingRequest) else 20)
                )
                or (not use_web and message.content[0].annotations)
            ):
                raise ValueError
            raw_evidence = (
                call.model_dump_json() if call is not None else ""
            ) + message.model_dump_json()
            if len(raw_evidence) > (
                131072 if isinstance(request, CalorieGroundingRequest) else 65536
            ):
                raise ValueError
            if combined:
                output = CombinedRecipeOutput.model_validate_json(response.output_text)
                if output.recipe is not None:
                    raise ValueError
                draft = WebDraft(claims=(), calorie_estimates=output.calorie_estimates)
            else:
                draft = WebDraft.model_validate_json(response.output_text)
            searched = (
                {source.url for source in action.sources or []}
                if action is not None and action.type == "search"
                else set()
            )
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
            references = {
                item.reference for item in draft.calorie_estimates if item.source == "web_iq"
            }
            if not (set(urls) | references) <= searched & native.keys() or (
                call is not None and call.status == "failed" and (urls or references)
            ):
                raise ValueError
            return (
                draft,
                native,
                TokenUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            )
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

    @staticmethod
    def _recipe_body() -> dict[str, object]:
        return {"store": False}

    def _grounded_recipe_body(self) -> dict[str, object]:
        body = self._recipe_body()
        body["max_tool_calls"] = 1
        return body

    def _combined_recipe_body(self, request: CombinedRecipeGenerationRequest) -> dict[str, object]:
        body = self._grounded_recipe_body()
        body["max_tool_calls"] = 2 if request.web_query is not None else 1
        body["include"] = ["web_search_call.action.sources"]
        return body

    @staticmethod
    def _recipe_result(
        request: FoundryRequest,
        response: Response,
        *,
        grounding: RecipeGrounding | None = None,
    ) -> tuple[RecipeDraft, TokenUsage]:
        assert isinstance(request, RecipeGenerationRequest)
        try:
            usage = response.usage
            actions = [item for item in response.output if item.type != "reasoning"]
            if (
                usage is None
                or len(actions) != 1
                or actions[0].type != "message"
                or actions[0].status != "completed"
                or len(actions[0].content) != 1
                or actions[0].content[0].type != "output_text"
            ):
                raise ValueError
            payload = json.loads(response.output_text)
            if not isinstance(payload, dict):
                raise ValueError
            if isinstance(request, CombinedRecipeGenerationRequest):
                if (
                    set(payload) != {"recipe", "calorieEstimates"}
                    or payload["calorieEstimates"] != []
                    or not isinstance(payload["recipe"], dict)
                ):
                    raise ValueError
                payload = payload["recipe"]
            if grounding is not None:
                # Attribution is tool-owned; optional generated quotations cannot veto a recipe.
                sources = {item.source.source_id: item.snippet for item in grounding.recipes}
                claims = payload.get("sourceClaims")
                verified = (
                    [
                        claim
                        for claim in claims
                        if isinstance(claim, dict)
                        and set(claim) == {"sourceId", "quote"}
                        and isinstance(claim["sourceId"], str)
                        and claim["sourceId"] in sources
                        and isinstance(claim["quote"], str)
                        and 0 < len(claim["quote"].strip()) <= 240
                        and claim["quote"] in sources[claim["sourceId"]]
                    ][:10]
                    if isinstance(claims, list)
                    else []
                )
                if payload.get("sourceIds") != list(sources) or claims != verified:
                    operational_event(Event.RECIPE_ATTRIBUTION_NORMALIZED)
                payload.update(sourceIds=list(sources), sourceClaims=verified)
            model = (
                CombinedRecipeDraft
                if isinstance(request, CombinedRecipeGenerationRequest)
                else RecipeDraft
            )
            return (
                model.model_validate_json(json.dumps(payload)),
                TokenUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            )
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

    def _recipe_grounding(self, request: FoundryRequest, response: Response) -> RecipeGrounding:
        from sous_chef.recipe_definition import RECIPE_KNOWLEDGE_LABEL, RECIPE_KNOWLEDGE_TOOL
        from sous_chef.services.recipe_grounding import (
            parse_mcp_grounding,
            recipe_knowledge_arguments,
        )

        assert isinstance(request, GroundedRecipeGenerationRequest)
        calls = [item for item in response.output if item.type == "mcp_call"]
        listings = [item for item in response.output if item.type == "mcp_list_tools"]
        allowed = {"reasoning", "message", "mcp_list_tools", "mcp_call"}
        if isinstance(request, CombinedRecipeGenerationRequest):
            allowed.add("web_search_call")
        if (
            len(calls) != 1
            or len(listings) > 1
            or any(item.type not in allowed for item in response.output)
            or any(
                item.server_label != RECIPE_KNOWLEDGE_LABEL or item.error is not None
                for item in listings
            )
        ):
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
        knowledge = RecipeKnowledgeConfig.from_settings(self._settings)
        try:
            validate_recipe_mcp_envelopes(
                [item.model_dump_json() for item in listings],
                [item.model_dump_json() for item in calls],
                max_tool_result_tokens=knowledge.mcp_max_context_tokens,
            )
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
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
        try:
            if call.output is None or parse_tool_arguments(call.arguments) != (
                recipe_knowledge_arguments(request.retrieval_query)
            ):
                raise ValueError
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
        grounding = parse_mcp_grounding(
            call.output,
            max_results=knowledge.max_results,
            max_context_tokens=knowledge.mcp_max_context_tokens,
            max_output_characters=knowledge.mcp_max_output_characters,
        )
        if grounding.status == "empty":
            raise FoundryGatewayError(FoundryFailure.NO_RESULTS, request)
        return grounding

    def _grounded_recipe_result(
        self, request: FoundryRequest, response: Response
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        grounding = self._recipe_grounding(request, response)
        recipe_response = response.model_copy(
            update={
                "output": [
                    item for item in response.output if item.type in {"reasoning", "message"}
                ]
            }
        )
        draft, usage = self._recipe_result(request, recipe_response, grounding=grounding)
        return draft, grounding, usage

    def _combined_recipe_result(
        self,
        request: FoundryRequest,
        response: Response,
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]:
        assert isinstance(request, CombinedRecipeGenerationRequest)
        try:
            combined, grounding, usage = self._grounded_recipe_result(request, response)
            assert isinstance(combined, CombinedRecipeDraft)
            messages = [item for item in response.output if item.type == "message"]
            calls = [item for item in response.output if item.type == "web_search_call"]
            if len(messages) != 1 or len(messages[0].content) != 1:
                raise ValueError
            message = messages[0]
            content = message.content[0]
            if content.type != "output_text" or len(content.annotations) > 10:
                raise ValueError
            web = None
            if request.web_query is None:
                if calls or combined.web_claims or content.annotations:
                    raise ValueError
            else:
                if len(calls) != 1 or calls[0].status != "completed":
                    raise ValueError
                call = calls[0]
                action = call.action
                if (
                    action.type != "search"
                    or (action.queries if action.queries is not None else [action.query])
                    != [request.web_query]
                    or action.query not in (None, request.web_query)
                    or len(action.sources or []) > 20
                ):
                    raise ValueError
                native: dict[str, str] = {}
                for annotation in content.annotations:
                    if (
                        annotation.type != "url_citation"
                        or not 0
                        <= annotation.start_index
                        < annotation.end_index
                        <= len(content.text)
                    ):
                        raise ValueError
                    native[annotation.url] = annotation.title
                searched = {source.url for source in action.sources or []}
                urls = tuple(dict.fromkeys(claim.source_url for claim in combined.web_claims))
                if not set(urls) <= searched & native.keys():
                    raise ValueError
                raw = (
                    call.model_dump_json()
                    + json.dumps([claim.model_dump() for claim in combined.web_claims])
                    + "".join(annotation.model_dump_json() for annotation in content.annotations)
                )
                if len(raw) > MAX_MCP_ENVELOPE_CHARACTERS:
                    raise ValueError
                if urls:
                    web = WebGrounding(
                        claims=combined.web_claims,
                        citations=tuple(WebCitation(url=url, title=native[url]) for url in urls),
                        retrieved_at=datetime.now(UTC),
                    )
            draft = RecipeDraft.model_validate(combined.model_dump(exclude={"web_claims"}))
            return draft, grounding, web, usage
        except ValueError:
            raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None

    @staticmethod
    def _coordination_body(request: CoordinationRequest) -> dict[str, object]:
        body: dict[str, object] = {
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
                        arguments=SpecialistArguments.model_validate(
                            parse_tool_arguments(action.arguments)
                        ),
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
            if (
                action.type != "message"
                or action.status != "completed"
                or len(action.content) != 1
                or action.content[0].type != "output_text"
            ):
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
