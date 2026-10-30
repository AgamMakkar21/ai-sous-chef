import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sous_chef.domain.food_safety import SafetyProfile
from sous_chef.domain.orchestrator import (
    CandidateIngredient,
    CandidateInstruction,
    RecipeCandidate,
    SpecialistResult,
)
from sous_chef.domain.recipe_safety import (
    UNABLE,
    RecipeSafetyError,
    render_recipe,
    validate_final_recipe,
)
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services.orchestrator import ExecutionBudget, OrchestratorService
from test_orchestrator import USAGE, Coordinator, Specialist, answer, call, context

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "recipe-specialist.json").read_text(encoding="utf-8")
)


def full_recipe(
    names: tuple[str, ...] = ("rice",),
    instructions: tuple[str, ...] = ("Cook the rice.",),
    references: tuple[tuple[str, ...], ...] | None = None,
) -> StructuredRecipe:
    data: dict[str, Any] = {
        **FIXTURE["draft"],
        "title": "Simple dinner",
        "fitReasons": ["Fits the time budget."],
        "adaptations": [],
        "substitutions": [],
        "ingredients": [
            {
                "lineId": f"line{index}",
                "name": name,
                "quantity": 100,
                "unit": "g",
                "ingredientId": None,
                "inventoryStatus": "unknown",
                "groceryIds": [],
            }
            for index, name in enumerate(names)
        ],
        "instructions": [
            {
                "step": index + 1,
                "text": text,
                "ingredientLineIds": list(references[index])
                if references is not None
                else [f"line{line}" for line in range(len(names))],
            }
            for index, text in enumerate(instructions)
        ],
        "shoppingItems": [],
        "sources": [FIXTURE["grounding"]["recipes"][0]["source"]],
        "webGrounding": None,
        "nutrition": {
            "status": "unavailable",
            "servingBasis": "per_serving",
            "servings": 2,
            "nutrients": [],
            "mappings": [],
            "sources": [],
            "notice": "Informational estimate, not medical advice.",
        },
    }
    return StructuredRecipe.model_validate_json(json.dumps(data))


def projection(recipe: StructuredRecipe) -> RecipeCandidate:
    return RecipeCandidate(
        title=recipe.title,
        total_minutes=recipe.total_minutes,
        ingredients=tuple(
            CandidateIngredient(line_id=line.line_id, name=line.name) for line in recipe.ingredients
        ),
        instructions=tuple(
            CandidateInstruction(text=step.text, ingredient_line_ids=step.ingredient_line_ids)
            for step in recipe.instructions
        ),
        fit_reasons=recipe.fit_reasons,
    )


def test_compliant_recipe_is_rendered_without_untrusted_evidence_or_commentary() -> None:
    recipe = full_recipe()
    checked = validate_final_recipe(recipe, SafetyProfile())
    assert checked == recipe and checked is not recipe
    content = render_recipe(checked)
    assert "Cook the rice." in content
    assert "2 hours (1 hour above 90 F)" in content
    assert "Beginner preparation" not in content  # Source excerpt is not a cooking direction.


@pytest.mark.parametrize(
    "text",
    [
        "Cook the rice, then garnish with peanut.",
        "Cook the rice, then garnish with mystery seasoning.",
        "Cook the rice with almond milk.",
        "Cook the rice, then add peanut oil.",
        "Cook the rice. Serve raw chicken.",
        "Leave the rice on the counter overnight.",
        "Cook the rice for 999 minutes.",
        "Cook the rice without cooking.",
        "Cook the rice\u30d4\u30fc\u30ca\u30c3\u30c4.",
    ],
)
def test_undeclared_garnishes_ambiguous_prose_and_unsafe_handling_fail_closed(text: str) -> None:
    with pytest.raises(RecipeSafetyError) as caught:
        validate_final_recipe(
            full_recipe(instructions=(text,)), SafetyProfile(allergies=("peanut",))
        )
    assert text not in str(caught.value)
    assert "peanut" not in str(caught.value)


@pytest.mark.parametrize("name", ["mystery seasoning", "rice and peanut", "rice\u9db4"])
def test_unknown_ingredients_fail_even_without_stored_restrictions(name: str) -> None:
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(full_recipe(names=(name,)), SafetyProfile())


