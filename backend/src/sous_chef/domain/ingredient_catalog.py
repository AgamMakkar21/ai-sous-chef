"""Versioned ingredient catalog and deterministic name matching for food-safety checks.

The catalog is reviewed data, not model output. Extend it only through reviewed changes and
bump ``CATALOG_VERSION`` when entries change.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

CATALOG_VERSION = "1"


class Trait(StrEnum):
    PEANUT = "peanut"
    TREE_NUT = "tree_nut"
    MILK = "milk"
    EGG = "egg"
    GLUTEN = "gluten"
    SOY = "soy"
    FISH = "fish"
    SHELLFISH = "shellfish"
    SESAME = "sesame"
    MEAT = "meat"
    HONEY = "honey"


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ingredient_id: str
    aliases: tuple[str, ...]
    # Traits the ingredient always carries.
    contains: frozenset[Trait]
    # Traits that depend on the product or recipe; they require clarification, never a pass.
    may_contain: frozenset[Trait]


# Words that describe preparation or amount and never change what the ingredient is.
DESCRIPTORS = frozenset(
    """a about additional an and approximately boneless canned chopped coarsely cold cooked
    crushed cut diced divided dried extra fine finely fresh freshly frozen garnish grated
    ground heaping hot large lean level medium minced more of optional or packed peeled pitted
    plain raw ripe roasted salted seeded shredded skinless sliced small softened sweetened
    taste the thinly to toasted trimmed unsalted unsweetened whole""".split()
)

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "sses")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> tuple[str, ...]:
    """Lowercase, strip accents and punctuation, and singularize each word."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return tuple(_singular(token) for token in _NON_WORD.split(folded) if token)


def _entry(
    slug: str,
    *aliases: str,
    contains: tuple[Trait, ...] = (),
    may: tuple[Trait, ...] = (),
) -> CatalogEntry:
    return CatalogEntry(
        ingredient_id=f"ingredient:{slug}",
        aliases=(slug.replace("-", " "), *aliases),
        contains=frozenset(contains),
        may_contain=frozenset(may),
    )


