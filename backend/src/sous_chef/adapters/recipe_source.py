"""Bounded retrieval of a small slice of the pinned Hugging Face recipe CSV."""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass

from sous_chef.domain.recipe_corpus import DATASET_ID, SOURCE_FILE

# Commit of the dataset repository the corpus is built from. Change it deliberately.
PINNED_REVISION = "f4683928f44a20e68fd9ae8ce4c4092ca6031aec"
BYTES_PER_ROW_BUDGET = 16 * 1024
MAX_FETCH_BYTES = 16 * 1024 * 1024
_REVISION = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class CsvSlice:
    text: str
    # True when the bytes were cut off, so the final row may be incomplete.
    truncated: bool


def source_url(revision: str) -> str:
    if not _REVISION.fullmatch(revision):
        raise ValueError("revision must be a full 40-character commit hash")
    return f"https://huggingface.co/datasets/{DATASET_ID}/resolve/{revision}/{SOURCE_FILE}"


def fetch_csv_slice(rows: int, revision: str = PINNED_REVISION) -> CsvSlice:
    """Read only the leading bytes needed for `rows` rows; never the whole 700 MB file."""
    limit = min(MAX_FETCH_BYTES, (rows + 1) * BYTES_PER_ROW_BUDGET)
    request = urllib.request.Request(
        source_url(revision), headers={"Range": f"bytes=0-{limit - 1}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read(limit)
    return CsvSlice(text=data.decode("utf-8", errors="ignore"), truncated=len(data) >= limit)
