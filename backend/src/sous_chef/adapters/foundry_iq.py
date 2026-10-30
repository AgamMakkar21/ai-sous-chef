"""Keyless Foundry IQ setup, recipe upserts and cited knowledge-base retrieval."""

from dataclasses import dataclass
from typing import Literal

from azure.core import PipelineClient
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from azure.core.pipeline.policies import (
    BearerTokenCredentialPolicy,
    RedirectPolicy,
    RetryPolicy,
)
from azure.core.pipeline.transport import HttpTransport
from azure.core.rest import HttpRequest, HttpResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sous_chef.domain.recipe_corpus import CorpusRecipe
from sous_chef.services.recipe_indexing import (
    RecipeIndexingError,
    RecipeSearchDocument,
    search_document,
)
from sous_chef.services.recipe_ingestion import MAX_ROWS
from sous_chef.settings import ConfigurationError, Settings

SEARCH_API_VERSION = "2026-08-01-preview"
CONNECTION_API_VERSION = "2025-10-01-preview"
SEMANTIC_CONFIGURATION = "recipe-content"
MIN_RETRIEVAL_OUTPUT_TOKENS = 5000
SEARCH_SCOPE = "https://search.azure.com/.default"
ARM_SCOPE = "https://management.azure.com/.default"
BATCH_SIZE = 50


