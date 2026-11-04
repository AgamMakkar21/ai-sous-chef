"""Bounded stdio client for HemantaPatil/mcp_postgres's two food lookup tools."""

import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from time import monotonic

from pydantic import JsonValue, TypeAdapter, ValidationError

from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.nutrition import UsdaFood, UsdaFoodProfile, UsdaFoodSearch, UsdaNutrientRow
from sous_chef.domain.recipes import RecipeToolError
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.usda_dependency import UsdaSetupError, installed_source

PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 131_072
MAX_SESSION_BYTES = 2_097_152
SEARCH_LIMIT = 20
TOOLS = {
    "search_foods": {"keyword": "string", "limit": "integer"},
    "get_nutrition_profile": {"ndb_no": "string"},
}
_FOODS = TypeAdapter(list[UsdaFood])
_NUTRIENTS = TypeAdapter(list[UsdaNutrientRow])
_JSON = TypeAdapter(JsonValue)


def _invalid() -> RecipeToolError:
    return RecipeToolError(FoundryFailure.INVALID_RESULT)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace(",", " ").split())


@dataclass(frozen=True)
class UsdaMcpConfig:
    python: Path = field(repr=False)
    server: Path = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    timeout_seconds: float = 10

    @classmethod
    def from_settings(cls, settings: Settings) -> "UsdaMcpConfig":
        environment = {
            name: value.get_secret_value() for name, value in settings.usda_mcp_env.items()
        }
        server = settings.usda_mcp_server
        if server is None:
            server = Path(__file__).parents[1] / "usda_mcp_server.py"
            try:
                environment.setdefault("USDA_MCP_SOURCE", str(installed_source()))
            except UsdaSetupError as error:
                raise ConfigurationError(str(error)) from None
            required = {
                "USDA_POSTGRES_HOST",
                "USDA_POSTGRES_DATABASE",
                "USDA_POSTGRES_USER",
                "USDA_POSTGRES_PASSWORD",
            }
            if any(not environment.get(key) for key in required):
                raise ConfigurationError(
                    "Set ASC_USDA_MCP_ENV with USDA_POSTGRES_HOST, USDA_POSTGRES_DATABASE, "
                    "USDA_POSTGRES_USER and USDA_POSTGRES_PASSWORD in protected configuration."
                )
        return cls(
            settings.usda_mcp_python or Path(sys.executable),
            server,
            environment,
            settings.usda_mcp_timeout_seconds,
        )


