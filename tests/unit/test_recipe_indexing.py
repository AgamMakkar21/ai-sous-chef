import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from sous_chef import index_recipes
from sous_chef.adapters.foundry_iq import (
    FoundryRecipeKnowledge,
    RecipeCitation,
    RecipeKnowledgeConfig,
)
from sous_chef.domain.recipe_corpus import CorpusRecipe, normalize_row
from sous_chef.services.recipe_indexing import (
    MAX_CORPUS_BYTES,
    MAX_DOCUMENT_BYTES,
    RecipeIndexingError,
    load_corpus,
    search_document,
)
from sous_chef.settings import ConfigurationError, Settings, load_settings

REVISION = "a" * 40
PROJECT_ID = (
    "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
    "/providers/Microsoft.CognitiveServices/accounts/synthetic/projects/recipes"
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.upper().startswith("ASC_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def recipe_fixture(number: int = 1, *, name: str = "Synthetic Rice") -> CorpusRecipe:
    recipe = normalize_row(
        {
            "RecipeId": str(number),
            "Name": name,
            "AuthorName": "Synthetic Fixture Author",
            "RecipeIngredientParts": 'c("rice", "water")',
            "RecipeIngredientQuantities": 'c("1 cup", "2 cups")',
            "RecipeInstructions": 'c("Combine the fixture ingredients.", "Cook until tender.")',
        },
        number,
        REVISION,
    )
    assert isinstance(recipe, CorpusRecipe)
    return recipe


def knowledge_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "local",
        "adapter_mode": "local",
        "foundry_project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/recipes",
        "foundry_project_resource_id": PROJECT_ID,
        "foundry_iq_search_endpoint": "https://synthetic.search.windows.net",
        "foundry_iq_index_name": "fixture-recipes",
        "foundry_iq_knowledge_source_name": "fixture-source",
        "foundry_iq_knowledge_base_name": "fixture-knowledge",
        "foundry_iq_connection_name": "fixture-mcp",
    }
    return Settings(**(values | overrides))


def test_search_document_preserves_complete_recipe_and_versioned_provenance() -> None:
    recipe = recipe_fixture()
    document = search_document(recipe)
    assert document["source_id"] == recipe.source_id
    assert document["title"] == recipe.name
    assert "1 cup rice" in document["content"]
    assert recipe.instructions[0] in document["content"]
    assert document["source_url"] == (
        f"https://huggingface.co/datasets/untitledwebsite123/food-recipes/blob/{REVISION}/recipes.csv"
    )
    assert CorpusRecipe.model_validate_json(document["recipe_json"]) == recipe
    assert json.loads(document["recipe_json"])["provenance"]["license"] == "unspecified"


def test_load_corpus_consumes_asc28_jsonl_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "recipes.jsonl"
    recipes = [recipe_fixture(), recipe_fixture(2, name="Other Synthetic Rice")]
    path.write_text(
        "".join(recipe.model_dump_json() + "\n" for recipe in recipes), encoding="utf-8"
    )
    assert load_corpus(path) == load_corpus(path) == [search_document(recipe) for recipe in recipes]


@pytest.mark.parametrize("content", ["", "\n", "not JSON", "{}", '{"name":"private-input"}'])
def test_bad_corpus_is_rejected_without_echoing_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "recipes.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RecipeIndexingError) as error:
        load_corpus(path)
    if content.strip():
        assert content not in str(error.value)


def test_missing_corpus_reports_an_actionable_error_without_the_path(tmp_path: Path) -> None:
    with pytest.raises(RecipeIndexingError, match="--corpus") as error:
        load_corpus(tmp_path / "private-path.jsonl")
    assert "private-path" not in str(error.value)


@pytest.mark.parametrize("same_id", [True, False])
def test_duplicate_ids_and_content_are_rejected_before_indexing(
    tmp_path: Path, same_id: bool
) -> None:
    first = recipe_fixture()
    second = recipe_fixture(1 if same_id else 2)
    path = tmp_path / "recipes.jsonl"
    path.write_text(
        first.model_dump_json() + "\n" + second.model_dump_json() + "\n", encoding="utf-8"
    )
    with pytest.raises(RecipeIndexingError, match="line 2 repeats"):
        load_corpus(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset", "private-dataset"),
        ("source_file", "../private-file"),
        ("revision", "main"),
        ("source_recipe_id", "not-a-number"),
        ("source_recipe_id", "1" * 5000),
        ("source_recipe_id", "42"),
        ("row_number", 0),
    ],
)
def test_invalid_provenance_never_becomes_a_source_reference(field: str, value: object) -> None:
    recipe = recipe_fixture()
    recipe = recipe.model_copy(
        update={"provenance": recipe.provenance.model_copy(update={field: value})}
    )
    with pytest.raises(RecipeIndexingError, match="provenance"):
        search_document(recipe)


def test_input_and_indexed_document_sizes_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "large.jsonl"
    path.write_bytes(b"x" * (MAX_CORPUS_BYTES + 1))
    with pytest.raises(RecipeIndexingError, match="16 MiB"):
        load_corpus(path)
    path.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(RecipeIndexingError, match="line 1 exceeds"):
        load_corpus(path)
    recipe = recipe_fixture().model_copy(update={"instructions": ["x" * MAX_DOCUMENT_BYTES]})
    with pytest.raises(RecipeIndexingError, match="document limit"):
        search_document(recipe)


def test_large_slices_and_empty_recipe_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "recipes.jsonl"
    path.write_bytes(b"{}\n" * 1001)
    with pytest.raises(RecipeIndexingError, match="between 1 and 1000"):
        load_corpus(path)
    with pytest.raises(RecipeIndexingError, match="fields are empty"):
        search_document(recipe_fixture().model_copy(update={"ingredients": []}))


