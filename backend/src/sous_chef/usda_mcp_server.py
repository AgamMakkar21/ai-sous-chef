"""Secure stdio entrypoint for the supplied, separately installed USDA dependency."""

import asyncio
import importlib.util
import json
import os
import re
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypedDict, TypeVar, Unpack

_Protocol = TypeVar("_Protocol", bound=asyncio.BaseProtocol)


class _ConnectionOptions(TypedDict, total=False):
    ssl: ssl.SSLContext | bool | None
    family: int
    proto: int
    flags: int
    server_hostname: str | None
    ssl_handshake_timeout: float | None
    ssl_shutdown_timeout: float | None
    happy_eyeballs_delay: float | None
    interleave: int | None
    all_errors: bool


class _DatabaseEventLoop(asyncio.SelectorEventLoop):
    async def create_connection(
        self,
        protocol_factory: Callable[[], _Protocol],
        host: str | None = None,
        port: int | None = None,
        *,
        sock: socket.socket | None = None,
        local_addr: tuple[str, int] | None = None,
        **kwargs: Unpack[_ConnectionOptions],
    ) -> tuple[asyncio.Transport, _Protocol]:
        if sock is not None:
            if host is not None or port is not None or local_addr is not None:
                raise ValueError("A connected socket cannot be combined with address arguments.")
            return await super().create_connection(protocol_factory, sock=sock, **kwargs)
        if host is None or port is None:
            raise ValueError("Database connections require a hostname and port.")
        # A blocked first address must not consume the entire database deadline.
        kwargs.setdefault("happy_eyeballs_delay", 0.25)
        return await super().create_connection(
            protocol_factory, host, port, local_addr=local_addr, **kwargs
        )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set required USDA server configuration: {name}.")
    return value


def _search_text(column: str) -> str:
    expression = f"lower(fd.{column})"
    for separator in ",-/().":
        expression = f"replace({expression}, '{separator}', ' ')"
    return f"(' ' || {expression} || ' ')"


def _configure_search(module: ModuleType) -> None:
    async def search_foods(keyword: str, limit: int = 20) -> str:
        """Search all ingredient terms, ranking raw ingredients before prepared products."""
        words = list(dict.fromkeys(re.findall(r"[^\W_]+", keyword.lower())))
        if (
            not words
            or len(keyword) > 100
            or any(character in keyword for character in "%_\x00\r\n")
            or not 1 <= limit <= 20
        ):
            raise ValueError("Provide a food name and a search limit between 1 and 20.")
        long_text, short_text = _search_text("long_desc"), _search_text("shrt_desc")
        forms = {
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
        patterns = [f"% {word} %" if word in forms else f"%{word}%" for word in words]
        conditions = " AND ".join(
            f"({long_text} LIKE ${index} OR {short_text} LIKE ${index})"
            for index in range(1, len(patterns) + 1)
        )
        # Only placeholder numbers and fixed column expressions enter SQL; terms are bound.
        query = f"""
            SELECT fd.ndb_no, fd.long_desc, fd.shrt_desc, fg.fddrp_desc AS food_group
            FROM food_des fd
            JOIN fd_group fg ON fd.fdgrp_cd = fg.fdgrp_cd
            WHERE {conditions}
            ORDER BY
                CASE WHEN lower(fd.long_desc) = ${len(patterns) + 1} THEN 0 ELSE 1 END,
                CASE
                    WHEN lower(fg.fddrp_desc) IN (
                        'baby foods', 'fast foods', 'restaurant foods',
                        'meals, entrees, and side dishes', 'soups, sauces, and gravies'
                    ) THEN 2
                    WHEN {long_text} LIKE '% raw %' THEN 0
                    ELSE 1
                END,
                fd.long_desc, fd.ndb_no
            LIMIT ${len(patterns) + 2}
        """
        pool = await module.get_db_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(query, *patterns, keyword.strip().lower(), limit)
        return json.dumps([dict(row) for row in rows], default=str)

    module.mcp.remove_tool("search_foods")
    module.mcp.add_tool(search_foods)


def main() -> None:
    source = Path(_required("USDA_MCP_SOURCE"))
    if not source.is_absolute() or not source.is_file():
        raise ValueError("USDA_MCP_SOURCE must be the installed dependency's absolute source path.")
    port = int(os.environ.get("USDA_POSTGRES_PORT", "5432"))
    if not 1 <= port <= 65535:
        raise ValueError("USDA_POSTGRES_PORT must be a valid port.")
    database = {
        "host": _required("USDA_POSTGRES_HOST"),
        "port": port,
        "database": _required("USDA_POSTGRES_DATABASE"),
        "user": _required("USDA_POSTGRES_USER"),
        "password": _required("USDA_POSTGRES_PASSWORD"),
        "ssl": ssl.create_default_context(),
        "timeout": 5,
        "command_timeout": 5,
        "min_size": 1,
        "max_size": 2,
        "server_settings": {"default_transaction_read_only": "on"},
    }
    spec = importlib.util.spec_from_file_location("asc_usda_dependency", source)
    if spec is None or spec.loader is None:
        raise ValueError("The USDA dependency could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep the pinned source unchanged; specialize its search and protected configuration.
    module.__dict__["db_config"] = database
    _configure_search(module)
    with asyncio.Runner(loop_factory=_DatabaseEventLoop) as runner:
        runner.run(module.main())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ImportError):
        raise SystemExit(
            "USDA server setup failed; check dependency and protected configuration."
        ) from None
