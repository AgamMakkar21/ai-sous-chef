import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.food_safety import SafetyProfile
from sous_chef.domain.foundry import FoundryRequest
from sous_chef.domain.orchestrator import (
    CandidateIngredient,
    CandidateInstruction,
    RecipeAllergyError,
    RecipeCandidate,
    SpecialistResult,
)
from sous_chef.domain.recipe_safety import (
    UNABLE,
    RecipeSafetyError,
    RecipeSafetyFailure,
    render_recipe,
    validate_final_recipe,
)
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services.orchestrator import ExecutionBudget, OrchestratorService
from sous_chef.telemetry import configure_safe_telemetry
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


@pytest.mark.parametrize("long_recipe", [False, True])
def test_fit_notes_survive_final_validation_and_bounded_transcript_rendering(
    long_recipe: bool,
) -> None:
    instructions = tuple(("Cook the rice carefully. " * 20).strip() for _ in range(9))
    if not long_recipe:
        instructions = ("Cook the rice.",)
    recipe = full_recipe(instructions=instructions).model_copy(
        update={
            "fit_notes": (
                "This recipe may take longer than your time budget.",
                "This recipe may be above your cooking skill level.",
            )
        }
    )
    checked = validate_final_recipe(recipe, SafetyProfile())
    content = render_recipe(checked)
    assert len(content) <= 4000
    assert ("recipe card" in content) is long_recipe
    assert "Recipe notes" in content
    assert all(note in content for note in checked.fit_notes)
    assert "Nutrition [Unavailable]" in content
    assert tuple(item.text for item in checked.instructions) == instructions


@pytest.mark.parametrize(
    "text",
    [
        "Cook the rice, then garnish with peanut.",
        "Cook the rice with almond milk.",
        "Cook the rice, then add peanut oil.",
        "Cook the rice\u30d4\u30fc\u30ca\u30c3\u30c4.",
    ],
    ids=[
        "hidden-peanut-garnish",
        "hidden-almond-milk",
        "hidden-peanut-oil",
        "non-ascii-food",
    ],
)
def test_allergy_validation_only_checks_declared_ingredients_not_prose(text: str) -> None:
    recipe = full_recipe(instructions=(text,))
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut", "tree nut"))) == recipe


@pytest.mark.parametrize(
    "name", ["mystery seasoning", "rice\u9db4", "chicken breast (mystery seasoning)", "pesto"]
)
def test_unknown_or_ambiguous_names_do_not_block_without_a_listed_allergen(name: str) -> None:
    recipe = full_recipe(names=(name,))
    assert validate_final_recipe(recipe, SafetyProfile()) == recipe
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


@pytest.mark.parametrize("name", ["rice and peanut", "PEANUTS", "peanut oil", "groundnut oil"])
def test_listed_allergens_and_recognized_aliases_remain_blocked(name: str) -> None:
    with pytest.raises(RecipeSafetyError) as caught:
        validate_final_recipe(full_recipe(names=(name,)), SafetyProfile(allergies=("peanut",)))
    assert caught.value.code == RecipeSafetyFailure.INGREDIENT
    assert name not in str(caught.value)


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
    updated = StructuredRecipe.model_validate_json(json.dumps(data))
    assert validate_final_recipe(updated, SafetyProfile(allergies=("milk",))) == updated


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
def test_metadata_mentions_do_not_create_ingredient_list_allergy_conflicts(field: str) -> None:
    value: Any = "No peanuts used."
    if field != "title":
        value = (value,)
    recipe = full_recipe().model_copy(update={field: value})
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