@dataclass(frozen=True)
class RecipeKnowledgeConfig:
    search_endpoint: str
    index_name: str
    knowledge_source_name: str
    knowledge_base_name: str
    connection_name: str
    project_resource_id: str
    max_results: int = 3
    max_context_tokens: int = 4000

    @property
    def service_output_tokens(self) -> int:
        """Search's output minimum is separate from the local acceptance limit."""
        return max(MIN_RETRIEVAL_OUTPUT_TOKENS, self.max_context_tokens)

    @classmethod
    def from_settings(cls, settings: Settings) -> "RecipeKnowledgeConfig":
        required = {
            "ASC_FOUNDRY_IQ_SEARCH_ENDPOINT": settings.foundry_iq_search_endpoint,
            "ASC_FOUNDRY_IQ_INDEX_NAME": settings.foundry_iq_index_name,
            "ASC_FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": settings.foundry_iq_knowledge_source_name,
            "ASC_FOUNDRY_IQ_KNOWLEDGE_BASE_NAME": settings.foundry_iq_knowledge_base_name,
            "ASC_FOUNDRY_IQ_CONNECTION_NAME": settings.foundry_iq_connection_name,
            "ASC_FOUNDRY_PROJECT_RESOURCE_ID": settings.foundry_project_resource_id,
            "ASC_FOUNDRY_PROJECT_ENDPOINT": settings.foundry_project_endpoint,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ConfigurationError(
                "Set required recipe knowledge settings: " + ", ".join(missing)
            )
        endpoint = settings.foundry_iq_search_endpoint
        index = settings.foundry_iq_index_name
        source = settings.foundry_iq_knowledge_source_name
        base = settings.foundry_iq_knowledge_base_name
        connection = settings.foundry_iq_connection_name
        resource_id = settings.foundry_project_resource_id
        assert endpoint and index and source and base and connection and resource_id
        project_endpoint = settings.foundry_project_endpoint
        assert project_endpoint
        parts = resource_id.split("/")
        if (
            project_endpoint.host != f"{parts[8]}.services.ai.azure.com"
            or (project_endpoint.path or "").rstrip("/") != f"/api/projects/{parts[10]}"
        ):
            raise ConfigurationError(
                "ASC_FOUNDRY_PROJECT_RESOURCE_ID must identify ASC_FOUNDRY_PROJECT_ENDPOINT."
            )
        return cls(
            str(endpoint).rstrip("/"),
            index,
            source,
            base,
            connection,
            resource_id,
            settings.foundry_iq_max_results,
            settings.foundry_iq_max_context_tokens,
        )

    @property
    def mcp_endpoint(self) -> str:
        return (
            f"{self.search_endpoint}/knowledgebases/{self.knowledge_base_name}/mcp"
            f"?api-version={SEARCH_API_VERSION}"
        )


class _IndexResult(BaseModel):
    model_config = ConfigDict(strict=True)
    key: str
    status: bool
    status_code: int = Field(alias="statusCode")


class _IndexResponse(BaseModel):
    value: list[_IndexResult]


class _SourceData(BaseModel):
    source_id: str
    title: str
    source_url: str
    recipe_json: str


class _Reference(BaseModel):
    type: Literal["searchIndex"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    doc_key: str = Field(alias="docKey")
    source_data: _SourceData = Field(alias="sourceData")


class _RetrievalResponse(BaseModel):
    references: list[_Reference]


@dataclass(frozen=True)
class RecipeCitation:
    reference_id: str
    source_url: str
    recipe: CorpusRecipe


class FoundryRecipeKnowledge:
    def __init__(
        self,
        config: RecipeKnowledgeConfig,
        credential: TokenCredential,
        *,
        transport: HttpTransport[HttpRequest, HttpResponse] | None = None,
    ) -> None:
        self.config = config
        self._search = PipelineClient[HttpRequest, HttpResponse](
            base_url=config.search_endpoint,
            policies=[
                BearerTokenCredentialPolicy(credential, SEARCH_SCOPE),
                RetryPolicy(retry_total=0),
                RedirectPolicy(permit_redirects=False),
            ],
            transport=transport,
        )
        self._management = PipelineClient[HttpRequest, HttpResponse](
            base_url="https://management.azure.com",
            policies=[
                BearerTokenCredentialPolicy(credential, ARM_SCOPE),
                RetryPolicy(retry_total=0),
                RedirectPolicy(permit_redirects=False),
            ],
            transport=transport,
        )

    def close(self) -> None:
        self._search.close()
        self._management.close()

    def _request(
        self,
        operation: str,
        method: str,
        path: str,
        body: dict[str, object],
        *,
        management: bool = False,
    ) -> object:
        client = self._management if management else self._search
        endpoint = "https://management.azure.com" if management else self.config.search_endpoint
        version = CONNECTION_API_VERSION if management else SEARCH_API_VERSION
        request = HttpRequest(
            method,
            f"{endpoint}{path}?api-version={version}",
            headers={"Prefer": "return=representation"},
            json=body,
        )
        try:
            response = client.send_request(request, connection_timeout=5, read_timeout=20)
        except AzureError as error:
            raise RecipeIndexingError(
                f"Foundry IQ {operation} failed: {type(error).__name__}; "
                "check identity and connectivity."
            ) from None
        with response:
            expected = (
                (200, 201)
                if method == "PUT"
                else (200, 207)
                if operation == "index_recipes"
                else (200,)
            )
            if response.status_code not in expected:
                raise RecipeIndexingError(
                    f"Foundry IQ {operation} failed (HTTP {response.status_code}); "
                    "check configured resources, identity/RBAC and service status."
                )
            try:
                payload: object = response.json()
            except ValueError:
                raise RecipeIndexingError(
                    f"Foundry IQ {operation} returned invalid JSON."
                ) from None
            return payload

    def configure(self) -> None:
        """Create/update only the four explicitly configured, dedicated resources."""
        config = self.config
        fields: list[dict[str, object]] = [
            {"name": "source_id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True},
            {"name": "content", "type": "Edm.String", "searchable": True},
            {"name": "grounding", "type": "Edm.String", "searchable": True},
            {"name": "source_url", "type": "Edm.String", "searchable": False},
            {"name": "recipe_json", "type": "Edm.String", "searchable": False},
        ]
        self._request(
            "configure_index",
            "PUT",
            f"/indexes/{config.index_name}",
            {
                "name": config.index_name,
                "fields": fields,
                "semantic": {
                    "defaultConfiguration": SEMANTIC_CONFIGURATION,
                    "configurations": [
                        {
                            "name": SEMANTIC_CONFIGURATION,
                            "prioritizedFields": {
                                "titleField": {"fieldName": "title"},
                                "prioritizedContentFields": [{"fieldName": "grounding"}],
                            },
                        }
                    ],
                },
            },
        )
        self._request(
            "configure_source",
            "PUT",
            f"/knowledgesources/{config.knowledge_source_name}",
            {
                "name": config.knowledge_source_name,
                "kind": "searchIndex",
                "searchIndexParameters": {
                    "searchIndexName": config.index_name,
                    "semanticConfigurationName": SEMANTIC_CONFIGURATION,
                    "searchFields": [{"name": "title"}, {"name": "content"}],
                    "sourceDataFields": [
                        {"name": name}
                        for name in ("source_id", "title", "source_url", "recipe_json")
                    ],
                },
            },
        )
        self._request(
            "configure_knowledge_base",
            "PUT",
            f"/knowledgebases/{config.knowledge_base_name}",
            {
                "name": config.knowledge_base_name,
                "knowledgeSources": [{"name": config.knowledge_source_name}],
                "models": [],
                "retrievalReasoningEffort": {"kind": "minimal"},
                "outputMode": "extractiveData",
                "retrieveDefaults": {
                    "maxRuntimeInSeconds": 10,
                    "maxOutputDocuments": config.max_results,
                    "maxOutputSizeInTokens": config.service_output_tokens,
                },
            },
        )
        self._request(
            "configure_connection",
            "PUT",
            f"{config.project_resource_id}/connections/{config.connection_name}",
            {
                "properties": {
                    "category": "RemoteTool",
                    "authType": "AgenticIdentityToken",
                    "target": config.mcp_endpoint,
                    "audience": "https://search.azure.com/",
                    "isSharedToAll": False,
                    "metadata": {"ApiType": "Azure"},
                }
            },
            management=True,
        )

    def index(self, documents: list[RecipeSearchDocument]) -> int:
        if not 1 <= len(documents) <= MAX_ROWS:
            raise RecipeIndexingError(f"Indexing requires between 1 and {MAX_ROWS} recipes.")
        if len({document["source_id"] for document in documents}) != len(documents):
            raise RecipeIndexingError("Indexing requires unique stable source IDs.")
        for offset in range(0, len(documents), BATCH_SIZE):
            batch = documents[offset : offset + BATCH_SIZE]
            payload = self._request(
                "index_recipes",
                "POST",
                f"/indexes/{self.config.index_name}/docs/index",
                {"value": [{"@search.action": "upload", **document} for document in batch]},
            )
            try:
                result = _IndexResponse.model_validate(payload)
            except ValidationError:
                raise RecipeIndexingError(
                    "Foundry IQ index_recipes returned invalid results."
                ) from None
            keys = {document["source_id"] for document in batch}
            if (
                len(result.value) != len(batch)
                or {item.key for item in result.value} != keys
                or any(
                    not item.status or item.status_code not in (200, 201) for item in result.value
                )
            ):
                raise RecipeIndexingError(
                    "Foundry IQ index_recipes did not confirm every recipe; "
                    "some writes may have succeeded. Re-running the same corpus is safe."
                )
        return len(documents)

    def retrieve(self, query: str) -> list[RecipeCitation]:
        if not query.strip() or len(query) > 2000:
            raise RecipeIndexingError("Retrieval query must contain 1 to 2000 characters.")
        payload = self._request(
            "retrieve_recipes",
            "POST",
            f"/knowledgebases/{self.config.knowledge_base_name}/retrieve",
            {
                "intents": [{"type": "semantic", "search": query}],
                "retrievalReasoningEffort": {"kind": "minimal"},
                "outputMode": "extractiveData",
                "maxRuntimeInSeconds": 10,
                "maxOutputDocuments": self.config.max_results,
                "maxOutputSizeInTokens": self.config.service_output_tokens,
                "knowledgeSourceParams": [
                    {
                        "kind": "searchIndex",
                        "knowledgeSourceName": self.config.knowledge_source_name,
                        "includeReferences": True,
                        "includeReferenceSourceData": True,
                        "alwaysQuerySource": True,
                        "failOnError": True,
                    }
                ],
            },
        )
        try:
            result = _RetrievalResponse.model_validate(payload)
            citations: list[RecipeCitation] = []
            for reference in result.references:
                data = reference.source_data
                recipe = CorpusRecipe.model_validate_json(data.recipe_json)
                document = search_document(recipe)
                if (
                    reference.doc_key != recipe.source_id
                    or data.source_id != recipe.source_id
                    or data.title != document["title"]
                    or data.source_url != document["source_url"]
                ):
                    raise RecipeIndexingError(
                        "Foundry IQ retrieval returned inconsistent citations."
                    )
                citations.append(RecipeCitation(reference.id, data.source_url, recipe))
        except ValidationError:
            raise RecipeIndexingError(
                "Foundry IQ retrieval omitted valid recipe content or citation metadata."
            ) from None
        if (
            len(citations) > self.config.max_results
            or len({item.reference_id for item in citations}) != len(citations)
            or len({item.recipe.source_id for item in citations}) != len(citations)
        ):
            raise RecipeIndexingError(
                "Foundry IQ retrieval returned invalid citation counts or IDs."
            )
        return citations
