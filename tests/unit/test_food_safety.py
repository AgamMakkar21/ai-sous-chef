import pytest
from pydantic import ValidationError
from sous_chef.domain.food_safety import (
    FindingKind,
    IngredientLine,
    SafetyProfile,
    SafetyResult,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.ingredient_catalog import (
    CATALOG_VERSION,
    DESCRIPTORS,
    Trait,
    catalog_entries,
    resolve,
    tokenize,
)


def lines(*names: str) -> list[IngredientLine]:
    return [IngredientLine(line_id=f"l{i}", name=name) for i, name in enumerate(names, 1)]


def check(profile: SafetyProfile, *names: str) -> SafetyResult:
    return validate_ingredients(profile, lines(*names))


PEANUT = SafetyProfile(allergies=("peanuts",))
VEGETARIAN = SafetyProfile(dietary_restrictions=("vegetarian",))


@pytest.mark.parametrize(
    "name",
    ["peanut oil", "Groundnut Oil", "arachis oil", "roasted peanuts", "peanut butter"],
)
def test_peanut_aliases_are_blocked(name: str) -> None:
    result = check(PEANUT, "salt", name)
    assert result.verdict is Verdict.BLOCKED
    finding = result.findings[0]
    assert finding.kind is FindingKind.VIOLATION
    assert finding.line_id == "l2"
    assert finding.restriction == "peanuts"


@pytest.mark.parametrize(
    "name",
    ["chicken stock", "beef broth", "bacon", "gelatin", "lard", "Salmon", "anchovy paste"],
)
def test_vegetarian_meat_and_stock_are_blocked(name: str) -> None:
    result = check(VEGETARIAN, "carrot", name)
    assert result.verdict is Verdict.BLOCKED


def test_substitutions_that_introduce_allergens_are_blocked() -> None:
    profile = SafetyProfile(allergies=("tree nuts",))
    result = check(profile, "almond milk", "olive oil")
    assert result.verdict is Verdict.BLOCKED
    assert result.findings[0].ingredient_ids == ("ingredient:almond-milk",)


def test_dairy_substitute_does_not_hide_dairy_in_the_same_list() -> None:
    result = check(SafetyProfile(allergies=("milk",)), "oat milk", "butter")
    assert result.verdict is Verdict.BLOCKED


@pytest.mark.parametrize("name", ["special house sauce", "secret spice blend", "dragonfruit"])
def test_unknown_ingredients_are_not_safe(name: str) -> None:
    result = check(PEANUT, "salt", name)
    assert result.verdict is Verdict.NEEDS_CLARIFICATION
    assert result.findings[0].kind is FindingKind.UNKNOWN_INGREDIENT


def test_known_word_does_not_hide_an_unknown_word() -> None:
    result = check(PEANUT, "salt and mystery crunch")
    assert result.verdict is Verdict.NEEDS_CLARIFICATION


def test_ambiguous_compound_ingredients_need_clarification() -> None:
    result = check(VEGETARIAN, "vegetable stock", "stock")
    assert result.verdict is Verdict.NEEDS_CLARIFICATION
    assert {f.kind for f in result.findings} == {FindingKind.AMBIGUOUS}
    assert result.findings[0].line_id == "l2"


def test_ambiguity_only_matters_when_the_trait_is_restricted() -> None:
    assert check(SafetyProfile(allergies=("egg",)), "curry paste").verdict is Verdict.PASS


def test_known_compliant_recipe_passes_with_an_explanation() -> None:
    result = check(
        SafetyProfile(allergies=("peanut", "shellfish"), dietary_restrictions=("vegetarian",)),
        "fresh spinach, chopped",
        "olive oil",
        "Lemon juice",
        "cooked basmati rice",
        "kosher salt",
    )
    assert result.verdict is Verdict.PASS
    assert result.findings == ()
    assert result.catalog_version == CATALOG_VERSION


def test_result_is_deterministic() -> None:
    profile = SafetyProfile(allergies=("peanut", "egg"))
    names = ("mayonnaise", "peanut oil", "mystery sauce")
    assert check(profile, *names) == check(profile, *names)


def test_blocked_takes_precedence_over_clarification() -> None:
    result = check(PEANUT, "mystery sauce", "peanut butter")
    assert result.verdict is Verdict.BLOCKED
    assert {f.kind for f in result.findings} == {
        FindingKind.VIOLATION,
        FindingKind.UNKNOWN_INGREDIENT,
    }


def test_vegan_blocks_honey_dairy_and_egg() -> None:
    profile = SafetyProfile(dietary_restrictions=("vegan",))
    result = check(profile, "honey", "cheddar cheese", "egg yolk")
    assert [f.line_id for f in result.findings] == ["l1", "l2", "l3"]
    assert result.verdict is Verdict.BLOCKED


def test_pescatarian_allows_fish_but_not_meat() -> None:
    profile = SafetyProfile(dietary_restrictions=("pescatarian",))
    assert check(profile, "salmon", "lemon").verdict is Verdict.PASS
    assert check(profile, "chicken").verdict is Verdict.BLOCKED


def test_unsupported_restriction_is_reported_not_ignored() -> None:
    profile = SafetyProfile(dietary_restrictions=("keto",))
    result = check(profile, "salt")
    assert result.verdict is Verdict.NEEDS_CLARIFICATION
    assert result.findings[0].kind is FindingKind.UNSUPPORTED_RESTRICTION


def test_custom_food_allergy_blocks_ingredient_and_allows_compliant() -> None:
    profile = SafetyProfile(allergies=("peaches",))
    # Direct match should be blocked
    blocked_result = check(profile, "sliced peaches", "sugar")
    assert blocked_result.verdict is Verdict.BLOCKED
    assert blocked_result.findings[0].kind is FindingKind.VIOLATION
    assert blocked_result.findings[0].restriction == "peaches"
    assert blocked_result.findings[0].line_id == "l1"

    # Peach pie (compound containing peach)
    pie_result = check(profile, "peach pie")
    assert pie_result.verdict is Verdict.BLOCKED

    # Compliant recipe with recognized safe ingredients passes cleanly
    pass_result = check(profile, "fresh spinach", "olive oil", "kosher salt")
    assert pass_result.verdict is Verdict.PASS


def test_empty_ingredient_list_with_restrictions_is_not_safe() -> None:
    assert validate_ingredients(PEANUT, []).verdict is Verdict.NEEDS_CLARIFICATION


def test_no_restrictions_means_nothing_to_violate() -> None:
    assert check(SafetyProfile(), "mystery sauce", "peanut oil").verdict is Verdict.PASS


def test_dislikes_are_reported_but_never_change_the_verdict() -> None:
    profile = SafetyProfile(allergies=("egg",), dislikes=("mushrooms",))
    result = check(profile, "sliced mushrooms", "salt")
    assert result.verdict is Verdict.PASS
    assert [m.line_id for m in result.dislike_matches] == ["l1"]


def test_a_dislike_cannot_weaken_an_allergy() -> None:
    profile = SafetyProfile(allergies=("peanut",), dislikes=("peanut",))
    assert check(profile, "peanut butter").verdict is Verdict.BLOCKED


def test_profile_rejects_override_fields() -> None:
    with pytest.raises(ValidationError):
        SafetyProfile.model_validate({"allergies": ["peanut"], "allow_allergens": ["peanut"]})
    with pytest.raises(ValidationError):
        SafetyProfile.model_validate({"allergies": ["peanut"], "override": True})


@pytest.mark.parametrize(
    ("name", "traits"),
    [
        ("eggplant", set()),
        ("coconut milk", set()),
        ("nutmeg", set()),
        ("cream of tartar", set()),
        ("water chestnuts", set()),
        ("peanut butter", {Trait.PEANUT}),
        ("heavy cream", {Trait.MILK}),
    ],
)
def test_lookalike_names_resolve_to_the_right_entry(name: str, traits: set[Trait]) -> None:
    resolution = resolve(name)
    assert resolution.is_known
    assert {t for e in resolution.entries for t in e.contains} == traits


def test_catalog_integrity() -> None:
    entries = catalog_entries()
    ids = [e.ingredient_id for e in entries]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("ingredient:") for i in ids)
    assert not any(
        tokenize(alias)[0] in DESCRIPTORS and len(tokenize(alias)) == 1
        for e in entries
        for alias in e.aliases
    )
