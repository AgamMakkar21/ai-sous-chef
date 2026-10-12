import csv
import io
import json
from pathlib import Path

import pytest
from sous_chef.domain.recipe_corpus import (
    CorpusRecipe,
    Rejection,
    RejectionReason,
    normalize_row,
    parse_r_vector,
)
from sous_chef.ingest_recipes import main
from sous_chef.services.recipe_ingestion import (
    SliceTooSmallError,
    build_corpus,
    read_rows,
    write_corpus,
)

REVISION = "a" * 40
COLUMNS = [
    "RecipeId",
    "Name",
    "AuthorName",
    "CookTime",
    "PrepTime",
    "TotalTime",
    "RecipeCategory",
    "Keywords",
    "RecipeIngredientQuantities",
    "RecipeIngredientParts",
    "RecipeServings",
    "RecipeInstructions",
]


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "RecipeId": "1",
        "Name": "  Test   Toast ",
        "AuthorName": "Fixture Author",
        "CookTime": "PT5M",
        "PrepTime": "PT1M",
        "TotalTime": "PT6M",
        "RecipeCategory": "Breakfast",
        "Keywords": 'c("Easy", "Quick")',
        "RecipeIngredientQuantities": 'c("2", NA)',
        "RecipeIngredientParts": 'c("bread", "salt")',
        "RecipeServings": "2",
        "RecipeInstructions": 'c("Toast the bread.", "Add a \\"pinch\\" of salt.")',
    }
    return {**base, **overrides}


def _csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


FIXTURE_ROWS = [
    _row(RecipeId="1"),
    _row(
        RecipeId="2",
        Name="Plain Rice",
        RecipeIngredientParts='c("rice")',
        RecipeIngredientQuantities='c("1")',
        RecipeInstructions='c("Boil the rice.")',
    ),
    _row(RecipeId="1", Name="Other Name"),  # repeated source id
    _row(RecipeId="3", Name="test toast"),  # same content as recipe 1 under another id
    _row(RecipeId="4", Name=""),
    _row(RecipeId="5", RecipeIngredientParts="NA", RecipeIngredientQuantities="NA"),
    _row(RecipeId="6", RecipeInstructions="NA"),
    _row(RecipeId="7", Name="Mismatch Dish", RecipeIngredientQuantities='c("1")'),
    _row(RecipeId="abc"),
    _row(RecipeId="8", RecipeIngredientParts='c("bread", oops)'),
]


def test_parse_r_vector_handles_na_escapes_and_plain_text() -> None:
    assert parse_r_vector('c("a", NA, "b \\"x\\"")') == ["a", "", 'b "x"']
    assert parse_r_vector("NA") == []
    assert parse_r_vector("") == []
    assert parse_r_vector("single value") == ["single value"]
    assert parse_r_vector('c("a", oops)') is None


def test_normalize_row_builds_clean_record_with_provenance() -> None:
    recipe = normalize_row(_row(), 1, REVISION)
    assert isinstance(recipe, CorpusRecipe)
    assert recipe.source_id == "hf-food-recipes-1"
    assert recipe.name == "Test Toast"
    assert [(i.quantity, i.name) for i in recipe.ingredients] == [("2", "bread"), ("", "salt")]
    assert recipe.instructions[1] == 'Add a "pinch" of salt.'
    assert recipe.servings == 2
    provenance = recipe.provenance
    assert provenance.dataset == "untitledwebsite123/food-recipes"
    assert provenance.revision == REVISION
    assert provenance.row_number == 1
    assert provenance.license == "unspecified"
    assert "Fixture Author" in provenance.attribution


def test_mismatched_quantities_keep_names_and_are_flagged() -> None:
    recipe = normalize_row(_row(RecipeIngredientQuantities='c("1")'), 1, REVISION)
    assert isinstance(recipe, CorpusRecipe)
    assert not recipe.quantities_aligned
    assert [(i.quantity, i.name) for i in recipe.ingredients] == [("", "bread"), ("", "salt")]


def test_build_corpus_reports_every_rejection_with_reason_and_count() -> None:
    result = build_corpus(FIXTURE_ROWS, 10, "ds", REVISION)
    report = result.report
    assert [r.source_id for r in result.recipes] == [
        "hf-food-recipes-1",
        "hf-food-recipes-2",
        "hf-food-recipes-7",
    ]
    assert report.rows_read == 10
    assert report.accepted == 3
    assert report.quantities_unaligned == 1
    assert report.rejected == 7
    assert report.rejected_by_reason == {
        "duplicate_content": 1,
        "duplicate_source_id": 1,
        "invalid_recipe_id": 1,
        "malformed_ingredients": 1,
        "missing_ingredients": 1,
        "missing_instructions": 1,
        "missing_name": 1,
    }
    by_row = {r.row_number: r for r in report.rejections}
    assert by_row[3] == Rejection(
        row_number=3, source_recipe_id="1", reason=RejectionReason.DUPLICATE_SOURCE_ID
    )
    assert by_row[9].source_recipe_id is None


def test_rerunning_the_same_slice_yields_identical_output_without_duplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(_csv(FIXTURE_ROWS), encoding="utf-8")
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        assert main(["--source-file", str(source), "--rows", "10", "--output-dir", str(out)]) == 0
    assert main(["--source-file", str(source), "--rows", "10", "--output-dir", str(first)]) == 0
    for name in ("recipes.jsonl", "report.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    ids = [json.loads(line)["source_id"] for line in (first / "recipes.jsonl").open()]
    assert len(ids) == len(set(ids)) == 3


def test_write_corpus_output_is_utf8_json_lines(tmp_path: Path) -> None:
    result = build_corpus([_row(Name="Crème brûlée")], 1, "ds", REVISION)
    corpus, _ = write_corpus(result, tmp_path)
    assert json.loads(corpus.read_text(encoding="utf-8"))["name"] == "Crème brûlée"


def test_read_rows_drops_a_row_cut_short_by_the_byte_limit() -> None:
    text = _csv([_row(RecipeId="1"), _row(RecipeId="2"), _row(RecipeId="3")])
    cut = text[: text.rindex("3,") + 12]
    assert [r["RecipeId"] for r in read_rows(cut, 2, truncated=True)] == ["1", "2"]
    with pytest.raises(SliceTooSmallError):
        read_rows(cut, 3, truncated=True)


def test_read_rows_limits_to_requested_rows() -> None:
    text = _csv([_row(RecipeId=str(n), Name=f"Dish {n}") for n in range(1, 6)])
    assert len(read_rows(text, 3, truncated=False)) == 3


def test_cli_exits_nonzero_when_nothing_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "bad.csv"
    source.write_text(_csv([_row(Name="")]), encoding="utf-8")
    code = main(["--source-file", str(source), "--rows", "1", "--output-dir", str(tmp_path / "o")])
    assert code == 1
    report = json.loads((tmp_path / "o" / "report.json").read_text(encoding="utf-8"))
    assert report["rejected_by_reason"] == {"missing_name": 1}