@pytest.mark.parametrize(
    "name,text",
    [
        ("chicken breast", "Sear the chicken breast in a hot skillet until fully cooked."),
        ("egg", "Whisk the egg, then cook in a skillet until set."),
        ("milk", "Warm the milk gently in a saucepan."),
        ("shrimp", "Cook the shrimp until opaque, then serve."),
        ("flour", "Toast the flour in a dry pan."),
        ("lentil", "Simmer the lentil until tender, then drain."),
        ("cooked rice", "Reheat the cooked rice thoroughly, then serve."),
        ("frozen chicken breast", "Thaw the frozen chicken breast in the fridge before cooking."),
    ],
    ids=[
        "poultry-without-thermometer-template",
        "egg",
        "dairy",
        "shellfish",
        "flour",
        "dry-pulses",
        "leftovers",
        "frozen-poultry",
    ],
)
def test_regular_foods_and_cooking_methods_are_not_a_rejection_gate(name: str, text: str) -> None:
    recipe = full_recipe(names=(name,), instructions=(text,))
    assert validate_final_recipe(recipe, SafetyProfile()) == recipe
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


@pytest.mark.parametrize(
    "names,instruction",
    [
        (("Greek yogurt", "blueberries"), "Spoon the yogurt into a bowl and top with blueberries."),
        (("tomato", "cucumber"), "Chop the tomato and cucumber, then toss together."),
        (("bread", "cheddar"), "Layer the cheddar between the bread slices."),
        (("banana", "milk"), "Blend the banana with the milk until smooth."),
    ],
    ids=["yogurt-bowl", "salad", "sandwich", "smoothie"],
)
def test_no_cook_recipes_do_not_require_cooking_steps_or_temperature_templates(
    names: tuple[str, ...], instruction: str
) -> None:
    recipe = full_recipe(names=names, instructions=(instruction,))
    checked = validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",)))
    assert checked == recipe
    assert instruction in render_recipe(checked)


@pytest.mark.parametrize("name", ["canned beef", "dried beef", "cold beef", "toasted beef"])
def test_preparation_words_do_not_trigger_a_cooking_gate(name: str) -> None:
    recipe = full_recipe(names=(name,), instructions=(f"Prepare the {name} and serve.",))
    assert validate_final_recipe(recipe, SafetyProfile()) == recipe


def test_prose_does_not_change_the_list_used_for_allergy_validation() -> None:
    recipe = full_recipe(names=("carrot",), instructions=("Avoid peanut oil.",))
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


@pytest.mark.parametrize(
    "name,text,allergy",
    [
        ("olive oil", "Heat the oil, then cook the rice.", "peanut"),
        ("coconut milk", "Warm the milk, then add the rice.", "milk"),
        ("rice flour", "Mix the flour with the rice.", "gluten"),
    ],
)
def test_step_shorthand_uses_its_unambiguous_validated_ingredient_reference(
    name: str, text: str, allergy: str
) -> None:
    recipe = full_recipe(names=("rice", name), instructions=(text,))
    assert validate_final_recipe(recipe, SafetyProfile(allergies=(allergy,))) == recipe


@pytest.mark.parametrize(
    "names,text,allergy",
    [
        (("rice", "olive oil"), "Cook the rice with peanut oil.", "peanut"),
        (("rice", "coconut milk"), "Cook the rice with almond milk.", "tree nut"),
        (("rice", "coconut milk"), "Cook the rice with milk powder.", "milk"),
        (("rice", "coconut milk", "oat milk"), "Cook the rice with milk.", "milk"),
    ],
)
def test_step_text_is_not_an_additional_allergy_gate(
    names: tuple[str, ...], text: str, allergy: str
) -> None:
    recipe = full_recipe(names=names, instructions=(text,))
    assert validate_final_recipe(recipe, SafetyProfile(allergies=(allergy,))) == recipe


def test_generic_step_wording_does_not_change_declared_ingredient_matching() -> None:
    recipe = full_recipe(
        names=("rice", "olive oil"),
        instructions=("Cook the rice in oil.", "Drizzle the olive oil."),
        references=(("line0",), ("line1",)),
    )
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


def test_incorrect_instruction_references_and_model_copy_bypasses_are_revalidated() -> None:
    recipe = full_recipe()
    forged = recipe.model_copy(update={"servings": 0})
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(forged, SafetyProfile())
    recipe = recipe.model_copy(
        update={
            "instructions": (
                recipe.instructions[0].model_copy(update={"ingredient_line_ids": ("missing",)}),
            )
        }
    )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(recipe, SafetyProfile())