def test_safe_replacement_does_not_consume_the_excluded_original_allergen() -> None:
    recipe = full_recipe(names=("rice", "olive oil"), instructions=("Cook the rice in olive oil.",))
    data = recipe.model_dump(mode="json", by_alias=True)
    data["substitutions"] = [
        {
            "replaces": "butter",
            "ingredientLineId": "line1",
            "reason": "Use olive oil instead of butter to respect the dairy restriction.",
        }
    ]
    recipe = StructuredRecipe.model_validate_json(json.dumps(data))
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("milk",))) == recipe
    data["substitutions"][0]["reason"] = "Use olive oil, then garnish with butter."
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            StructuredRecipe.model_validate_json(json.dumps(data)),
            SafetyProfile(allergies=("milk",)),
        )


def test_replacement_ingredient_itself_is_subject_to_current_allergies() -> None:
    recipe = full_recipe(
        names=("rice", "peanut oil"), instructions=("Cook the rice in peanut oil.",)
    )
    data = recipe.model_dump(mode="json", by_alias=True)
    data["substitutions"] = [
        {
            "replaces": "butter",
            "ingredientLineId": "line1",
            "reason": "Use peanut oil instead of butter.",
        }
    ]
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            StructuredRecipe.model_validate_json(json.dumps(data)),
            SafetyProfile(allergies=("peanut",)),
        )


@pytest.mark.parametrize("field", ["title", "fit_reasons", "adaptations"])
def test_ingredient_bearing_metadata_cannot_bypass_the_gate(field: str) -> None:
    value: Any = "Garnish with peanut"
    if field != "title":
        value = (value,)
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            full_recipe().model_copy(update={field: value}), SafetyProfile(allergies=("peanut",))
        )


@pytest.mark.parametrize(
    "name,text,accepted",
    [
        ("chicken", "Cook the chicken.", False),
        (
            "chicken",
            "Cook the chicken to an internal temperature of 145 F, "
            "measured with a food thermometer.",
            False,
        ),
        (
            "chicken",
            "Cook the chicken to an internal temperature of 165 F, "
            "measured with a food thermometer.",
            True,
        ),
        (
            "ground beef",
            "Cook the ground beef to an internal temperature of 160 F, "
            "measured with a food thermometer.",
            True,
        ),
        (
            "minced beef",
            "Cook the minced beef to an internal temperature of 145 F, "
            "measured with a food thermometer, then rest for 3 minutes.",
            False,
        ),
        (
            "minced beef",
            "Cook the minced beef to an internal temperature of 160 F, "
            "measured with a food thermometer.",
            True,
        ),
        (
            "beef",
            "Cook the beef to an internal temperature of 145 F, measured with a food thermometer.",
            False,
        ),
        (
            "beef",
            "Cook the beef to an internal temperature of 145 F, "
            "measured with a food thermometer, then rest for 3 minutes.",
            True,
        ),
        (
            "salmon",
            "Cook the salmon to an internal temperature of 145 F, "
            "measured with a food thermometer.",
            True,
        ),
        (
            "minced salmon",
            "Cook the salmon to an internal temperature of 145 F, "
            "measured with a food thermometer.",
            False,
        ),
        ("chicken", "Rinse the chicken.", False),
        ("chicken", "Serve the chicken.", False),
        ("egg", "Serve the egg.", False),
        ("flour", "Serve the flour.", False),
        ("cornmeal", "Serve the cornmeal.", False),
        ("kidney bean", "Cook the kidney bean.", False),
        ("canned kidney bean", "Cook the kidney bean.", True),
        ("cooked rice", "Heat the rice.", False),
        (
            "cooked rice",
            "Cook the rice to an internal temperature of 165 F, measured with a food thermometer.",
            True,
        ),
    ],
)
def test_reviewed_cooking_rules(name: str, text: str, accepted: bool) -> None:
    recipe = full_recipe(names=(name,), instructions=(text,))
    if accepted:
        assert validate_final_recipe(recipe, SafetyProfile()) == recipe
    else:
        with pytest.raises(RecipeSafetyError):
            validate_final_recipe(recipe, SafetyProfile())


@pytest.mark.parametrize("name", ["canned beef", "dried beef", "cold beef", "toasted beef"])
def test_unsupported_animal_preparation_cannot_inherit_whole_cut_rules(name: str) -> None:
    recipe = full_recipe(
        names=(name,),
        instructions=(
            "Cook the beef to an internal temperature of 145 F, "
            "measured with a food thermometer, then rest for 3 minutes.",
        ),
    )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(recipe, SafetyProfile())


