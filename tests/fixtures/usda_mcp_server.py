"""Synthetic stdio contract fixture, NOT a USDA data source or runtime fallback."""

import json
import os
import sys
import time
from pathlib import Path
from typing import TypedDict


class ToolSchema(TypedDict):
    type: str
    required: list[str]
    properties: dict[str, dict[str, str | int]]


class ToolDefinition(TypedDict):
    name: str
    inputSchema: ToolSchema


MODE = os.environ.get("TEST_USDA_MODE", "")
FOODS = [
    {"fdc_id": "99901", "long_desc": "rice", "shrt_desc": "RICE", "food_group": "Fixture"},
    {"fdc_id": "99902", "long_desc": "carrot", "shrt_desc": "CARROT", "food_group": "Fixture"},
    {"fdc_id": "99903", "long_desc": "olive oil", "shrt_desc": "OIL", "food_group": "Fixture"},
    {
        "fdc_id": "99904",
        "long_desc": "Carrots, raw",
        "shrt_desc": "CARROTS,RAW",
        "food_group": "Fixture",
    },
    {
        "fdc_id": "99905",
        "long_desc": "chicken breast",
        "shrt_desc": "CHICKEN BREAST",
        "food_group": "Fixture",
    },
]
if MODE == "leading_zero":
    FOODS[0]["fdc_id"] = "00001"
