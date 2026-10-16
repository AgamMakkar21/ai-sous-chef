"""Deterministic allergy and strict-dietary-restriction validation of final ingredient lists.

No model decides safety here. The validator fails closed: an ingredient the catalog cannot
fully explain is never reported as safe.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sous_chef.domain.ingredient_catalog import CATALOG_VERSION, Trait, resolve, tokenize


class Verdict(StrEnum):
    PASS = "pass"
    BLOCKED = "blocked"
    NEEDS_CLARIFICATION = "needs_clarification"


class FindingKind(StrEnum):
    VIOLATION = "violation"
    AMBIGUOUS = "ambiguous"
    UNKNOWN_INGREDIENT = "unknown_ingredient"
    UNSUPPORTED_RESTRICTION = "unsupported_restriction"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IngredientLine(_Frozen):
    line_id: str
    name: str


class SafetyProfile(_Frozen):
    """Stored explicit safety state.

    The model forbids extra fields, so a request cannot smuggle an override in.
    """

    allergies: tuple[str, ...] = ()
    dietary_restrictions: tuple[str, ...] = ()
    # Soft preferences never change the verdict.
    dislikes: tuple[str, ...] = ()


class Finding(_Frozen):
    kind: FindingKind
    line_id: str | None = None
    ingredient: str | None = None
    ingredient_ids: tuple[str, ...] = ()
    restriction: str | None = None
    message: str


class DislikeMatch(_Frozen):
    line_id: str
    ingredient: str
    dislike: str


class SafetyResult(_Frozen):
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    dislike_matches: tuple[DislikeMatch, ...] = ()
    catalog_version: str = Field(default=CATALOG_VERSION)


_NOT_NAMING_A_FOOD = frozenset(
    "allergy allergic allergen intolerance intolerant sensitivity free no avoid to".split()
)

_ALL_ANIMAL = frozenset({Trait.MEAT, Trait.FISH, Trait.SHELLFISH})
_SUPPORTED_RESTRICTIONS: dict[str, frozenset[Trait]] = {
    "peanut": frozenset({Trait.PEANUT}),
    "tree nut": frozenset({Trait.TREE_NUT}),
    "nut": frozenset({Trait.TREE_NUT, Trait.PEANUT}),
    "milk": frozenset({Trait.MILK}),
    "dairy": frozenset({Trait.MILK}),
    "lactose": frozenset({Trait.MILK}),
    "egg": frozenset({Trait.EGG}),
    "gluten": frozenset({Trait.GLUTEN}),
    "wheat": frozenset({Trait.GLUTEN}),
    "soy": frozenset({Trait.SOY}),
    "soya": frozenset({Trait.SOY}),
    "fish": frozenset({Trait.FISH}),
    "shellfish": frozenset({Trait.SHELLFISH}),
    "seafood": frozenset({Trait.FISH, Trait.SHELLFISH}),
    "sesame": frozenset({Trait.SESAME}),
    "vegetarian": _ALL_ANIMAL,
    "vegan": _ALL_ANIMAL | {Trait.MILK, Trait.EGG, Trait.HONEY},
    "pescatarian": frozenset({Trait.MEAT}),
    "pescetarian": frozenset({Trait.MEAT}),
}
_RESTRICTION_INDEX = {tokenize(name): traits for name, traits in _SUPPORTED_RESTRICTIONS.items()}


def _traits_for(restriction: str) -> frozenset[Trait] | None:
    tokens = tuple(t for t in tokenize(restriction) if t not in _NOT_NAMING_A_FOOD)
    return _RESTRICTION_INDEX.get(tokens)


def supports_restriction(restriction: str, *, allergy: bool = False) -> bool:
    """Use the same supported traits and custom-food rules as final safety checks."""
    return _traits_for(restriction) is not None or (
        allergy and any(token not in _NOT_NAMING_A_FOOD for token in tokenize(restriction))
    )


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return size > 0 and any(tokens[i : i + size] == phrase for i in range(len(tokens) - size + 1))


def validate_ingredients(
    profile: SafetyProfile, ingredients: tuple[IngredientLine, ...] | list[IngredientLine]
) -> SafetyResult:
    """Check the complete final ingredient list against stored hard restrictions."""
    findings: list[Finding] = []
    forbidden: dict[Trait, str] = {}
    custom_allergies: list[tuple[str, tuple[str, ...]]] = []

    for restriction in (*profile.allergies, *profile.dietary_restrictions):
        traits = _traits_for(restriction)
        if traits is not None:
            for trait in sorted(traits):
                forbidden.setdefault(trait, restriction)
        elif restriction in profile.allergies:
            # Custom / specific food allergy outside FDA catalog traits (e.g. "peaches", "kiwi")
            cleaned_tokens = tuple(t for t in tokenize(restriction) if t not in _NOT_NAMING_A_FOOD)
            if cleaned_tokens:
                custom_allergies.append((restriction, cleaned_tokens))
            else:
                findings.append(
                    Finding(
                        kind=FindingKind.UNSUPPORTED_RESTRICTION,
                        restriction=restriction,
                        message=(
                            f"The restriction {restriction!r} is not supported by catalog version "
                            f"{CATALOG_VERSION}, so safety cannot be confirmed."
                        ),
                    )
                )
        else:
            findings.append(
                Finding(
                    kind=FindingKind.UNSUPPORTED_RESTRICTION,
                    restriction=restriction,
                    message=(
                        f"The restriction {restriction!r} is not supported by catalog version "
                        f"{CATALOG_VERSION}, so safety cannot be confirmed."
                    ),
                )
            )

    has_hard_restrictions = bool(profile.allergies or profile.dietary_restrictions)
    if has_hard_restrictions and not ingredients:
        findings.append(
            Finding(
                kind=FindingKind.UNKNOWN_INGREDIENT,
                message="There are no ingredients to validate, so safety cannot be confirmed.",
            )
        )

    dislike_phrases = [(d, tokenize(d)) for d in profile.dislikes]
    dislike_matches: list[DislikeMatch] = []
    for line in ingredients:
        line_tokens = tokenize(line.name)
        for dislike, phrase in dislike_phrases:
            if _contains_phrase(line_tokens, phrase):
                dislike_matches.append(
                    DislikeMatch(line_id=line.line_id, ingredient=line.name, dislike=dislike)
                )
        if has_hard_restrictions:
            findings.extend(_check_line(line, forbidden, custom_allergies))

    if any(f.kind is FindingKind.VIOLATION for f in findings):
        verdict = Verdict.BLOCKED
    elif findings:
        verdict = Verdict.NEEDS_CLARIFICATION
    else:
        verdict = Verdict.PASS
    return SafetyResult(
        verdict=verdict, findings=tuple(findings), dislike_matches=tuple(dislike_matches)
    )


def _check_line(
    line: IngredientLine,
    forbidden: dict[Trait, str],
    custom_allergies: list[tuple[str, tuple[str, ...]]],
) -> list[Finding]:
    resolution = resolve(line.name)
    ids = tuple(dict.fromkeys(entry.ingredient_id for entry in resolution.entries))
    findings: list[Finding] = []
    line_tokens = tokenize(line.name)

    # Check custom literal food allergies (e.g. peach, strawberry)
    for raw_restriction, phrase in custom_allergies:
        if _contains_phrase(line_tokens, phrase):
            findings.append(
                Finding(
                    kind=FindingKind.VIOLATION,
                    line_id=line.line_id,
                    ingredient=line.name,
                    ingredient_ids=ids,
                    restriction=raw_restriction,
                    message=f"{line.name!r} matches custom allergy {raw_restriction!r}.",
                )
            )

    certain = {trait for entry in resolution.entries for trait in entry.contains}
    possible = {trait for entry in resolution.entries for trait in entry.may_contain} - certain
    for trait in sorted(certain & forbidden.keys()):
        findings.append(
            Finding(
                kind=FindingKind.VIOLATION,
                line_id=line.line_id,
                ingredient=line.name,
                ingredient_ids=ids,
                restriction=forbidden[trait],
                message=f"{line.name!r} contains {trait.value}, which conflicts with "
                f"the stored restriction {forbidden[trait]!r}.",
            )
        )
    for trait in sorted(possible & forbidden.keys()):
        findings.append(
            Finding(
                kind=FindingKind.AMBIGUOUS,
                line_id=line.line_id,
                ingredient=line.name,
                ingredient_ids=ids,
                restriction=forbidden[trait],
                message=f"{line.name!r} may contain {trait.value}, which conflicts with "
                f"the stored restriction {forbidden[trait]!r}; confirm the exact product.",
            )
        )
    if not resolution.is_known:
        detail = ", ".join(resolution.unresolved) or "no recognizable ingredient"
        findings.append(
            Finding(
                kind=FindingKind.UNKNOWN_INGREDIENT,
                line_id=line.line_id,
                ingredient=line.name,
                ingredient_ids=ids,
                message=f"{line.name!r} is not fully recognized ({detail}), so it cannot be "
                "confirmed safe.",
            )
        )
    return findings
