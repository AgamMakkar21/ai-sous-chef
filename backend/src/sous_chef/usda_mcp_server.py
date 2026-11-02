"""Secure stdio entrypoint for the supplied, separately installed USDA dependency."""

import asyncio
import importlib.util
import os
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
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
    # Keep the dependency's tools and cleanup, replacing only its insecure sample config.
    module.__dict__["db_config"] = database
    with asyncio.Runner(loop_factory=_DatabaseEventLoop) as runner:
        runner.run(module.main())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ImportError):
        raise SystemExit(
            "USDA server setup failed; check dependency and protected configuration."
        ) from None