def test_edits_and_substitutions_cannot_reuse_an_old_pass() -> None:
    recipe = validate_final_recipe(full_recipe(), SafetyProfile(allergies=("peanut",)))
    edit = recipe.model_copy(
        update={
            "ingredients": (recipe.ingredients[0].model_copy(update={"name": "peanut oil"}),),
        }
    )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(edit, SafetyProfile(allergies=("peanut",)))


def test_natural_recipe_prose_and_metadata_do_not_require_a_whitelist() -> None:
    recipe = full_recipe(
        names=("chicken breast",),
        instructions=("Saut\u00e9 the chicken breast in a skillet until golden and fully cooked.",),
    ).model_copy(
        update={
            "title": "Golden chicken skillet",
            "fit_reasons": ("A weeknight option with straightforward preparation.",),
            "adaptations": ("Slice thinly for quicker cooking.",),
        }
    )
    assert validate_final_recipe(recipe, SafetyProfile(allergies=("peanut",))) == recipe


@pytest.mark.parametrize(
    "text,profile",
    [
        ("Finish with pesto.", SafetyProfile(allergies=("milk",))),
        ("Garnish with strawberries.", SafetyProfile(allergies=("strawberry",))),
        ("Simmer in chicken stock.", SafetyProfile(allergies=("chicken",))),
    ],
)
def test_freeform_prose_does_not_add_undeclared_ingredient_checks(
    text: str, profile: SafetyProfile
) -> None:
    recipe = full_recipe(instructions=(text,))
    assert validate_final_recipe(recipe, profile) == recipe


@pytest.mark.parametrize(
    "name",
    [
        "chicken breast",
        "boneless skinless chicken breast",
        "chicken thighs",
        "raw chicken",
        "raw chicken breast",
        "turkey breast",
        "duck breast",
        "CHICKEN BREASTS",
        "chicken-breast",
        "chicken breast, boneless, skinless",
        "chicken breasts (boneless, skinless)",
        "diced chicken breast",
        "cubed chicken",
    ],
)
def test_poultry_name_variants_pass_without_a_temperature_template(name: str) -> None:
    cooked_name = name.removeprefix("raw ")
    hot = f"Pan-fry the {name} until fully cooked."
    recipe = full_recipe(names=(name,), instructions=(hot, f"Serve the {cooked_name}."))
    assert validate_final_recipe(recipe, SafetyProfile()) == recipe
    assert (
        validate_final_recipe(recipe, SafetyProfile(dietary_restrictions=("vegetarian",))) == recipe
    )
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(recipe, SafetyProfile(allergies=("chicken", "turkey", "duck")))


@pytest.mark.parametrize(
    "name",
    ["frozen chicken breast", "cooked chicken breast", "chicken stock", "chicken mystery cut"],
)
def test_poultry_preparation_is_not_a_cooking_gate(name: str) -> None:
    recipe = full_recipe(names=(name,), instructions=(f"Prepare the {name} following the recipe.",))
    assert validate_final_recipe(recipe, SafetyProfile()) == recipe


@pytest.mark.parametrize(
    "name",
    [
        "chicken breast (peanut coating)",
        "chicken breast, peanut oil",
        "chicken breast without peanut",
    ],
)
def test_flexible_name_formatting_never_discards_allergens_or_preparation(name: str) -> None:
    with pytest.raises(RecipeSafetyError):
        validate_final_recipe(
            full_recipe(
                names=(name,),
                instructions=(
                    f"Cook the {name} to an internal temperature of 165 F, "
                    "measured with a food thermometer.",
                ),
            ),
            SafetyProfile(allergies=("peanut",)),
        )


class FullSpecialist(Specialist):
    def __init__(self, *recipes: StructuredRecipe) -> None:
        self.recipes = list(recipes)
        super().__init__(*(projection(recipe) for recipe in recipes))

    async def generate(self, *args: Any, **kwargs: Any) -> SpecialistResult:
        result = await super().generate(*args, **kwargs)
        return result.model_copy(update={"recipe": self.recipes.pop(0)})


