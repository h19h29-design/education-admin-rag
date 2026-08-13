#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

case "${1:-}" in
  policy)
    ./scripts/verify-public-repo.sh
    ;;
  quality)
    uv sync --locked --group dev
    uv run pytest -q tests
    uv run ruff check src tests
    uv run ruff format --check src
    uv run mypy --strict --explicit-package-bases src
    uv lock --check --offline
    uv sync --project apps/travel-map --frozen --dev
    uv run --project apps/travel-map pytest apps/travel-map/tests -q -W error
    uv run --project apps/travel-map ruff check apps/travel-map
    uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
    ;;
  security)
    ./scripts/verify-public-repo.sh
    ./scripts/scan-secrets.sh
    ;;
  docs)
    uv sync --locked --group dev
    uv run pytest tests/test_release_shell_scripts.py tests/test_gitlab_public_ci.py -q
    ;;
  *)
    printf '%s\n' 'public_ci_gate=invalid' >&2
    exit 2
    ;;
esac