class UsdaMcpSession:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._sequence = 0
        self._bytes = 0

    async def _send(self, message: dict[str, object]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message, allow_nan=False).encode() + b"\n")
        await self._process.stdin.drain()

    async def _request(self, method: str, params: dict[str, object]) -> dict[str, JsonValue]:
        self._sequence += 1
        await self._send(
            {"jsonrpc": "2.0", "id": self._sequence, "method": method, "params": params}
        )
        assert self._process.stdout is not None
        raw = await self._process.stdout.readline()
        self._bytes += len(raw)
        if not raw:
            raise RecipeToolError(FoundryFailure.UNAVAILABLE)
        if len(raw) > MAX_MESSAGE_BYTES or self._bytes > MAX_SESSION_BYTES:
            raise _invalid()
        payload = _object(_JSON.validate_json(raw))
        if (
            payload.get("jsonrpc") != "2.0"
            or type(payload.get("id")) is not int
            or payload.get("id") != self._sequence
        ):
            raise _invalid()
        if "error" in payload:
            raise RecipeToolError(FoundryFailure.UNAVAILABLE)
        return _object(payload.get("result"))

    async def initialize(self) -> None:
        response = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ai-sous-chef-usda", "version": "1"},
            },
        )
        if response.get("protocolVersion") != PROTOCOL_VERSION:
            raise _invalid()
        capabilities = _object(response.get("capabilities"))
        if "tools" not in capabilities:
            raise _invalid()
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = await self._request("tools/list", {})
        tools = response.get("tools")
        if not isinstance(tools, list) or response.get("nextCursor"):
            raise _invalid()
        found: set[str] = set()
        for value in tools:
            tool = _object(value)
            name = tool.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                continue
            schema = _object(tool.get("inputSchema"))
            properties = _object(schema.get("properties"))
            required = ["keyword"] if name == "search_foods" else ["ndb_no"]
            if (
                name in found
                or schema.get("type") != "object"
                or schema.get("required") != required
                or set(properties) != set(TOOLS[name])
                or any(
                    _object(properties[key]).get("type") != kind
                    for key, kind in TOOLS[name].items()
                )
            ):
                raise _invalid()
            found.add(name)
        if found != set(TOOLS):
            raise _invalid()

    async def _call(self, name: str, arguments: dict[str, object]) -> str:
        # No model/user-provided tool name or SQL is ever forwarded.
        if name not in TOOLS or set(arguments) != set(TOOLS[name]):
            raise RecipeToolError(FoundryFailure.REJECTED)
        response = await self._request("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError", False) is not False:
            raise RecipeToolError(FoundryFailure.UNAVAILABLE)
        content = response.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise _invalid()
        text = _object(content[0])
        value = text.get("text")
        if text.get("type") != "text" or not isinstance(value, str):
            raise _invalid()
        if value.startswith("Error:"):
            raise RecipeToolError(FoundryFailure.UNAVAILABLE)
        return value

    async def search(self, name: str) -> UsdaFoodSearch:
        if not name.strip() or len(name) > 100 or any(c in name for c in "%_\x00\r\n"):
            raise RecipeToolError(FoundryFailure.REJECTED)
        foods = _FOODS.validate_json(
            await self._call("search_foods", {"keyword": name, "limit": SEARCH_LIMIT})
        )
        if len(foods) > SEARCH_LIMIT or len({food.ndb_no for food in foods}) != len(foods):
            raise _invalid()
        return UsdaFoodSearch(foods=tuple(foods), truncated=len(foods) == SEARCH_LIMIT)

    async def food(self, name: str, *, food_id: str | None = None) -> UsdaFoodProfile:
        search = await self.search(name)
        if food_id is not None:
            if not re.fullmatch(r"[0-9]{5}", food_id):
                raise RecipeToolError(FoundryFailure.REJECTED)
            matches = [food for food in search.foods if food.ndb_no == food_id]
        else:
            if search.truncated:
                raise RecipeToolError(FoundryFailure.NO_RESULTS)
            words = set(re.findall(r"[^\W_]+", name.casefold()))
            matches = [
                food
                for food in search.foods
                if _normalize(name) in {_normalize(food.long_desc), _normalize(food.shrt_desc)}
                or words == set(re.findall(r"[^\W_]+", food.long_desc.casefold()))
                or words == set(re.findall(r"[^\W_]+", food.shrt_desc.casefold()))
            ]
        if len(matches) != 1:
            raise RecipeToolError(FoundryFailure.NO_RESULTS)
        food = matches[0]
        return await self.profile(food)

    async def profile(self, food: UsdaFood) -> UsdaFoodProfile:
        """Fetch only an identified search candidate, validating source descriptions."""
        raw = await self._call("get_nutrition_profile", {"ndb_no": food.ndb_no})
        data = _JSON.validate_json(raw)
        if data == {"error": f"No food found with NDB number: {food.ndb_no}"}:
            raise RecipeToolError(FoundryFailure.NO_RESULTS)
        rows = _NUTRIENTS.validate_json(raw)
        if (
            not rows
            or len(rows) > 256
            or any(
                (row.long_desc, row.shrt_desc, row.food_group)
                != (food.long_desc, food.shrt_desc, food.food_group)
                for row in rows
            )
            or len({(row.nutrdesc, row.units) for row in rows}) != len(rows)
        ):
            raise _invalid()
        return UsdaFoodProfile(food=food, nutrients=tuple(rows))


@asynccontextmanager
async def open_usda(config: UsdaMcpConfig, *, deadline: float) -> AsyncIterator[UsdaMcpSession]:
    if not isfinite(deadline):
        raise ValueError("The USDA deadline must be finite.")
    process: asyncio.subprocess.Process | None = None
    expires_at = min(deadline, monotonic() + config.timeout_seconds)
    try:
        async with asyncio.timeout_at(expires_at):
            if monotonic() >= deadline:
                raise TimeoutError
            # Do not forward profile, app credentials or telemetry settings to the server.
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG"}
            }
            environment.update(config.environment)
            process = await asyncio.create_subprocess_exec(
                str(config.python),
                "-u",
                str(config.server),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                limit=MAX_MESSAGE_BYTES,
            )
            session = UsdaMcpSession(process)
            await session.initialize()
            yield session
            if monotonic() >= expires_at:
                raise TimeoutError
    except TimeoutError:
        raise RecipeToolError(FoundryFailure.TIMEOUT) from None
    except (ValidationError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    except OSError:
        raise RecipeToolError(FoundryFailure.UNAVAILABLE) from None
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
