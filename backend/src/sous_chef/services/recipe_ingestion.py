"""Build the normalized recipe corpus from a bounded slice of source rows."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from sous_chef.domain.recipe_corpus import (
    CorpusRecipe,
    Rejection,
    RejectionReason,
    content_key,
    normalize_row,
)

CORPUS_FILENAME = "recipes.jsonl"
REPORT_FILENAME = "report.json"
MAX_ROWS = 1000


class SliceTooSmallError(Exception):
    """The fetched bytes held fewer complete rows than were requested."""


class IngestionReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str
    revision: str
    rows_requested: int
    rows_read: int
    accepted: int
    rejected: int
    quantities_unaligned: int
    rejected_by_reason: dict[str, int]
    rejections: list[Rejection]


@dataclass(frozen=True)
class IngestionResult:
    recipes: list[CorpusRecipe]
    report: IngestionReport


def read_rows(text: str, rows: int, truncated: bool) -> list[dict[str, str]]:
    """Parse the CSV text, dropping a final row that the byte limit may have cut short."""
    csv.field_size_limit(1 << 24)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    parsed = [{k: v or "" for k, v in row.items() if k} for row in reader]
    if truncated and parsed:
        parsed.pop()
    if truncated and len(parsed) < rows:
        raise SliceTooSmallError(f"only {len(parsed)} complete rows were fetched; need {rows}")
    return parsed[:rows]


def build_corpus(
    source_rows: list[dict[str, str]], rows_requested: int, dataset: str, revision: str
) -> IngestionResult:
    recipes: list[CorpusRecipe] = []
    rejections: list[Rejection] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()

    for row_number, row in enumerate(source_rows, start=1):
        outcome = normalize_row(row, row_number, revision)
        if isinstance(outcome, Rejection):
            rejections.append(outcome)
            continue
        reason: RejectionReason | None = None
        if outcome.source_id in seen_ids:
            reason = RejectionReason.DUPLICATE_SOURCE_ID
        elif (key := content_key(outcome)) in seen_content:
            reason = RejectionReason.DUPLICATE_CONTENT
        if reason is not None:
            rejections.append(
                Rejection(
                    row_number=row_number,
                    source_recipe_id=outcome.provenance.source_recipe_id,
                    reason=reason,
                )
            )
            continue
        seen_ids.add(outcome.source_id)
        seen_content.add(key)
        recipes.append(outcome)

    counts = Counter(r.reason.value for r in rejections)
    report = IngestionReport(
        dataset=dataset,
        revision=revision,
        rows_requested=rows_requested,
        rows_read=len(source_rows),
        accepted=len(recipes),
        rejected=len(rejections),
        quantities_unaligned=sum(not r.quantities_aligned for r in recipes),
        rejected_by_reason=dict(sorted(counts.items())),
        rejections=rejections,
    )
    return IngestionResult(recipes=recipes, report=report)


def write_corpus(result: IngestionResult, output_dir: Path) -> tuple[Path, Path]:
    """Write byte-identical output for identical input: ordered rows, sorted keys, no clocks."""
    output_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = output_dir / CORPUS_FILENAME
    report_path = output_dir / REPORT_FILENAME
    lines = (
        json.dumps(r.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        for r in result.recipes
    )
    corpus_path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    report_text = json.dumps(
        result.report.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
    )
    report_path.write_text(report_text + "\n", encoding="utf-8", newline="\n")
    return corpus_path, report_path
