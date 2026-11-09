"""Backend-shaped queries and bounded, untrusted evidence for the MVP recipe index."""

import json
import re
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.orchestrator import SpecialistArguments
from sous_chef.domain.recipe_corpus import DATASET_ID, SOURCE_FILE, CorpusRecipe
from sous_chef.domain.recipes import (
    CuratedRecipe,
    RecipeGrounding,
    RecipeToolError,
)
from sous_chef.services.context import _encoding
from sous_chef.services.recipe_indexing import RecipeIndexingError, search_document

MCP_MAX_OUTPUT_CHARACTERS = 16000
DEV_MCP_MAX_OUTPUT_CHARACTERS = 65_536
DEV_MCP_MAX_CONTEXT_TOKENS = 16000


def retrieval_query(context: RequestContext, arguments: SpecialistArguments) -> str:
    """Never include user IDs, conversation history or learned profile evidence."""
    preferences = context.explicit_preferences
    data = {
        "requirements": arguments.requirements,
        "timeBudgetMinutes": arguments.time_budget_minutes,
        "allergies": preferences.allergies,
        "dietaryRestrictions": preferences.dietary_restrictions,
        "cookingSkill": preferences.cooking_skill,
        "groceries": [row.name for row in context.groceries],
    }
    query = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(query) > 2000:
        # Do not silently drop mandatory restrictions to satisfy a query limit.
        raise RecipeToolError(FoundryFailure.REJECTED)
    return query


def recipe_knowledge_arguments(query: str) -> dict[str, list[str]]:
    """Search MCP receives exactly one unchanged backend query, not model variants."""
    return {"query_variants": [query]}


GroundingStage = Literal[
    "output_size",
    "output_json",
    "mcp_envelope",
    "document_schema",
    "document_source",
    "mcp_tool_error",
    "chunk_json",
    "chunk_list",
    "chunk_count",
    "chunk_schema",
    "duplicate_reference",
    "recipe_schema",
    "provenance",
    "source_set",
    "reference_count",
    "reference_schema",
    "reference_match",
    "context_tokens",
]


class McpGroundingError(RecipeToolError):
    def __init__(
        self, stage: GroundingStage, *, code: FoundryFailure = FoundryFailure.INVALID_RESULT
    ) -> None:
        self.stage = stage
        super().__init__(code)


class _ReferenceId(BaseModel):
    model_config = ConfigDict(strict=True)
    ref_id: str = Field(min_length=1, max_length=128)

    @field_validator("ref_id", mode="before")
    @classmethod
    def integer_reference(cls, value: object) -> object:
        if type(value) is int and 0 <= value < 10**128:
            return str(value)
        return value


class _Chunk(_ReferenceId):
    grounding: str = Field(validation_alias=AliasChoices("grounding", "content"))
    title: str | None = None

    @model_validator(mode="before")
    @classmethod
    def consistent_content(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and "grounding" in value
            and "content" in value
            and value["grounding"] != value["content"]
        ):
            raise ValueError
        return value


class _ReferenceData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    grounding: str
    source_id: str | None = None
    title: str | None = None
    source_url: str | None = None
    recipe_json: str | None = None


class _McpReference(_ReferenceId):
    source_data: _ReferenceData = Field(alias="sourceData")
    doc_key: str | None = Field(default=None, validation_alias=AliasChoices("docKey", "doc_key"))

    @model_validator(mode="before")
    @classmethod
    def consistent_document_key(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and "docKey" in value
            and "doc_key" in value
            and value["docKey"] != value["doc_key"]
        ):
            raise ValueError
        return value


class _TextContent(BaseModel):
    model_config = ConfigDict(strict=True)
    type: str
    text: str


class _McpResult(BaseModel):
    model_config = ConfigDict(strict=True)
    content: list[_TextContent] = Field(min_length=1, max_length=4)
    isError: bool = False


class _McpDocument(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=128)
    content: str
    url: str


class _DocumentResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    documents: list[_McpDocument] = Field(max_length=3)


