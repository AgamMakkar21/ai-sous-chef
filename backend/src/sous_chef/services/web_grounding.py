"""Minimized public queries; private recipe context never enters the web-only invocation."""

import re

from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.orchestrator import SpecialistArguments
from sous_chef.domain.recipes import RecipeToolError

_PRIVATE = re.compile(
    r"@|://|www\.|\b(?:my|mine|patient|diagnosed|address|email|phone)\b"
    r"|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b"
    r"|\d[\d\s().+-]{6,}\d",
    re.IGNORECASE,
)
_PUBLIC_WORDS = set(
    "fda usda current latest recent updated today food foods recall recalls safety "
    "seasonal season produce cooking technique techniques guidelines nutrition news "
    "trend trends public information recommendations week month year for of and in the on".split()
)


def public_ingredient_name(name: str) -> str | None:
    """Keep recipe food names intact, excluding obvious private identifiers."""
    return None if _PRIVATE.search(name) else " ".join(name.split())


def quantity_web_query(arguments: SpecialistArguments) -> str | None:
    """Build a bounded query from AI-classified ingredient names, not user prose."""
    foods = [
        name
        for value in arguments.quantity_ingredients
        if (name := public_ingredient_name(value)) is not None
    ]
    if not foods:
        return None
    suffix = " recipe ingredient amounts in grams and number of servings"
    query = ""
    for food in dict.fromkeys(foods):
        if len(query) + len(food) + 1 + len(suffix) <= 240:
            query = (query + " " + food).strip()
    return query + suffix if query else None


def public_web_query(context: RequestContext, arguments: SpecialistArguments) -> str | None:
    if not arguments.use_web:
        return quantity_web_query(arguments)
    query = arguments.web_query
    if query is None or _PRIVATE.search(query):
        raise RecipeToolError(FoundryFailure.REJECTED)
    request_words = set(re.findall(r"\w+", context.current_request.casefold()))
    query_words = set(re.findall(r"\w+", query.casefold()))
    if not query_words or not query_words <= request_words | _PUBLIC_WORDS:
        raise RecipeToolError(FoundryFailure.REJECTED)
    return query