def test_coordinator_projection_must_match_full_instruction_references() -> None:
    class MismatchedSpecialist(FullSpecialist):
        async def generate(self, *args: Any, **kwargs: Any) -> SpecialistResult:
            result = await super().generate(*args, **kwargs)
            wrong = result.candidate.model_copy(
                update={
                    "instructions": (
                        result.candidate.instructions[0].model_copy(
                            update={"ingredient_line_ids": ("line1",)}
                        ),
                        result.candidate.instructions[1],
                    )
                }
            )
            return result.model_copy(update={"candidate": wrong})

    recipe = full_recipe(
        names=("rice", "carrot"),
        instructions=("Cook the rice.", "Cook the carrot."),
        references=(("line0",), ("line1",)),
    )
    router = Coordinator(call(), answer())
    result = asyncio.run(OrchestratorService(router, MismatchedSpecialist(recipe)).run(context()))
    assert result.status == "unable" and result.recipe is None
    feedback = json.loads(router.requests[1].tool_output or "{}")
    assert feedback["candidate"] is None
    assert feedback["rejection_code"] == "invalid_recipe_structure"


def test_one_final_gate_regeneration_shares_the_existing_four_call_budget() -> None:
    bad = full_recipe(names=("rice", "peanut oil"))
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
    assert json.loads(correction)["rejection_code"] == "recipe_ingredient_rejected"


def test_repeated_ingredient_allergy_conflicts_stop_without_a_recipe_or_extra_budget() -> None:
    class AllergySpecialist(FullSpecialist):
        async def generate(self, *args: Any, **kwargs: Any) -> SpecialistResult:
            result = await super().generate(*args, **kwargs)
            raise RecipeAllergyError(
                FoundryRequest(
                    role=AgentRole.RECIPE_NUTRITION,
                    prompt="synthetic",
                    operation_id=kwargs["operation_id"],
                ),
                result.usage,
            )

    specialist = AllergySpecialist(full_recipe(), full_recipe())
    router = Coordinator(call(), call(call_id="substitution"))
    result = asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert result.status == "unable" and result.recipe is None and result.candidate is None
    budget = specialist.budgets[0]
    assert isinstance(budget, ExecutionBudget) and budget.model_calls == 4
    assert budget.input_tokens == 4 * USAGE.input_tokens
    assert budget.output_tokens == 4 * USAGE.output_tokens
    assert len(specialist.calls) == 2
    feedback = json.loads(router.requests[1].tool_output or "{}")
    assert feedback["candidate"] is None
    assert feedback["rejection_code"] == "recipe_ingredient_rejected"


def test_rejection_diagnostics_only_contain_allowlisted_categories(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    bad = full_recipe(names=("rice", "peanut oil"))
    router = Coordinator(call(), call(call_id="revision"))
    asyncio.run(OrchestratorService(router, FullSpecialist(bad, full_recipe())).run(context()))
    assert '"event":"recipe_safety_rejected"' in caplog.text
    assert '"error_category":"recipe_ingredient_rejected"' in caplog.text
    for private in ("peanut", "garnish", "Make rice", "Simple dinner"):
        assert private not in caplog.text


@pytest.mark.parametrize("accept_rejected", [False, True])
def test_rejected_recipe_never_returns_as_success_and_regeneration_terminates(
    accept_rejected: bool,
) -> None:
    bad = full_recipe(names=("rice", "peanut oil"))
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

    bad = full_recipe(names=("rice", "peanut oil"))
    specialist = NestedSpecialist(bad)
    router = Coordinator(call(), call(call_id="revision"))
    result = asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert result.status == "unable" and result.explanation == UNABLE
    assert len(specialist.calls) == 1
    budget = specialist.budgets[0]
    assert isinstance(budget, ExecutionBudget) and budget.model_calls == 4