def test_document_limit_counts_escaped_unicode_on_the_wire() -> None:
    recipe = recipe_fixture().model_copy(update={"instructions": ["\U0001f35a" * 12000]})
    assert len(recipe.model_dump_json().encode("utf-8")) < MAX_DOCUMENT_BYTES
    with pytest.raises(RecipeIndexingError, match="document limit"):
        search_document(recipe)


def test_recipe_knowledge_settings_are_optional_until_the_command_is_used() -> None:
    settings = Settings(environment="local", adapter_mode="local")
    with pytest.raises(ConfigurationError, match="ASC_FOUNDRY_IQ_SEARCH_ENDPOINT"):
        RecipeKnowledgeConfig.from_settings(settings)
    config = RecipeKnowledgeConfig.from_settings(knowledge_settings())
    assert config.project_resource_id == PROJECT_ID
    assert config.mcp_endpoint.startswith(
        "https://synthetic.search.windows.net/knowledgebases/fixture-knowledge/mcp?"
    )


def test_connection_resource_id_must_match_the_configured_new_foundry_project() -> None:
    settings = knowledge_settings(
        foundry_project_endpoint="https://other.services.ai.azure.com/api/projects/recipes"
    )
    with pytest.raises(ConfigurationError, match="must identify"):
        RecipeKnowledgeConfig.from_settings(settings)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://synthetic.search.windows.net",
        "https://synthetic.search.windows.net/private",
        "https://synthetic.search.windows.net?api-key=private-value",
        "https://synthetic.search.windows.net#private-value",
        "https://synthetic.search.windows.net:8443",
        "https://synthetic.search.windows.net.example.invalid",
        "https://private-value@synthetic.search.windows.net",
    ],
)
def test_search_tokens_can_only_target_an_https_search_service_origin(endpoint: str) -> None:
    with pytest.raises(ConfigurationError, match="ASC_FOUNDRY_IQ_SEARCH_ENDPOINT") as error:
        knowledge_settings(foundry_iq_search_endpoint=endpoint)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("FOUNDRY_IQ_INDEX_NAME", "bad/private-name"),
        ("FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME", "bad--private-name"),
        ("FOUNDRY_IQ_KNOWLEDGE_BASE_NAME", "private-name-"),
        ("FOUNDRY_IQ_CONNECTION_NAME", "x" * 34),
        ("FOUNDRY_PROJECT_RESOURCE_ID", "/private-id"),
    ],
)
def test_invalid_knowledge_configuration_is_reported_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setting: str, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASC_ENVIRONMENT", "local")
    monkeypatch.setenv("ASC_ADAPTER_MODE", "local")
    monkeypatch.setenv(f"ASC_{setting}", value)
    with pytest.raises(ConfigurationError, match=f"ASC_{setting}") as error:
        load_settings()
    assert value not in str(error.value)


@pytest.fixture
def cli_client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(spec=FoundryRecipeKnowledge)
    monkeypatch.setattr(index_recipes, "load_settings", knowledge_settings)
    monkeypatch.setattr(index_recipes, "build_credential", lambda settings: Mock())
    monkeypatch.setattr(index_recipes, "FoundryRecipeKnowledge", lambda config, credential: client)
    return client


@pytest.mark.parametrize("configure", [False, True])
def test_cli_refresh_only_configures_when_explicitly_requested(
    cli_client: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str], configure: bool
) -> None:
    recipe = recipe_fixture()
    path = tmp_path / "recipes.jsonl"
    path.write_text(recipe.model_dump_json() + "\n", encoding="utf-8")
    cli_client.index.return_value = 1
    arguments = ["refresh", "--corpus", str(path)] + (["--configure"] if configure else [])
    assert index_recipes.main(arguments) == 0
    assert cli_client.configure.call_count == int(configure)
    cli_client.index.assert_called_once_with([search_document(recipe)])
    cli_client.close.assert_called_once()
    assert "Indexed 1 recipes" in capsys.readouterr().out


def test_cli_validates_the_complete_corpus_before_configuring_or_indexing(
    cli_client: Mock, tmp_path: Path
) -> None:
    path = tmp_path / "recipes.jsonl"
    path.write_text(recipe_fixture().model_dump_json() + "\nnot-json\n", encoding="utf-8")
    assert index_recipes.main(["refresh", "--configure", "--corpus", str(path)]) == 1
    cli_client.configure.assert_not_called()
    cli_client.index.assert_not_called()


def test_cli_query_is_read_only_and_prints_only_source_metadata(
    cli_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = recipe_fixture()
    cli_client.retrieve.return_value = [
        RecipeCitation("0", search_document(recipe)["source_url"], recipe)
    ]
    assert index_recipes.main(["query", "--query", "rice"]) == 0
    output = capsys.readouterr()
    assert recipe.source_id in output.out
    assert recipe.instructions[0] not in output.out
    assert recipe.name not in output.out
    assert output.err == ""
    cli_client.configure.assert_not_called()
    cli_client.index.assert_not_called()
    cli_client.close.assert_called_once()


def test_cli_zero_results_and_real_query_errors_exit_nonzero(
    cli_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_client.retrieve.return_value = []
    assert index_recipes.main(["query", "--query", "rice"]) == 1
    assert "no recipes" in capsys.readouterr().err
    cli_client.retrieve.side_effect = RecipeIndexingError("Synthetic HTTP 403 failure")
    assert index_recipes.main(["query", "--query", "rice"]) == 1
    assert "HTTP 403" in capsys.readouterr().err