_T = Trait
_ENTRIES: list[CatalogEntry] = [
    # Peanut
    _entry("peanut", "groundnut", "goober", contains=(_T.PEANUT,)),
    _entry("peanut-oil", "groundnut oil", "arachis oil", contains=(_T.PEANUT,)),
    _entry("peanut-butter", contains=(_T.PEANUT,)),
    # Tree nuts
    _entry("almond", "almond extract", contains=(_T.TREE_NUT,)),
    _entry("almond-milk", contains=(_T.TREE_NUT,)),
    _entry("almond-flour", "almond meal", "ground almond", contains=(_T.TREE_NUT,)),
    _entry("almond-butter", contains=(_T.TREE_NUT,)),
    _entry("marzipan", "almond paste", contains=(_T.TREE_NUT,)),
    _entry("walnut", contains=(_T.TREE_NUT,)),
    _entry("cashew", "cashew butter", contains=(_T.TREE_NUT,)),
    _entry("pecan", contains=(_T.TREE_NUT,)),
    _entry("pistachio", contains=(_T.TREE_NUT,)),
    _entry("hazelnut", "filbert", contains=(_T.TREE_NUT,)),
    _entry("macadamia", "macadamia nut", contains=(_T.TREE_NUT,)),
    _entry("brazil-nut", contains=(_T.TREE_NUT,)),
    _entry("pine-nut", "pignoli", contains=(_T.TREE_NUT,)),
    _entry("chestnut", contains=(_T.TREE_NUT,)),
    _entry("water-chestnut"),
    _entry("nut", "mixed nut", may=(_T.TREE_NUT, _T.PEANUT)),
    _entry("nut-oil", may=(_T.TREE_NUT, _T.PEANUT)),
    _entry("pesto", may=(_T.TREE_NUT, _T.MILK)),
    _entry("coconut", "coconut milk", "coconut oil", "coconut cream", "coconut flour"),
    _entry("nutmeg"),
    _entry("butternut-squash", "butternut"),
    # Milk
    _entry(
        "milk",
        "whole milk",
        "skim milk",
        "buttermilk",
        "evaporated milk",
        "condensed milk",
        "sweetened condensed milk",
        "milk powder",
        "powdered milk",
        "dairy",
        contains=(_T.MILK,),
    ),
    _entry("butter", "ghee", "clarified butter", contains=(_T.MILK,)),
    _entry(
        "cream",
        "heavy cream",
        "whipping cream",
        "double cream",
        "half and half",
        "sour cream",
        "creme fraiche",
        contains=(_T.MILK,),
    ),
    _entry("cream-of-tartar"),
    _entry(
        "cheese",
        "cheddar",
        "mozzarella",
        "parmesan",
        "parmigiano reggiano",
        "feta",
        "ricotta",
        "cream cheese",
        "mascarpone",
        "gouda",
        "brie",
        "goat cheese",
        "cottage cheese",
        "halloumi",
        "paneer",
        contains=(_T.MILK,),
    ),
    _entry("yogurt", "yoghurt", "greek yogurt", contains=(_T.MILK,)),
    _entry("whey", "casein", "lactose", contains=(_T.MILK,)),
    _entry("ice-cream", contains=(_T.MILK,)),
    _entry("margarine", may=(_T.MILK, _T.SOY)),
    _entry("chocolate", "chocolate chip", "dark chocolate", may=(_T.MILK, _T.SOY)),
    _entry("oat-milk", may=(_T.GLUTEN,)),
    # Egg
    _entry("egg", "egg white", "egg yolk", "beaten egg", contains=(_T.EGG,)),
    _entry("mayonnaise", "mayo", "aioli", contains=(_T.EGG,)),
    _entry("meringue", contains=(_T.EGG,)),
    _entry("egg-noodle", contains=(_T.EGG, _T.GLUTEN)),
    _entry("eggplant", "aubergine"),
    # Gluten
    _entry(
        "wheat-flour",
        "flour",
        "all purpose flour",
        "plain flour",
        "bread flour",
        "self raising flour",
        "whole wheat flour",
        "wheat",
        contains=(_T.GLUTEN,),
    ),
    _entry("bread", "breadcrumb", "panko", "toast", "bun", "baguette", contains=(_T.GLUTEN,)),
    _entry(
        "pasta",
        "spaghetti",
        "penne",
        "macaroni",
        "lasagna",
        "fettuccine",
        "couscous",
        "semolina",
        contains=(_T.GLUTEN,),
    ),
    _entry("barley", "rye", "bulgur", "seitan", "malt", "beer", contains=(_T.GLUTEN,)),
    _entry("dough", "pizza dough", "pie crust", "cracker", contains=(_T.GLUTEN,)),
    _entry("noodle", may=(_T.GLUTEN, _T.EGG)),
    _entry("tortilla", may=(_T.GLUTEN,)),
    _entry("oat", "rolled oat", "oatmeal", may=(_T.GLUTEN,)),
    _entry("rice-noodle"),
    _entry("rice-flour", "corn flour", "chickpea flour", "cornstarch", "cornmeal"),
    # Soy
    _entry("soybean", "soy", "soya", "edamame", "tofu", "tempeh", contains=(_T.SOY,)),
    _entry("soy-milk", "soya milk", contains=(_T.SOY,)),
    _entry("soy-sauce", "shoyu", contains=(_T.SOY, _T.GLUTEN)),
    _entry("tamari", contains=(_T.SOY,), may=(_T.GLUTEN,)),
    _entry("miso", contains=(_T.SOY,), may=(_T.GLUTEN,)),
    _entry("vegetable-oil", "cooking oil", "oil", may=(_T.SOY, _T.PEANUT, _T.SESAME, _T.TREE_NUT)),
    # Fish
    _entry(
        "fish",
        "salmon",
        "tuna",
        "cod",
        "tilapia",
        "trout",
        "halibut",
        "mackerel",
        "sardine",
        "anchovy",
        "bonito",
        "caviar",
        "fish stock",
        "dashi",
        contains=(_T.FISH,),
    ),
    _entry("fish-sauce", "worcestershire sauce", contains=(_T.FISH,)),
    _entry("caesar-dressing", contains=(_T.EGG,), may=(_T.FISH, _T.MILK)),
    # Shellfish
    _entry(
        "shellfish",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "crayfish",
        "scallop",
        "clam",
        "mussel",
        "oyster",
        "squid",
        "shrimp paste",
        "oyster sauce",
        contains=(_T.SHELLFISH,),
    ),
    _entry("curry-paste", may=(_T.SHELLFISH, _T.FISH)),
    # Sesame
    _entry(
        "sesame",
        "sesame seed",
        "sesame oil",
        "tahini",
        "hummus",
        "halva",
        contains=(_T.SESAME,),
    ),
    # Meat, poultry and meat-derived products
    _entry(
        "meat",
        "chicken",
        "beef",
        "pork",
        "lamb",
        "turkey",
        "duck",
        "veal",
        "venison",
        "bacon",
        "ham",
        "sausage",
        "chorizo",
        "salami",
        "pepperoni",
        "prosciutto",
        "steak",
        "meatball",
        "hot dog",
        "lard",
        "suet",
        "tallow",
        "gelatin",
        "chicken stock",
        "beef stock",
        "chicken broth",
        "beef broth",
        "bone broth",
        "meat stock",
        contains=(_T.MEAT,),
    ),
    _entry("stock", "broth", "bouillon", "stock cube", may=(_T.MEAT, _T.FISH, _T.SHELLFISH)),
    _entry("vegetable-stock", "vegetable broth", "mushroom stock"),
    # Honey
    _entry("honey", contains=(_T.HONEY,)),
    # Everything below has no tracked trait in this catalog version.
    _entry("water", "ice"),
    _entry("salt", "kosher salt", "sea salt", "black pepper", "pepper", "peppercorn"),
    _entry("sugar", "brown sugar", "powdered sugar", "maple syrup", "agave", "molasses"),
    _entry("olive-oil", "canola oil", "sunflower oil", "avocado oil"),
    _entry("rice", "basmati rice", "jasmine rice", "quinoa", "polenta"),
    _entry("baking-soda", "baking powder", "yeast", "vinegar", "vanilla", "cocoa powder"),
    _entry("lemon", "lime", "orange", "lemon juice", "lime juice", "lemon zest", "lime zest"),
    _entry(
        "apple",
        "banana",
        "strawberry",
        "blueberry",
        "raspberry",
        "mango",
        "pineapple",
        "peach",
        "pear",
        "cherry",
        "plum",
        "grape",
        "watermelon",
        "melon",
        "kiwi",
    ),
    _entry(
        "garlic",
        "onion",
        "shallot",
        "scallion",
        "green onion",
        "leek",
        "tomato",
        "tomato sauce",
        "tomato paste",
        "potato",
        "sweet potato",
        "carrot",
        "celery",
        "cucumber",
        "spinach",
        "kale",
        "lettuce",
        "cabbage",
        "broccoli",
        "cauliflower",
        "zucchini",
        "bell pepper",
        "mushroom",
        "corn",
        "pea",
        "avocado",
    ),
    _entry("chickpea", "lentil", "black bean", "kidney bean", "white bean", "bean"),
    _entry(
        "parsley",
        "cilantro",
        "basil",
        "oregano",
        "thyme",
        "rosemary",
        "mint",
        "dill",
        "chive",
        "bay leaf",
        "cumin",
        "paprika",
        "cinnamon",
        "ginger",
        "turmeric",
        "cayenne",
        "chili powder",
        "red pepper flake",
        "garlic powder",
        "onion powder",
        "coriander",
        "clove",
    ),
]


