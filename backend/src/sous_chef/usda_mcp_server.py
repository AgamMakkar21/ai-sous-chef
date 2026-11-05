"""Read-only FoodData Central REST tools, exposed over app-owned MCP stdio."""

import asyncio
import json
import os
import re
from decimal import Decimal
from time import monotonic
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from sous_chef.domain.nutrition import (
    SourceText,
    UsdaFood,
    UsdaFoodProfile,
    UsdaFoodSearch,
    UsdaNutrientRow,
)

API_ROOT = "https://api.nal.usda.gov/fdc/v1/"
MAX_RESPONSE_BYTES = 2_097_152
PAGE_SIZE = 200
_FORMS = {
    "raw",
    "uncooked",
    "cooked",
    "roasted",
    "fried",
    "boiled",
    "baked",
    "grilled",
    "steamed",
    "stewed",
    "broiled",
    "sauteed",
}
_NUTRIENTS = {
    1008: ("Energy", "kcal"),
    2048: ("Energy", "kcal"),
    2047: ("Energy", "kcal"),
    1003: ("Protein", "g"),
    1005: ("Carbohydrate, by difference", "g"),
    1004: ("Total lipid (fat)", "g"),
    1079: ("Fiber, total dietary", "g"),
    1093: ("Sodium, Na", "mg"),
}


