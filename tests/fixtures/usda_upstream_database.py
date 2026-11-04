"""Use the real pinned server/MCP SDK with synthetic database rows, never Azure."""

import asyncio
import importlib.util
import os
import sqlite3
from typing import Self

from sous_chef.usda_mcp_server import _configure_search, _DatabaseEventLoop

database = sqlite3.connect(":memory:")
database.row_factory = sqlite3.Row
database.executescript("""
    CREATE TABLE fd_group (fdgrp_cd TEXT PRIMARY KEY, fddrp_desc TEXT NOT NULL);
    CREATE TABLE food_des (
        ndb_no TEXT PRIMARY KEY, long_desc TEXT NOT NULL,
        shrt_desc TEXT NOT NULL, fdgrp_cd TEXT NOT NULL
    );
    INSERT INTO fd_group VALUES
        ('fixture', 'Fixture'), ('poultry', 'Poultry Products'),
        ('baby', 'Baby Foods'), ('restaurant', 'Restaurant Foods');
""")
database.executemany(
    "INSERT INTO food_des VALUES (?, ?, ?, ?)",
    [
        ("99904", "Carrots, raw", "CARROTS,RAW", "fixture"),
        (
            "99910",
            "Chicken, broilers or fryers, breast, meat only, raw",
            "CHICKEN,BREAST,MEAT ONLY,RAW",
            "poultry",
        ),
        (
            "99911",
            "Chicken, broilers or fryers, thigh, meat only, raw",
            "CHICKEN,THIGH,MEAT ONLY,RAW",
            "poultry",
        ),
        (
            "99912",
            "Chicken, broilers or fryers, breast, meat only, roasted",
            "CHICKEN,BREAST,ROASTED",
            "poultry",
        ),
        (
            "99913",
            "Chicken, broilers or fryers, breast, meat and skin, raw",
            "CHICKEN,BREAST,MEAT AND SKIN,RAW",
            "poultry",
        ),
        ("99914", "Applebee's, chicken breast, roasted", "CHICKEN BREAST ROASTED", "restaurant"),
        ("99915", "Babyfood, chicken and strawberry", "CHICKEN STRAWBERRY", "baby"),
        *[
            (f"{91000 + index}", f"Babyfood, chicken dinner {index}", "CHICKEN DINNER", "baby")
            for index in range(30)
        ],
    ],
)


class FixtureConnection:
    async def fetch(self, query: str, *arguments: object) -> list[dict[str, object]]:
        if "LIMIT" in query:
            # Execute the launcher's actual filtering/order/limit SQL, not a ranked fake.
            return [dict(row) for row in database.execute(query, arguments)]
        food = database.execute(
            """
            SELECT fd.long_desc, fd.shrt_desc, fg.fddrp_desc AS food_group
            FROM food_des fd JOIN fd_group fg ON fd.fdgrp_cd = fg.fdgrp_cd
            WHERE fd.ndb_no = ?
        """,
            arguments,
        ).fetchone()
        assert food is not None
        return [
            {
                **dict(food),
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
_configure_search(module)
with asyncio.Runner(loop_factory=_DatabaseEventLoop) as runner:
    runner.run(module.main())