def _build_index(entries: list[CatalogEntry]) -> dict[tuple[str, ...], CatalogEntry]:
    index: dict[tuple[str, ...], CatalogEntry] = {}
    for entry in entries:
        for alias in entry.aliases:
            key = tokenize(alias)
            if key in index and index[key] is not entry:
                raise ValueError(f"alias {alias!r} is defined by more than one catalog entry")
            index[key] = entry
    return index


_INDEX = _build_index(_ENTRIES)
_MAX_ALIAS_TOKENS = max(len(key) for key in _INDEX)


def catalog_entries() -> tuple[CatalogEntry, ...]:
    return tuple(_ENTRIES)


class Resolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[CatalogEntry, ...]
    # Words the catalog could not explain. Any unresolved word means the line is not known.
    unresolved: tuple[str, ...]

    @property
    def is_known(self) -> bool:
        return bool(self.entries) and not self.unresolved


def resolve(name: str) -> Resolution:
    """Explain every word of an ingredient name with catalog entries or descriptors."""
    tokens = tokenize(name)
    entries: list[CatalogEntry] = []
    unresolved: list[str] = []
    position = 0
    while position < len(tokens):
        for length in range(min(_MAX_ALIAS_TOKENS, len(tokens) - position), 0, -1):
            entry = _INDEX.get(tokens[position : position + length])
            if entry is not None:
                entries.append(entry)
                position += length
                break
        else:
            if tokens[position] not in DESCRIPTORS:
                unresolved.append(tokens[position])
            position += 1
    return Resolution(entries=tuple(entries), unresolved=tuple(unresolved))
