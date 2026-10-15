"""Bounded ASC-28 corpus loading and lossless search-document mapping."""

import json
import re
from pathlib import Path
from typing import TypedDict

from pydantic import ValidationError

from sous_chef.domain.recipe_corpus import (
    DATASET_ID,
    SOURCE_FILE,
    CorpusRecipe,
    content_key,
)
from sous_chef.services.recipe_ingestion import MAX_ROWS

MAX_CORPUS_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 256 * 1024


class RecipeIndexingError(RuntimeError):
    """An indexing or retrieval failure with no raw input or service response."""


class RecipeSearchDocument(TypedDict):
    source_id: str
    title: str
    content: str
    source_url: str
    recipe_json: str


def source_reference(recipe: CorpusRecipe) -> str:
    provenance = recipe.provenance
    if (
        provenance.dataset != DATASET_ID
        or provenance.source_file != SOURCE_FILE
        or re.fullmatch(r"[0-9a-f]{40}", provenance.revision) is None
        or re.fullmatch(r"[0-9]{1,20}", provenance.source_recipe_id) is None
        or recipe.source_id != f"hf-food-recipes-{int(provenance.source_recipe_id)}"
        or provenance.row_number < 1
    ):
        raise RecipeIndexingError(
            "Corpus provenance is invalid; regenerate it with ingest_recipes."
        )
    return f"https://huggingface.co/datasets/{DATASET_ID}/blob/{provenance.revision}/{SOURCE_FILE}"


def search_document(recipe: CorpusRecipe) -> RecipeSearchDocument:
    ingredients = [
        " ".join(part for part in (ingredient.quantity, ingredient.name) if part)
        for ingredient in recipe.ingredients
    ]
    if (
        not recipe.name.strip()
        or not ingredients
        or any(not ingredient.name.strip() for ingredient in recipe.ingredients)
        or not recipe.instructions
        or any(not step.strip() for step in recipe.instructions)
    ):
        raise RecipeIndexingError("Corpus recipe fields are empty; regenerate with ingest_recipes.")
    document: RecipeSearchDocument = {
        "source_id": recipe.source_id,
        "title": recipe.name,
        "content": "\n".join(
            [
                recipe.name,
                *recipe.keywords,
                "Ingredients:",
                *ingredients,
                "Steps:",
                *recipe.instructions,
            ]
        ),
        "source_url": source_reference(recipe),
        "recipe_json": recipe.model_dump_json(),
    }
    if len(json.dumps(document).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise RecipeIndexingError("An indexed recipe exceeds the 256 KiB document limit.")
    return document


def load_corpus(path: Path) -> list[RecipeSearchDocument]:
    try:
        with path.open("rb") as source:
            data = source.read(MAX_CORPUS_BYTES + 1)
    except OSError:
        raise RecipeIndexingError(
            "Cannot read the corpus; check --corpus and file permissions."
        ) from None
    if len(data) > MAX_CORPUS_BYTES:
        raise RecipeIndexingError("Corpus exceeds 16 MiB; use a bounded ingest_recipes slice.")
    lines = data.splitlines()
    if not 1 <= len(lines) <= MAX_ROWS:
        raise RecipeIndexingError(f"Corpus must contain between 1 and {MAX_ROWS} recipe lines.")
    documents: list[RecipeSearchDocument] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    for number, line in enumerate(lines, start=1):
        if len(line) > MAX_DOCUMENT_BYTES:
            raise RecipeIndexingError(f"Corpus line {number} exceeds 256 KiB.")
        try:
            recipe = CorpusRecipe.model_validate_json(line)
        except ValidationError:
            raise RecipeIndexingError(
                f"Corpus line {number} is invalid; regenerate with ingest_recipes."
            ) from None
        key = content_key(recipe)
        if recipe.source_id in seen_ids or key in seen_content:
            raise RecipeIndexingError(
                f"Corpus line {number} repeats a source ID or recipe content."
            )
        documents.append(search_document(recipe))
        seen_ids.add(recipe.source_id)
        seen_content.add(key)
    return documents
