#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"

context_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-release.XXXXXX")
context_root="$context_parent/context"
trap 'rm -rf -- "$context_parent"' EXIT HUP INT TERM

# This validates the raw recorded source, both normalized geodata outputs, the
# hash-pinned rules, and current approved snapshot before Docker is consulted.
if ! snapshot_id=$(uv run --project apps/travel-map python \
    apps/travel-map/scripts/prepare-release-context.py \
    --source apps/travel-map --destination "$context_root" 2>/dev/null); then
    printf '%s\n' 'BLOCKED_INVALID_RELEASE_ARTIFACT' >&2
    exit 2
fi

# A missing daemon/client is a release block, never a reason to publish an
# artifact whose preflight has not completed.
if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
    printf '%s\n' 'BLOCKED_DOCKER_UNAVAILABLE' >&2
    exit 2
fi

uv sync --project apps/travel-map --frozen --dev
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
docker build --build-arg SNAPSHOT_ID="$snapshot_id" -t seoul-education-travel-map:0.1.0 "$context_root"
