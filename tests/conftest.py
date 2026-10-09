from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sous_chef.app import create_app
from sous_chef.settings import Settings


@pytest.fixture
def local_settings() -> Settings:
    return Settings(environment="local", adapter_mode="local", _env_file=None)


@pytest.fixture
def client(local_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(local_settings)) as test_client:
        yield test_client
