"""Use the real pinned server/MCP SDK with synthetic database rows, never Azure."""

import asyncio
import importlib.util
import os
from typing import Self

from sous_chef.usda_mcp_server import _DatabaseEventLoop


class FixtureConnection:
    async def fetch(self, query: str, *arguments: object) -> list[dict[str, object]]:
        if "ILIKE" in query:
            return [
                {
                    "ndb_no": "99904",
                    "long_desc": "Carrots, raw",
                    "shrt_desc": "CARROTS,RAW",
                    "food_group": "Fixture",
                }
            ]
        assert arguments == ("99904",)
        return [
            {
                "long_desc": "Carrots, raw",
                "shrt_desc": "CARROTS,RAW",
                "food_group": "Fixture",
                "nutrdesc": name,
                "units": unit,
                "nutr_val": amount,
                "sr_order": index,
            }
            for index, (name, unit, amount) in enumerate(
                [
                    ("Energy", "kcal", 200),
                    ("Protein", "g", 4),
                    ("Total lipid (fat)", "g", 2),
                    ("Carbohydrate, by difference", "g", 30),
                    ("Fiber, total dietary", "g", 1),
                    ("Sodium, Na", "mg", 10),
                ]
            )
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FixturePool:
    def acquire(self) -> FixtureConnection:
        return FixtureConnection()

    async def close(self) -> None:
        return None


spec = importlib.util.spec_from_file_location("usda_upstream", os.environ["TEST_USDA_SOURCE"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.__dict__["db_pool"] = FixturePool()
with asyncio.Runner(loop_factory=_DatabaseEventLoop) as runner:
    runner.run(module.main())
