from importlib.metadata import version

from azure.ai.projects import AIProjectClient


def test_pinned_new_foundry_sdk_baseline_imports_offline() -> None:
    assert version("azure-ai-projects") == "2.8.0"
    assert version("openai") == "3.27.0"
    assert callable(AIProjectClient.get_openai_client)
