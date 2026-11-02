import asyncio
import hashlib
import io
import socket
from pathlib import Path
from urllib.error import URLError

import pytest
from sous_chef import usda_dependency
from sous_chef.adapters.usda_mcp import UsdaMcpConfig
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.usda_mcp_server import _DatabaseEventLoop


def test_database_connection_tries_another_address_without_waiting_for_a_blocked_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original_connect = loop.sock_connect
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        async def resolve(
            *args: object, **kwargs: object
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port))
                for host in ("127.0.0.2", "127.0.0.1")
            ]

        async def connect(sock: socket.socket, address: tuple[str, int]) -> None:
            if address[0] == "127.0.0.2":
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                await original_connect(sock, address)

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(loop, "sock_connect", connect)
        try:
            transport, _ = await asyncio.wait_for(
                loop.create_connection(asyncio.Protocol, "synthetic.invalid", port), timeout=2
            )
            transport.close()
            assert blocked.is_set()
            assert cancelled.is_set()
        finally:
            server.close()
            await server.wait_closed()

    with asyncio.Runner(loop_factory=_DatabaseEventLoop) as runner:
        runner.run(check())


def test_setup_installs_once_and_rejects_modified_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = b"# synthetic pinned source\n"
    target = tmp_path / "_usda" / "main.py"
    monkeypatch.setattr(usda_dependency, "SOURCE_PATH", target)
    monkeypatch.setattr(usda_dependency, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    calls: list[str] = []

    def download(url: str, *, timeout: int) -> io.BytesIO:
        assert timeout == 30
        calls.append(url)
        return io.BytesIO(source)

    monkeypatch.setattr(usda_dependency, "urlopen", download)
    usda_dependency.install()
    assert usda_dependency.installed_source() == target
    usda_dependency.install()
    assert calls == [usda_dependency.SOURCE_URL]
    target.write_bytes(b"modified")
    with pytest.raises(usda_dependency.UsdaSetupError, match="checksum"):
        usda_dependency.install()
    assert calls == [usda_dependency.SOURCE_URL]


def test_bad_download_never_becomes_executable_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "_usda" / "main.py"
    monkeypatch.setattr(usda_dependency, "SOURCE_PATH", target)
    monkeypatch.setattr(usda_dependency, "urlopen", lambda *args, **kwargs: io.BytesIO(b"bad"))
    with pytest.raises(usda_dependency.UsdaSetupError, match="checksum"):
        usda_dependency.install()
    assert not target.exists()


def test_missing_source_has_setup_guidance_not_runtime_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(usda_dependency, "SOURCE_PATH", tmp_path / "missing.py")
    with pytest.raises(ConfigurationError, match="python -m sous_chef.usda_dependency"):
        UsdaMcpConfig.from_settings(
            Settings(
                environment="local",
                adapter_mode="local",
                _env_file=None,
            )
        )


def test_network_failure_is_safe_and_does_not_create_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise URLError("private network configuration")

    target = tmp_path / "_usda" / "main.py"
    monkeypatch.setattr(usda_dependency, "SOURCE_PATH", target)
    monkeypatch.setattr(usda_dependency, "urlopen", unavailable)
    with pytest.raises(usda_dependency.UsdaSetupError) as caught:
        usda_dependency.install()
    assert "private" not in str(caught.value) and not target.exists()


def test_default_config_uses_this_apps_interpreter_launcher_and_pinned_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sys

    source = b"# synthetic pinned source\n"
    target = tmp_path / "main.py"
    target.write_bytes(source)
    monkeypatch.setattr(usda_dependency, "SOURCE_PATH", target)
    monkeypatch.setattr(usda_dependency, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    settings = Settings(
        environment="local",
        adapter_mode="local",
        _env_file=None,
        usda_mcp_env={
            "USDA_POSTGRES_HOST": "synthetic.invalid",
            "USDA_POSTGRES_DATABASE": "usda",
            "USDA_POSTGRES_USER": "readonly",
            "USDA_POSTGRES_PASSWORD": "private",
        },
    )
    config = UsdaMcpConfig.from_settings(settings)
    assert config.python == Path(sys.executable)
    assert config.server.name == "usda_mcp_server.py"
    assert config.environment["USDA_MCP_SOURCE"] == str(target)
    assert "private" not in repr(config)
    with pytest.raises(ConfigurationError, match="USDA_POSTGRES_HOST") as caught:
        UsdaMcpConfig.from_settings(
            Settings(
                environment="local",
                adapter_mode="local",
                _env_file=None,
            )
        )
    assert "private" not in str(caught.value)