class FdcError(RuntimeError):
    """Only allowlisted codes cross the MCP boundary; never HTTP bodies or keys."""

    def __init__(
        self,
        code: Literal[
            "timeout",
            "unavailable",
            "authentication",
            "throttled",
            "invalid_result",
            "no_results",
            "rejected",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class _Category(_ApiModel):
    description: SourceText


class _Food(_ApiModel):
    fdc_id: int = Field(alias="fdcId", strict=True, gt=0, le=9_999_999_999)
    description: SourceText
    data_type: Literal["Foundation", "SR Legacy"] = Field(alias="dataType")
    publication_date: SourceText | None = Field(default=None, alias="publicationDate")
    category: SourceText | _Category | None = Field(default=None, alias="foodCategory")

    def food(self) -> UsdaFood:
        category = (
            self.category.description if isinstance(self.category, _Category) else self.category
        )
        return UsdaFood(
            fdc_id=str(self.fdc_id),
            long_desc=self.description,
            shrt_desc=self.description,
            food_group=category or self.data_type,
            data_type=self.data_type,
            publication_date=self.publication_date,
        )


class _Search(_ApiModel):
    foods: list[_Food] = Field(max_length=PAGE_SIZE)
    total_hits: int = Field(alias="totalHits", strict=True, ge=0)


class _Nutrient(_ApiModel):
    id: int = Field(strict=True, gt=0)
    unit_name: SourceText = Field(alias="unitName")


class _Amount(_ApiModel):
    nutrient: _Nutrient
    amount: Decimal | None = Field(default=None, ge=0, le=10_000_000)


class _Details(_Food):
    nutrients: list[_Amount] = Field(alias="foodNutrients", max_length=256)


def _words(value: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[^\W_]+", value.casefold())))


def _rank(food: UsdaFood, words: list[str]) -> tuple[int, int, str, int]:
    tokens = _words(food.long_desc)
    prepared = any(
        term in food.food_group.casefold()
        for term in (
            "baby",
            "fast food",
            "restaurant",
            "meals",
            "soups",
        )
    ) or any(term in tokens for term in ("babyfood", "restaurant"))
    return (
        0 if set(tokens) == set(words) else 1,
        2 if prepared else 0 if "raw" in tokens else 1,
        food.long_desc.casefold(),
        int(food.fdc_id),
    )


class FoodDataCentral:
    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr) -> None:
        self._client = client
        self._api_key = api_key
        self._retry_at = 0.0

    async def _request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> bytes:
        if monotonic() < self._retry_at:
            raise FdcError("throttled")
        try:
            async with self._client.stream(
                method,
                API_ROOT + path,
                headers={"X-Api-Key": self._api_key.get_secret_value()},
                json=body,
            ) as response:
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "")
                    seconds = (
                        max(1, min(int(retry), 3600))
                        if retry.isascii() and retry.isdecimal() and len(retry) <= 5
                        else 3600
                    )
                    self._retry_at = monotonic() + seconds
                    raise FdcError("throttled")
                if response.status_code in {401, 403}:
                    raise FdcError("authentication")
                if response.status_code == 404:
                    raise FdcError("no_results")
                if response.status_code != 200:
                    raise FdcError("unavailable")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise FdcError("invalid_result")
                return bytes(data)
        except httpx.TimeoutException:
            raise FdcError("timeout") from None
        except httpx.HTTPError:
            raise FdcError("unavailable") from None

    async def search(self, keyword: str, limit: int = 20) -> UsdaFoodSearch:
        words = _words(keyword)
        if (
            not words
            or len(keyword) > 100
            or any(c in keyword for c in "%_\x00\r\n")
            or not 1 <= limit <= 20
        ):
            raise FdcError("rejected")
        raw = await self._request(
            "POST",
            "foods/search",
            {
                "query": " ".join("+" + word for word in words),
                "dataType": ["Foundation", "SR Legacy"],
                "pageSize": PAGE_SIZE,
                "pageNumber": 1,
            },
        )
        try:
            result = _Search.model_validate_json(raw)
            if result.total_hits < len(result.foods) or len(
                {food.fdc_id for food in result.foods}
            ) != len(result.foods):
                raise FdcError("invalid_result")
            foods = [food.food() for food in result.foods]
            foods = [
                food
                for food in foods
                if all(
                    word in _words(food.long_desc)
                    if word in _FORMS
                    else word in food.long_desc.casefold()
                    for word in words
                )
            ]
            foods.sort(key=lambda food: _rank(food, words))
            return UsdaFoodSearch(
                foods=tuple(foods[:limit]),
                truncated=result.total_hits > len(result.foods) or len(foods) > limit,
            )
        except ValidationError:
            raise FdcError("invalid_result") from None

    async def profile(self, fdc_id: str) -> UsdaFoodProfile:
        if not re.fullmatch(r"[1-9][0-9]{0,9}", fdc_id):
            raise FdcError("rejected")
        raw = await self._request("GET", "food/" + fdc_id)
        try:
            result = _Details.model_validate_json(raw)
            food = result.food()
            if food.fdc_id != fdc_id:
                raise FdcError("invalid_result")
            values = {}
            for item in result.nutrients:
                if item.nutrient.id not in _NUTRIENTS or item.amount is None:
                    continue
                if item.nutrient.id in values:
                    raise FdcError("invalid_result")
                name, unit = _NUTRIENTS[item.nutrient.id]
                if item.nutrient.unit_name.casefold() != unit:
                    raise FdcError("invalid_result")
                values[item.nutrient.id] = item.amount
            energy = next((id for id in (1008, 2048, 2047) if id in values), None)
            rows = [
                UsdaNutrientRow(
                    long_desc=food.long_desc,
                    shrt_desc=food.shrt_desc,
                    food_group=food.food_group,
                    nutrdesc=_NUTRIENTS[id][0],
                    units=_NUTRIENTS[id][1],
                    nutr_val=amount,
                    sr_order=index,
                )
                for index, (id, amount) in enumerate(values.items())
                if _NUTRIENTS[id][0] != "Energy" or id == energy
            ]
            if not rows:
                raise FdcError("no_results")
            return UsdaFoodProfile(food=food, nutrients=tuple(rows))
        except ValidationError:
            raise FdcError("invalid_result") from None


def create_server(api: FoodDataCentral) -> FastMCP:
    server = FastMCP("usda-fooddata-central")

    @server.tool()
    async def search_foods(keyword: str, limit: int = 20) -> str:
        """Return source candidates; never guess a cut, skin or preparation."""
        try:
            return (await api.search(keyword, limit)).model_dump_json()
        except FdcError as error:
            return json.dumps({"error": error.code})

    @server.tool()
    async def get_nutrition_profile(fdc_id: str) -> str:
        """Return per-100-g source nutrition for an original FoodData Central ID."""
        try:
            return (await api.profile(fdc_id)).model_dump_json()
        except FdcError as error:
            return json.dumps({"error": error.code})

    return server


async def main() -> None:
    key = os.environ.get("USDA_API_KEY", "")
    if not key or key.strip() != key:
        raise SystemExit("USDA_API_KEY must be configured in protected server settings.")
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        server = create_server(FoodDataCentral(client, SecretStr(key)))
        await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