def test_coarse_catalog_ids_do_not_authorize_an_undeclared_food() -> None:
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            full_recipe(names=("carrot",), instructions=("Cook the onion.",)), SafetyProfile()
        )


def test_incorrect_instruction_references_and_model_copy_bypasses_are_revalidated() -> None:
    recipe = full_recipe()
    forged = recipe.model_copy(update={"servings": 0})
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(forged, SafetyProfile())
    recipe = full_recipe(names=("rice", "carrot"), instructions=("Cook the rice.",))
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(recipe, SafetyProfile())


def test_edits_and_substitutions_cannot_reuse_an_old_pass() -> None:
    recipe = validate_final_recipe(full_recipe(), SafetyProfile(allergies=("peanut",)))
    edit = recipe.model_copy(
        update={
            "instructions": (
                recipe.instructions[0].model_copy(
                    update={"text": "Cook the rice, then garnish with peanut."}
                ),
            )
        }
    )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(edit, SafetyProfile(allergies=("peanut",)))


def test_temperature_check_does_not_authorize_later_raw_serving_or_cross_contamination() -> None:
    hot = "Cook the chicken to an internal temperature of 165 F, measured with a food thermometer."
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            full_recipe(names=("raw chicken",), instructions=(hot, "Serve the raw chicken.")),
            SafetyProfile(),
        )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            full_recipe(
                names=("chicken", "lettuce"),
                instructions=("Chop the chicken with lettuce.", hot),
                references=(("line0", "line1"), ("line0",)),
            ),
            SafetyProfile(),
        )


class FullSpecialist(Specialist):
    def __init__(self, *recipes: StructuredRecipe) -> None:
        self.recipes = list(recipes)
        super().__init__(*(projection(recipe) for recipe in recipes))

    async def generate(self, *args: Any, **kwargs: Any) -> SpecialistResult:
        result = await super().generate(*args, **kwargs)
        return result.model_copy(update={"recipe": self.recipes.pop(0)})


def test_one_final_gate_regeneration_shares_the_existing_four_call_budget() -> None:
    bad = full_recipe(instructions=("Cook the rice, then garnish with peanut.",))
    specialist = FullSpecialist(bad, full_recipe())
    router = Coordinator(call(), call(call_id="revision"))
    result = asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert result.status == "candidate" and result.recipe == full_recipe()
    assert len(specialist.calls) == 2
    assert specialist.budgets[0] is specialist.budgets[1]
    budget = specialist.budgets[0]
    assert isinstance(budget, ExecutionBudget) and budget.model_calls == 4
    correction = router.requests[1].tool_output or ""
    assert "safety_rejected" in correction and "peanut" not in correction


@pytest.mark.parametrize("accept_rejected", [False, True])
def test_rejected_recipe_never_returns_as_success_and_regeneration_terminates(
    accept_rejected: bool,
) -> None:
    bad = full_recipe(instructions=("Cook the rice, then garnish with peanut.",))
    specialist = FullSpecialist(bad, bad, full_recipe())
    router = Coordinator(
        call(),
        answer(explanation="Unsafe draft text") if accept_rejected else call(call_id="revision"),
    )
    result = asyncio.run(
        OrchestratorService(router, specialist).run(context(), operation_id=UUID(int=1))
    )
    assert result.status == "unable" and result.explanation == UNABLE
    assert result.recipe is None and result.candidate is None
    assert len(specialist.calls) == (1 if accept_rejected else 2)


def test_nested_accounted_work_cannot_give_regeneration_extra_model_calls() -> None:
    class NestedSpecialist(FullSpecialist):
        async def generate(self, *args: Any, **kwargs: Any) -> SpecialistResult:
            result = await super().generate(*args, **kwargs)
            budget = kwargs["budget"]
            budget.model()
            budget.usage(USAGE)
            return result

    bad = full_recipe(instructions=("Cook the rice, then garnish with peanut.",))
    specialist = NestedSpecialist(bad)
    router = Coordinator(call(), call(call_id="revision"))
    result = asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert result.status == "unable" and result.explanation == UNABLE
    assert len(specialist.calls) == 1
    budget = specialist.budgets[0]
    assert isinstance(budget, ExecutionBudget) and budget.model_calls == 4