NUTRIENTS = [
    ("Energy", "kcal", "200"),
    ("Protein", "g", "4"),
    ("Carbohydrate, by difference", "g", "30"),
    ("Total lipid (fat)", "g", "2"),
    ("Fiber, total dietary", "g", "1"),
    ("Sodium, Na", "mg", "10"),
]
TOOLS: list[ToolDefinition] = [
    {
        "name": "search_foods",
        "inputSchema": {
            "type": "object",
            "required": ["keyword"],
            "properties": {
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "get_nutrition_profile",
        "inputSchema": {
            "type": "object",
            "required": ["fdc_id"],
            "properties": {"fdc_id": {"type": "string"}},
        },
    },
    {
        "name": "execute_query",
        "inputSchema": {"type": "object", "required": [], "properties": {}},
    },
]


def tool_output(request: dict[str, object]) -> str:
    params = request["params"]
    assert isinstance(params, dict)
    arguments = params["arguments"]
    if MODE == "timeout":
        time.sleep(60)
    if MODE == "slow_foods":
        time.sleep(0.03)
    if MODE == "hang_rice" and params["name"] == "search_foods" and arguments["keyword"] == "rice":
        time.sleep(60)
    if MODE == "error":
        return "Error: private database credential"
    if (
        MODE == "fail_carrot"
        and params["name"] == "search_foods"
        and arguments["keyword"] == "carrot"
    ):
        return json.dumps({"error": "unavailable"})
    if MODE == "malformed":
        return "private invalid JSON"
    if MODE == "oversized":
        return "x" * 140_000
    if MODE == "no_food":
        return json.dumps({"foods": [], "truncated": False})
    if MODE == "throttled":
        return json.dumps({"error": "throttled"})
    if params["name"] == "search_foods":
        assert set(arguments) == {"keyword", "limit"} and arguments["limit"] == 20
        foods = [food for food in FOODS if food["long_desc"] == arguments["keyword"]]
        if MODE in {"candidates", "reordered"}:
            foods = [
                {
                    "fdc_id": "99901",
                    "long_desc": "Chicken, breast, meat only, raw",
                    "shrt_desc": "CHICKEN BREAST RAW",
                    "food_group": "Fixture",
                },
                {
                    "fdc_id": "99902",
                    "long_desc": "Chicken, thigh, meat and skin, roasted",
                    "shrt_desc": "CHICKEN THIGH ROASTED",
                    "food_group": "Fixture",
                },
            ]
            if MODE == "reordered":
                foods = [dict(FOODS[0], long_desc="Rice, raw", shrt_desc="RICE,RAW")]
        if MODE == "ambiguous" and foods:
            foods.append(dict(foods[0], fdc_id="99909"))
        if MODE == "mismatch":
            foods = [FOODS[0]]
        if MODE == "saturated":
            foods = [dict(FOODS[0], fdc_id=str(100000 + index)) for index in range(20)]
        return json.dumps(
            {
                "foods": [dict(food, data_type="SR Legacy") for food in foods],
                "truncated": len(foods) == 20,
            }
        )
    assert params["name"] == "get_nutrition_profile" and set(arguments) == {"fdc_id"}
    food = next(food for food in FOODS if food["fdc_id"] == arguments["fdc_id"])
    if MODE == "candidates":
        food = dict(
            food,
            long_desc="Chicken, breast, meat only, raw"
            if food["fdc_id"] == "99901"
            else "Chicken, thigh, meat and skin, roasted",
            shrt_desc="CHICKEN BREAST RAW"
            if food["fdc_id"] == "99901"
            else "CHICKEN THIGH ROASTED",
            food_group="Fixture",
        )
    if MODE == "reordered":
        food = dict(food, long_desc="Rice, raw", shrt_desc="RICE,RAW")
    if MODE == "missing_profile":
        return json.dumps({"error": "no_results"})
    nutrients = NUTRIENTS
    if MODE in {"varied", "mixed_nutrients"}:
        amounts = {
            "99902": ("40", "1", "9", "0.2", "3", "60"),
            "99903": ("900", "0", "0", "100", "0", "0"),
        }.get(food["fdc_id"])
        if amounts is not None:
            nutrients = [
                (name, unit, amount)
                for (name, unit, _), amount in zip(NUTRIENTS, amounts, strict=True)
            ]
    rows = [
        {
            **{key: value for key, value in food.items() if key != "fdc_id"},
            "nutrdesc": name,
            "units": unit,
            "nutr_val": amount,
            "sr_order": index,
        }
        for index, (name, unit, amount) in enumerate(nutrients)
        if not (MODE == "missing_nutrient" and name == "Fiber, total dietary")
        and not (
            MODE == "mixed_nutrients"
            and (food["fdc_id"], name) in {("99901", "Protein"), ("99902", "Fiber, total dietary")}
        )
    ]
    if MODE == "wrong_food":
        rows[0]["long_desc"] = "different food"
    elif MODE == "wrong_unit":
        rows[1]["units"] = "mg"
    elif MODE == "nan":
        rows[0]["nutr_val"] = "NaN"
    elif MODE == "negative":
        rows[0]["nutr_val"] = "-1"
    elif MODE == "duplicate":
        rows.append(rows[0])
    elif MODE == "zero_nutrient":
        rows[4]["nutr_val"] = "0"
    elif MODE == "kj_only":
        rows[0]["units"] = "kJ"
    elif MODE in {"rows_256", "rows_257"}:
        count = int(MODE.removeprefix("rows_"))
        rows.extend(
            dict(rows[0], nutrdesc=f"Other fixture nutrient {index}", sr_order=index)
            for index in range(len(rows), count)
        )
    return json.dumps({"food": dict(food, data_type="SR Legacy"), "nutrients": rows})


for line in sys.stdin:
    request = json.loads(line)
    if log := os.environ.get("TEST_USDA_LOG"):
        with Path(log).open("a", encoding="utf-8") as file:
            file.write(json.dumps(request) + "\n")
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        if MODE == "slow_initialize":
            time.sleep(0.75)
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-usda", "version": "1"},
        }
    elif method == "tools/list":
        if MODE == "schema":
            TOOLS[0]["inputSchema"]["properties"]["keyword"]["type"] = "integer"
        result = {"tools": TOOLS}
    else:
        assert method == "tools/call"
        result = {
            "content": [{"type": "text", "text": tool_output(request)}],
            "isError": MODE == "is_error",
        }
    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    if MODE in {"at_limit", "over_limit"}:
        result["padding"] = ""
        length = 131_072 if MODE == "at_limit" else 131_073
        result["padding"] = "x" * (length - len(json.dumps(response).encode()) - 1)
    sys.stdout.buffer.write(json.dumps(response).encode() + b"\n")
    sys.stdout.buffer.flush()
