"""Synthetic supplied-module shape for testing the secure stdio launcher."""

import os
import runpy
import ssl
from pathlib import Path

from mcp.server.fastmcp import FastMCP

db_config: dict[str, object] = {"password": "insecure sample"}
mcp = FastMCP("synthetic-dependency")


@mcp.tool()
async def search_foods(keyword: str, limit: int = 20) -> str:
    raise AssertionError("The launcher's search must replace the sample tool.")


async def main() -> None:
    tls = db_config["ssl"]
    assert isinstance(tls, ssl.SSLContext)
    assert tls.check_hostname
    assert tls.verify_mode == ssl.CERT_REQUIRED
    assert tls.cert_store_stats()["x509_ca"] > 0
    assert db_config == {
        "host": "synthetic.invalid",
        "port": 5432,
        "database": "usda",
        "user": "readonly",
        "password": os.environ["USDA_POSTGRES_PASSWORD"],
        "ssl": tls,
        "timeout": 5,
        "command_timeout": 5,
        "min_size": 1,
        "max_size": 2,
        "server_settings": {"default_transaction_read_only": "on"},
    }
    assert "ASC_PAGINATION_KEY" not in os.environ
    runpy.run_path(str(Path(__file__).with_name("usda_mcp_server.py")), run_name="__main__")
