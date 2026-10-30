import json
from collections.abc import Iterator
from dataclasses import replace
from time import time
from types import TracebackType
from typing import Any
from unittest.mock import Mock, create_autospec
from urllib.parse import urlsplit

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.core.pipeline.transport import HttpTransport, RequestsTransport
from azure.core.rest import HttpRequest, HttpResponse
from requests import Session
from sous_chef.adapters.foundry_iq import (
    ARM_SCOPE,
    SEARCH_API_VERSION,
    SEARCH_SCOPE,
    FoundryRecipeKnowledge,
    RecipeKnowledgeConfig,
)
from sous_chef.domain.recipe_corpus import CorpusRecipe, normalize_row
from sous_chef.services.recipe_indexing import RecipeIndexingError, search_document


class Credential:
    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken("synthetic-test-token", int(time()) + 3600)


def request_body(request: HttpRequest) -> dict[str, Any]:
    assert isinstance(request.content, (str, bytes))
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


class SearchFixture(HttpTransport[HttpRequest, HttpResponse]):
    """Offline HTTP fixture; it verifies wiring, not Azure's relevance ranking."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.requests: list[HttpRequest] = []
        self.fail_status: int | None = None
        self.index_failure = False
        self.malformed_references = False
        self.exception: Exception | None = None
        self.response_override: dict[str, Any] | None = None
        self.response_bytes: bytes | None = None
        self._session = create_autospec(Session, instance=True)
        self._transport = RequestsTransport(session=self._session, session_owner=False)

    def open(self) -> None:
        pass

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "SearchFixture":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close()

    def _respond(
        self, request: HttpRequest, status: int, payload: object, **kwargs: Any
    ) -> HttpResponse:
        response = Mock(
            status_code=status,
            headers={"content-type": "application/json"},
            reason="Synthetic response",
            content=(
                self.response_bytes
                if self.response_bytes is not None
                else json.dumps(
                    self.response_override if self.response_override is not None else payload
                ).encode()
            ),
        )
        response._content_consumed = True
        response.iter_content.return_value = iter([response.content])
        self._session.request.return_value = response
        result = self._transport.send(request, **kwargs)
        assert isinstance(result, HttpResponse)
        return result

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-test-token"
        if self.exception is not None:
            raise self.exception
        if self.fail_status is not None:
            return self._respond(
                request, self.fail_status, {"error": "private-response-token"}, **kwargs
            )
        body = request_body(request)
        path = urlsplit(request.url).path
        if path.startswith("/knowledgebases/") and request.method == "PUT":
            if body["retrieveDefaults"]["maxOutputSizeInTokens"] < 5000:
                return self._respond(
                    request,
                    400,
                    {
                        "error": {
                            "message": (
                                "Knowledge Base MaxOutputSize configuration must be at least 5000"
                            )
                        }
                    },
                    **kwargs,
                )
        if request.method == "PUT":
            return self._respond(request, 201, body, **kwargs)
        if path.endswith("/docs/index"):
            results = []
            for document in body["value"]:
                assert document.pop("@search.action") == "upload"
                self.documents[document["source_id"]] = document
                results.append(
                    {
                        "key": document["source_id"],
                        "status": not self.index_failure,
                        "statusCode": 503 if self.index_failure else 201,
                        "errorMessage": "private-document" if self.index_failure else None,
                    }
                )
            return self._respond(
                request, 207 if self.index_failure else 200, {"value": results}, **kwargs
            )
        assert path.endswith("/retrieve"), "retrieval must use the knowledge base, not index search"
        if "maxOutputSizeInTokens" in body:
            return self._respond(
                request,
                400,
                {"error": {"message": "maxOutputSizeInTokens is not valid for retrieve."}},
                **kwargs,
            )
        query = body["intents"][0]["search"].casefold()
        references = [
            {
                "type": "searchIndex",
                "id": str(number),
                "docKey": key,
                "sourceData": document,
            }
            for number, (key, document) in enumerate(sorted(self.documents.items()))
            if query in document["content"].casefold()
        ]
        if self.malformed_references and references:
            references[0]["docKey"] = "foreign-source"
        return self._respond(request, 200, {"response": [], "references": references}, **kwargs)


@pytest.fixture
def corpus() -> list[CorpusRecipe]:
    rows = [
        ("1", "Synthetic Rice", 'c("rice", "water")'),
        ("2", "Synthetic Toast", 'c("bread")'),
    ]
    recipes = []
    for number, name, ingredients in rows:
        recipe = normalize_row(
            {
                "RecipeId": number,
                "Name": name,
                "RecipeIngredientParts": ingredients,
                "RecipeInstructions": 'c("Prepare the synthetic fixture meal.")',
            },
            int(number),
            "a" * 40,
        )
        assert isinstance(recipe, CorpusRecipe)
        recipes.append(recipe)
    return recipes


@pytest.fixture
def transport() -> SearchFixture:
    return SearchFixture()


@pytest.fixture
def credential() -> Credential:
    return Credential()


@pytest.fixture
def knowledge(transport: SearchFixture, credential: Credential) -> Iterator[FoundryRecipeKnowledge]:
    config = RecipeKnowledgeConfig(
        search_endpoint="https://synthetic.search.windows.net",
        index_name="fixture-recipes",
        knowledge_source_name="fixture-source",
        knowledge_base_name="fixture-knowledge",
        connection_name="fixture-mcp",
        project_resource_id=(
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
            "/providers/Microsoft.CognitiveServices/accounts/synthetic/projects/recipes"
        ),
    )
    client = FoundryRecipeKnowledge(config, credential, transport=transport)
    yield client
    client.close()


def test_refresh_twice_and_retrieve_a_relevant_recipe_with_stable_source_metadata(
    knowledge: FoundryRecipeKnowledge, corpus: list[CorpusRecipe], transport: SearchFixture
) -> None:
    knowledge.configure()
    documents = [search_document(recipe) for recipe in corpus]
    for _ in range(2):
        assert knowledge.index(documents) == 2
    assert sorted(transport.documents) == ["hf-food-recipes-1", "hf-food-recipes-2"]
    citations = knowledge.retrieve("rice")
    assert len(citations) == 1
    assert citations[0].reference_id == "0"
    assert citations[0].recipe == corpus[0]
    assert citations[0].source_url == documents[0]["source_url"]
    assert knowledge.retrieve("rice") == citations
    query = request_body(transport.requests[-1])
    assert query["maxOutputDocuments"] == 3
    assert query["maxRuntimeInSeconds"] == 10
    assert query["maxOutputSize"] == 5000
    assert "maxOutputSizeInTokens" not in query
    assert query["knowledgeSourceParams"][0]["includeReferenceSourceData"] is True


def test_setup_uses_configured_names_and_agent_identity_without_api_keys(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, credential: Credential
) -> None:
    knowledge.configure()
    requests = transport.requests
    assert [urlsplit(request.url).path for request in requests[:3]] == [
        "/indexes/fixture-recipes",
        "/knowledgesources/fixture-source",
        "/knowledgebases/fixture-knowledge",
    ]
    source = request_body(requests[1])
    assert source["searchIndexParameters"]["searchIndexName"] == "fixture-recipes"
    assert {field["name"] for field in source["searchIndexParameters"]["sourceDataFields"]} == {
        "source_id",
        "title",
        "source_url",
        "recipe_json",
    }
    base = request_body(requests[2])
    assert base["models"] == []
    assert base["retrievalReasoningEffort"] == {"kind": "minimal"}
    assert base["retrieveDefaults"]["maxOutputSizeInTokens"] == 5000
    connection = request_body(requests[3])["properties"]
    assert connection["authType"] == "AgenticIdentityToken"
    assert connection["audience"] == "https://search.azure.com/"
    assert connection["category"] == "RemoteTool"
    assert connection["target"] == knowledge.config.mcp_endpoint
    assert connection["isSharedToAll"] is False
    assert requests[3].url.endswith("/connections/fixture-mcp?api-version=2025-10-01-preview")
    assert (SEARCH_SCOPE,) in credential.scopes
    assert (ARM_SCOPE,) in credential.scopes
    for request in requests:
        assert "api-key" not in {key.lower() for key in request.headers}
    assert SEARCH_API_VERSION in requests[0].url


@pytest.mark.parametrize("context_tokens", [256, 512, 4000])
def test_service_minimum_applies_to_mcp_defaults_and_rest_queries_without_raising_local_limit(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, context_tokens: int
) -> None:
    knowledge.config = replace(knowledge.config, max_results=1, max_context_tokens=context_tokens)
    knowledge.configure()
    defaults = request_body(transport.requests[2])["retrieveDefaults"]
    assert defaults["maxOutputDocuments"] == 1
    assert defaults["maxOutputSizeInTokens"] == 5000
    knowledge.retrieve("rice")
    request = request_body(transport.requests[-1])
    assert request["maxOutputDocuments"] == 1
    assert request["maxOutputSize"] == 5000
    assert "maxOutputSizeInTokens" not in request
    assert knowledge.config.max_context_tokens == context_tokens
    semantic = request_body(transport.requests[0])["semantic"]["configurations"][0]
    assert semantic["prioritizedFields"]["prioritizedContentFields"] == [{"fieldName": "grounding"}]


@pytest.mark.parametrize("status", [206, 301, 401, 403, 404, 429, 500])
def test_http_and_partial_retrieval_errors_are_not_empty_successful_results(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, status: int
) -> None:
    transport.fail_status = status
    with pytest.raises(RecipeIndexingError, match=f"HTTP {status}") as error:
        knowledge.retrieve("rice")
    assert "private-response" not in str(error.value)
    assert len(transport.requests) == 1


def test_partial_document_failures_are_explicit_and_replay_is_safe(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, corpus: list[CorpusRecipe]
) -> None:
    transport.index_failure = True
    documents = [search_document(recipe) for recipe in corpus]
    with pytest.raises(RecipeIndexingError, match="some writes may have succeeded") as error:
        knowledge.index(documents)
    assert "private-document" not in str(error.value)
    transport.index_failure = False
    assert knowledge.index(documents) == 2
    assert len(transport.documents) == 2


@pytest.mark.parametrize("error_type", [ServiceRequestError, ClientAuthenticationError])
def test_query_connectivity_errors_are_sanitized_and_not_retried(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, error_type: type[Exception]
) -> None:
    transport.exception = error_type("private-endpoint-token")
    with pytest.raises(RecipeIndexingError, match=error_type.__name__) as error:
        knowledge.retrieve("rice")
    assert "private-endpoint" not in str(error.value)
    assert len(transport.requests) == 1


def test_foreign_citations_are_rejected_instead_of_grounding_on_mismatched_content(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, corpus: list[CorpusRecipe]
) -> None:
    knowledge.index([search_document(recipe) for recipe in corpus])
    transport.malformed_references = True
    with pytest.raises(RecipeIndexingError, match="inconsistent citations"):
        knowledge.retrieve("rice")


def test_no_matches_is_distinct_from_an_error(
    knowledge: FoundryRecipeKnowledge, corpus: list[CorpusRecipe]
) -> None:
    knowledge.index([search_document(recipe) for recipe in corpus])
    assert knowledge.retrieve("no matching recipe") == []


@pytest.mark.parametrize("query", ["", " ", "x" * 2001])
def test_invalid_queries_make_no_external_calls(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, query: str
) -> None:
    with pytest.raises(RecipeIndexingError, match="1 to 2000"):
        knowledge.retrieve(query)
    assert transport.requests == []


def test_batching_limits_each_upload_and_acknowledges_every_stable_key(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, corpus: list[CorpusRecipe]
) -> None:
    recipes = [
        corpus[0].model_copy(
            update={
                "source_id": f"hf-food-recipes-{number}",
                "name": f"Synthetic Recipe {number}",
                "provenance": corpus[0].provenance.model_copy(
                    update={"source_recipe_id": str(number), "row_number": number}
                ),
            }
        )
        for number in range(1, 52)
    ]
    assert knowledge.index([search_document(recipe) for recipe in recipes]) == 51
    assert [len(request_body(request)["value"]) for request in transport.requests] == [50, 1]
    assert len(transport.documents) == 51


@pytest.mark.parametrize("case", ["empty", "duplicate", "oversize"])
def test_invalid_batches_are_rejected_before_writing(
    knowledge: FoundryRecipeKnowledge,
    transport: SearchFixture,
    corpus: list[CorpusRecipe],
    case: str,
) -> None:
    document = search_document(corpus[0])
    documents = [] if case == "empty" else [document] * (1001 if case == "oversize" else 2)
    with pytest.raises(RecipeIndexingError):
        knowledge.index(documents)
    assert transport.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": []},
        {"value": [{"key": "foreign", "status": True, "statusCode": 201}]},
        {"value": [{"key": "hf-food-recipes-1", "status": "true", "statusCode": 201}]},
        {"value": [{"key": "hf-food-recipes-1", "status": False, "statusCode": 400}]},
    ],
)
def test_success_status_does_not_hide_malformed_or_failed_document_acknowledgments(
    knowledge: FoundryRecipeKnowledge,
    transport: SearchFixture,
    corpus: list[CorpusRecipe],
    payload: dict[str, Any],
) -> None:
    transport.response_override = payload
    with pytest.raises(RecipeIndexingError, match="index_recipes"):
        knowledge.index([search_document(corpus[0])])


@pytest.mark.parametrize(
    "payload", [{}, {"references": "private-invalid-references"}, {"references": [{}]}]
)
def test_incomplete_retrieval_payloads_are_not_successful_grounding(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, payload: dict[str, Any]
) -> None:
    transport.response_override = payload
    with pytest.raises(RecipeIndexingError, match="citation metadata") as error:
        knowledge.retrieve("rice")
    assert "private-invalid" not in str(error.value)


def test_malformed_json_is_a_sanitized_error(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture
) -> None:
    transport.response_bytes = b"private-not-json"
    with pytest.raises(RecipeIndexingError, match="invalid JSON") as error:
        knowledge.retrieve("rice")
    assert "private-not-json" not in str(error.value)


@pytest.mark.parametrize("case", ["reference_id", "source_id", "excessive_count"])
def test_repeated_or_excessive_citations_are_rejected(
    knowledge: FoundryRecipeKnowledge,
    transport: SearchFixture,
    corpus: list[CorpusRecipe],
    case: str,
) -> None:
    references = [
        {
            "type": "searchIndex",
            "id": "0" if case == "reference_id" else str(number),
            "docKey": recipe.source_id,
            "sourceData": search_document(recipe),
        }
        for number, recipe in enumerate(
            [corpus[0]] * 6
            if case == "excessive_count"
            else [corpus[0]] * 2
            if case == "source_id"
            else corpus
        )
    ]
    transport.response_override = {"references": references}
    with pytest.raises(RecipeIndexingError, match="invalid citation counts"):
        knowledge.retrieve("rice")


def test_retrieved_recipe_must_have_usable_content(
    knowledge: FoundryRecipeKnowledge, transport: SearchFixture, corpus: list[CorpusRecipe]
) -> None:
    document = search_document(corpus[0])
    document["recipe_json"] = corpus[0].model_copy(update={"instructions": []}).model_dump_json()
    transport.response_override = {
        "references": [
            {
                "type": "searchIndex",
                "id": "0",
                "docKey": corpus[0].source_id,
                "sourceData": document,
            }
        ]
    }
    with pytest.raises(RecipeIndexingError, match="fields are empty"):
        knowledge.retrieve("rice")
