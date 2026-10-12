"""Normalized recipe corpus records and the rules that build them from raw source rows."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

DATASET_ID = "untitledwebsite123/food-recipes"
SOURCE_FILE = "recipes.csv"
PUBLISHER = "Food.com"
# The dataset card declares no license; keep that visible instead of assuming one.
LICENSE_UNSPECIFIED = "unspecified"


class RejectionReason(StrEnum):
    INVALID_RECIPE_ID = "invalid_recipe_id"
    MISSING_NAME = "missing_name"
    MISSING_INGREDIENTS = "missing_ingredients"
    MISSING_INSTRUCTIONS = "missing_instructions"
    MALFORMED_INGREDIENTS = "malformed_ingredients"
    DUPLICATE_SOURCE_ID = "duplicate_source_id"
    DUPLICATE_CONTENT = "duplicate_content"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Ingredient(_Frozen):
    quantity: str
    name: str


class Provenance(_Frozen):
    dataset: str
    revision: str
    source_file: str
    row_number: int
    source_recipe_id: str
    publisher: str
    author_name: str | None
    license: str
    attribution: str


class CorpusRecipe(_Frozen):
    source_id: str
    name: str
    category: str | None
    keywords: list[str]
    prep_time: str | None
    cook_time: str | None
    total_time: str | None
    servings: int | None
    ingredients: list[Ingredient]
    # False when the source quantities could not be paired with ingredient names.
    quantities_aligned: bool
    instructions: list[str]
    provenance: Provenance


class Rejection(_Frozen):
    row_number: int
    source_recipe_id: str | None
    reason: RejectionReason


_R_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|\bNA\b')
_ESCAPE = re.compile(r"\\(.)")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def parse_r_vector(value: str) -> list[str] | None:
    """Parse the dataset's R-style `c("a", "b")` cells; None means the cell is malformed."""
    text = value.strip()
    if text in ("", "NA"):
        return []
    if not (text.startswith("c(") and text.endswith(")")):
        return [_collapse(text)]
    inner = text[2:-1]
    if _R_TOKEN.sub("", inner).replace(",", "").strip():
        return None
    return [
        _collapse(_ESCAPE.sub(r"\1", match.group(1) or "")) for match in _R_TOKEN.finditer(inner)
    ]


def _optional(value: str) -> str | None:
    text = _collapse(value)
    return None if text in ("", "NA") else text


def _servings(value: str) -> int | None:
    text = value.strip()
    return int(text) if text.isdigit() else None


def content_key(recipe: CorpusRecipe) -> str:
    """Identify a recipe by what it says, ignoring its source row, to catch repeats."""
    parts = [
        recipe.name.casefold(),
        *(i.name.casefold() for i in recipe.ingredients),
        *(s.casefold() for s in recipe.instructions),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def normalize_row(row: dict[str, str], row_number: int, revision: str) -> CorpusRecipe | Rejection:
    raw_id = row.get("RecipeId", "").strip()
    if not raw_id.isdigit():
        return Rejection(
            row_number=row_number, source_recipe_id=None, reason=RejectionReason.INVALID_RECIPE_ID
        )

    def reject(reason: RejectionReason) -> Rejection:
        return Rejection(row_number=row_number, source_recipe_id=raw_id, reason=reason)

    name = _collapse(row.get("Name", ""))
    if not name:
        return reject(RejectionReason.MISSING_NAME)

    parts = parse_r_vector(row.get("RecipeIngredientParts", ""))
    if parts is None:
        return reject(RejectionReason.MALFORMED_INGREDIENTS)
    if not parts:
        return reject(RejectionReason.MISSING_INGREDIENTS)
    # The source often lists a different number of quantities than ingredient names, so the
    # two cannot be paired. Keep the names and drop the quantities rather than guess.
    quantities = parse_r_vector(row.get("RecipeIngredientQuantities", ""))
    quantities_aligned = quantities is not None and len(quantities) == len(parts)
    paired = quantities if quantities is not None and quantities_aligned else [""] * len(parts)
    ingredients = [
        Ingredient(quantity=quantity, name=part)
        for quantity, part in zip(paired, parts, strict=True)
        if part
    ]
    if not ingredients:
        return reject(RejectionReason.MISSING_INGREDIENTS)

    steps = parse_r_vector(row.get("RecipeInstructions", ""))
    instructions = [step for step in steps or [] if step]
    if not instructions:
        return reject(RejectionReason.MISSING_INSTRUCTIONS)

    author = _optional(row.get("AuthorName", ""))
    attribution = f"Recipe from {PUBLISHER}" + (f" by {author}" if author else "")
    attribution += f", via Hugging Face dataset {DATASET_ID}"
    return CorpusRecipe(
        source_id=f"hf-food-recipes-{int(raw_id)}",
        name=name,
        category=_optional(row.get("RecipeCategory", "")),
        keywords=[k for k in parse_r_vector(row.get("Keywords", "")) or [] if k],
        prep_time=_optional(row.get("PrepTime", "")),
        cook_time=_optional(row.get("CookTime", "")),
        total_time=_optional(row.get("TotalTime", "")),
        servings=_servings(row.get("RecipeServings", "")),
        ingredients=ingredients,
        quantities_aligned=quantities_aligned,
        instructions=instructions,
        provenance=Provenance(
            dataset=DATASET_ID,
            revision=revision,
            source_file=SOURCE_FILE,
            row_number=row_number,
            source_recipe_id=raw_id,
            publisher=PUBLISHER,
            author_name=author,
            license=LICENSE_UNSPECIFIED,
            attribution=attribution,
        ),
    )
