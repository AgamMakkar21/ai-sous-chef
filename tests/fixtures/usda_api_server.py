"""Real production MCP server with a synthetic HTTP transport, never live USDA."""

import asyncio
import json
import os

import httpx
from pydantic import SecretStr
from sous_chef.usda_mcp_server import FoodDataCentral, create_server

FOODS = [
    {
        "fdcId": 1234567,
        "description": "Carrots, raw",
        "dataType": "SR Legacy",
        "publicationDate": "4/1/2019",
        "foodCategory": "Vegetables",
    },
    {
        "fdcId": 2345678,
        "description": "Chicken, breast, meat only, raw",
        "dataType": "Foundation",
        "publicationDate": "2024-04-18",
        "foodCategory": "Poultry Products",
    },
    {
        "fdcId": 2345679,
        "description": "Chicken, thigh, meat and skin, raw",
        "dataType": "SR Legacy",
    },
    {"fdcId": 2345680, "description": "Chicken, breast, roasted", "dataType": "SR Legacy"},
    *[
        {
            "fdcId": 3000000 + index,
            "description": f"Babyfood, chicken dinner {index}",
            "dataType": "SR Legacy",
            "foodCategory": "Baby Foods",
        }
        for index in range(30)
    ],
]


def respond(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Api-Key"] == "synthetic-private-key"
    assert request.url.host == "api.nal.usda.gov" and request.url.scheme == "https"
    assert "ASC_PAGINATION_KEY" not in os.environ
    assert not any(name.startswith("USDA_POSTGRES_") for name in os.environ)
    mode = os.environ.get("TEST_USDA_MODE", "")
    if mode == "error":
        return httpx.Response(503, text="private provider details")
    if mode == "malformed":
        return httpx.Response(200, text="private invalid JSON")
    if mode == "throttled":
        return httpx.Response(429, headers={"Retry-After": "60"})
    if request.url.path.endswith("/foods/search"):
        body = json.loads(request.content)
        assert body["dataType"] == ["Foundation", "SR Legacy"]
        assert body["pageSize"] == 20 and body["pageNumber"] == 1
        words = body["query"].replace("+", "").split()
        if mode == "throttle_chicken" and "chicken" in words:
            return httpx.Response(429, headers={"Retry-After": "60"})
        foods = [
            food
            for food in FOODS
            if all(word in str(food["description"]).casefold() for word in words)
        ]
        if mode == "no_food":
            foods = []
        total_hits = len(foods)
        if mode == "broad_search" and words in (["rice"], ["salt"]):
            total_hits = 300
            foods = [
                {
                    **FOODS[0],
                    "fdcId": 4000000 + index,
                    "description": f"{words[0]}, synthetic food {index}",
                    "foodNutrients": [
                        {
                            "nutrientId": 1008,
                            "value": 100,
                            "derivationDescription": "synthetic source metadata " * 600,
                        }
                    ],
                }
                for index in range(body["pageSize"])
            ]
        return httpx.Response(
            200, json={"foods": foods[: body["pageSize"]], "totalHits": total_hits}
        )
    food = next(food for food in FOODS if str(food["fdcId"]) == request.url.path.rsplit("/", 1)[1])
    return httpx.Response(
        200,
        json={
            **food,
            "foodNutrients": [
                {"nutrient": {"id": id, "unitName": unit}, "amount": amount}
                for id, unit, amount in [
                    (1008, "KCAL", 41),
                    (1003, "G", 0.93),
                    (1004, "G", 0.24),
                    (1005, "G", 9.58),
                    (1079, "G", 2.8),
                    (1093, "MG", 69),
                ]
            ],
        },
    )


async def main() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await create_server(
            FoodDataCentral(client, SecretStr("synthetic-private-key"))
        ).run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
