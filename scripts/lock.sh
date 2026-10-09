#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python -m piptools compile --strip-extras --no-annotate --no-header \
  --no-emit-index-url --no-emit-trusted-host \
  --output-file=requirements.txt pyproject.toml
python -m piptools compile --strip-extras --no-annotate --no-header \
  --no-emit-index-url --no-emit-trusted-host \
  --constraint=requirements.txt --extra=dev \
  --output-file=requirements-dev.txt pyproject.toml