def parse_mcp_grounding(
    output: str,
    *,
    max_results: int,
    max_context_tokens: int,
    max_output_characters: int = MCP_MAX_OUTPUT_CHARACTERS,
) -> RecipeGrounding:
    """Validate the overview and every attached reference within the full-response budget."""
    if len(output) > max_output_characters:
        raise McpGroundingError("output_size")
    stage: GroundingStage = "output_json"
    try:
        chunks = json.loads(output)
        envelope: _McpResult | None = None
        if isinstance(chunks, dict):
            if "documents" in chunks:
                stage = "document_schema"
                documents = _DocumentResult.model_validate(chunks).documents
                overview = []
                attachments = []
                for entry in documents:
                    data = _ReferenceData.model_validate_json(entry.content)
                    stage = "document_source"
                    if any(
                        value is None
                        for value in (
                            data.source_id,
                            data.title,
                            data.source_url,
                            data.recipe_json,
                        )
                    ):
                        raise ValueError
                    overview.append(
                        {"ref_id": entry.id, "title": data.title, "grounding": data.grounding}
                    )
                    attachments.append(
                        _TextContent(
                            type="text",
                            text=_McpReference(
                                ref_id=entry.id, sourceData=data, doc_key=data.source_id
                            ).model_dump_json(by_alias=True),
                        )
                    )
                    stage = "document_schema"
                envelope = _McpResult(
                    content=[_TextContent(type="text", text=json.dumps(overview)), *attachments]
                )
                chunks = envelope.model_dump()
            stage = "mcp_envelope"
            envelope = _McpResult.model_validate(chunks.get("result", chunks))
            if envelope.isError:
                raise McpGroundingError("mcp_tool_error", code=FoundryFailure.UNAVAILABLE)
            if any(item.type != "text" for item in envelope.content):
                raise ValueError
            stage = "chunk_json"
            chunks = json.loads(envelope.content[0].text)
        stage = "chunk_list"
        if not isinstance(chunks, list):
            raise ValueError
        stage = "chunk_count"
        if len(chunks) > max_results:
            raise ValueError
        references: set[str] = set()
        recipes: list[CuratedRecipe] = []
        chunks_by_id: dict[str, _Chunk] = {}
        recipes_by_id: dict[str, CuratedRecipe] = {}
        for value in chunks:
            stage = "chunk_schema"
            chunk = _Chunk.model_validate(value)
            stage = "duplicate_reference"
            if chunk.ref_id in references:
                raise ValueError
            references.add(chunk.ref_id)
            chunks_by_id[chunk.ref_id] = chunk
            stage = "recipe_schema"
            recipe = CuratedRecipe.model_validate_json(chunk.grounding)
            source = recipe.source
            expected = (
                f"https://huggingface.co/datasets/{DATASET_ID}/blob/{source.version}/{SOURCE_FILE}"
            )
            stage = "provenance"
            if (
                re.fullmatch(r"hf-food-recipes-[0-9]{1,20}", source.source_id) is None
                or re.fullmatch(r"[0-9a-f]{40}", source.version) is None
                or source.reference != expected
            ):
                raise ValueError
            recipes.append(recipe)
            recipes_by_id[chunk.ref_id] = recipe
        if envelope is not None and len(envelope.content) > 1:
            stage = "reference_count"
            if len(envelope.content) != len(recipes) + 1:
                raise ValueError
            matched: set[str] = set()
            for item in envelope.content[1:]:
                stage = "reference_schema"
                reference = _McpReference.model_validate_json(item.text)
                data = reference.source_data
                reference_recipe = CuratedRecipe.model_validate_json(data.grounding)
                stage = "reference_match"
                if reference.ref_id not in recipes_by_id or reference.ref_id in matched:
                    raise ValueError
                expected_recipe = recipes_by_id[reference.ref_id]
                if (
                    reference_recipe != expected_recipe
                    or data.source_id is not None
                    and data.source_id != expected_recipe.source.source_id
                    or data.source_url is not None
                    and data.source_url != expected_recipe.source.reference
                    or reference.doc_key is not None
                    and reference.doc_key != expected_recipe.source.source_id
                    or data.title is not None
                    and chunks_by_id[reference.ref_id].title is not None
                    and data.title != chunks_by_id[reference.ref_id].title
                ):
                    raise ValueError
                if data.recipe_json is not None:
                    stage = "reference_schema"
                    full_recipe = CorpusRecipe.model_validate_json(data.recipe_json)
                    stage = "reference_match"
                    document = search_document(full_recipe)
                    overview_title = chunks_by_id[reference.ref_id].title
                    if (
                        CuratedRecipe.model_validate_json(document["grounding"]) != expected_recipe
                        or data.title is not None
                        and data.title != document["title"]
                        or overview_title is not None
                        and overview_title != document["title"]
                    ):
                        raise ValueError
                matched.add(reference.ref_id)
        stage = "source_set"
        grounding = RecipeGrounding(
            status="available" if recipes else "empty", recipes=tuple(recipes)
        )
        stage = "context_tokens"
        tokens = _encoding().encode(output, disallowed_special=())
        if len(tokens) > max_context_tokens:
            raise ValueError
        return grounding
    except (ValueError, ValidationError, RecursionError, RecipeIndexingError):
        raise McpGroundingError(stage) from None
