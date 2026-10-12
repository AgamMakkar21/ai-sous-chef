"""Command line entry point: `python -m sous_chef.ingest_recipes`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sous_chef.adapters.recipe_source import PINNED_REVISION, CsvSlice, fetch_csv_slice
from sous_chef.domain.recipe_corpus import DATASET_ID
from sous_chef.services.recipe_ingestion import (
    MAX_ROWS,
    SliceTooSmallError,
    build_corpus,
    read_rows,
    write_corpus,
)


def _rows_arg(value: str) -> int:
    rows = int(value)
    if not 1 <= rows <= MAX_ROWS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_ROWS}")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a small, pinned slice of the Hugging Face recipe dataset."
    )
    parser.add_argument("--rows", type=_rows_arg, default=100, help="source rows to read")
    parser.add_argument("--output-dir", type=Path, default=Path("data/corpus"))
    parser.add_argument("--revision", default=PINNED_REVISION, help="dataset commit hash")
    parser.add_argument(
        "--source-file", type=Path, help="read a local CSV instead of fetching (offline/tests)"
    )
    args = parser.parse_args(argv)

    if args.source_file is not None:
        csv_slice = CsvSlice(text=args.source_file.read_text(encoding="utf-8"), truncated=False)
    else:
        csv_slice = fetch_csv_slice(args.rows, args.revision)
    try:
        source_rows = read_rows(csv_slice.text, args.rows, csv_slice.truncated)
    except SliceTooSmallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    result = build_corpus(source_rows, args.rows, DATASET_ID, args.revision)
    corpus_path, report_path = write_corpus(result, args.output_dir)
    report = result.report
    print(
        f"rows read: {report.rows_read}  accepted: {report.accepted}  "
        f"rejected: {report.rejected} {report.rejected_by_reason}  "
        f"quantities unaligned: {report.quantities_unaligned}"
    )
    print(f"corpus: {corpus_path}\nreport: {report_path}")
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
