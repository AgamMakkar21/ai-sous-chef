"""Comparable recipe amounts are assumptions, not measured inventory or model nutrients."""

import copy
import json
from datetime import UTC, datetime

import pytest
from sous_chef.domain.recipes import QuantityEstimate, RecipeDraft, RecipeGrounding, WebGrounding
from sous_chef.services.quantity_estimates import recipe_quantity_estimates
from sous_chef.services.web_grounding import quantity_web_query
from test_recipe_specialist import FIXTURE, cooking_context


def estimate(
    *claims: str, name: str = "chicken, boneless pieces", measured: bool = False
) -> dict[str, QuantityEstimate]:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft.update(
        servings=2,
        ingredients=[
            {
                "lineId": "chicken",
                "name": name,
                "quantity": 200 if measured else None,
                "unit": "g" if measured else None,
            }
        ],
        instructions=[{"step": 1, "text": "Cook chicken.", "ingredientLineIds": ["chicken"]}],
        substitutions=[],
        sourceClaims=[],
    )
    web = WebGrounding.model_validate(
        {
            "claims": tuple(
                {"text": text, "source_url": f"https://recipes.example.invalid/{i}"}
                for i, text in enumerate(claims)
            ),
            "citations": tuple(
                {"title": "Comparable recipe", "url": f"https://recipes.example.invalid/{i}"}
                for i in range(len(claims))
            ),
            "retrieved_at": datetime.now(UTC),
        }
    )
    return recipe_quantity_estimates(
        RecipeDraft.model_validate_json(json.dumps(draft)),
        RecipeGrounding.model_validate_json(json.dumps(FIXTURE["grounding"])),
        web,
    )


def test_scales_source_servings_and_preserves_distinct_evidence() -> None:
    result = estimate("Serves 4; 500 g chicken.", "Servings: 2; 300 g chicken breast.")
    inferred = result["chicken"]
    assert inferred.weight_grams == 275
    assert [source.weight_grams for source in inferred.sources] == [500, 300]
    assert [source.servings for source in inferred.sources] == [4, 2]


@pytest.mark.parametrize(
    "text",
    [
        "500 g chicken.",  # No serving denominator.
        "Serves 4; some chicken.",
        "Serves 4; 500 g cooked chicken.",
        "Serves 4; 500 g ground chicken.",
        "Serves 4; 500 g chicken spread.",
        "Serves 4; 500 g chicken and rice.",
        "Serves 4; 500 g chicken; 200 g chicken.",
        "Serves 4; 100000 g chicken.",
        "Serves 0; 500 g chicken.",
        "Serves 4-20; 500 g chicken.",
        "500 g chicken for 4 to 20 servings.",
        "Serves 4; 1,500 g chicken.",
        "Serves 4; 400 to 800 g chicken.",
        "Serves 4; 400-800 g chicken.",
        "Serves 4; 400\u2013800 g chicken.",
        "Serves 4; 1/2 lb chicken.",
    ],
)
def test_unsupported_or_incompatible_evidence_does_not_invent_weight(text: str) -> None:
    assert not estimate(text)


def test_conflicting_comparable_recipes_require_quantity_instead_of_guessing() -> None:
    assert not estimate("Serves 4; 200 g chicken.", "Serves 4; 600 g chicken.")


def test_supported_measured_amount_is_never_replaced_by_a_comparable_recipe() -> None:
    assert not estimate("Serves 4; 500 g chicken.", measured=True)


def test_equivalent_mass_units_are_converted_before_serving_scaling() -> None:
    assert estimate("Serves 4; 0.5 kg chicken.")["chicken"].weight_grams == 250


@pytest.mark.parametrize(
    ("name", "grams", "accepted"),
    [
        ("salt", 40, True),
        ("salt", 41, False),
        ("olive oil", 240, True),
        ("olive oil", 241, False),
    ],
)
def test_rejects_extreme_salt_and_oil_quantities_not_consumed_recipe_amounts(
    name: str,
    grams: int,
    accepted: bool,
) -> None:
    assert bool(estimate(f"Serves 4; {grams} g {name}.", name=name)) is accepted


def test_quantity_search_contains_public_food_names_not_private_context() -> None:
    data = cooking_context().model_copy(
        update={
            "current_request": "Use some chicken and rice. My email is private@example.invalid.",
        }
    )
    query = quantity_web_query(data)
    assert query is not None
    assert "chicken" in query and "rice" in query and "servings" in query
    assert "private" not in query and "email" not in query and "peanut" not in query
    assert len(query) <= 240
    assert quantity_web_query(cooking_context()) is None
