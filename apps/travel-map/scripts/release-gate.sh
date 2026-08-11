#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"

# Do this before any build attempt. A missing daemon/client is a release block,
# not a reason to publish an unverified image.
if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
    printf '%s\n' 'BLOCKED_DOCKER_UNAVAILABLE' >&2
    exit 2
fi

snapshot_root='apps/travel-map/resources/institution-snapshots'
if [ ! -f "$snapshot_root/current.json" ]; then
    printf '%s\n' 'BLOCKED_MISSING_APPROVED_SNAPSHOT' >&2
    exit 2
fi

snapshot_id=$(uv run --project apps/travel-map python -c \
    'from app.institutions.snapshot import verify_snapshot; print(verify_snapshot("apps/travel-map/resources/institution-snapshots").manifest.snapshot_id)')

uv sync --project apps/travel-map --frozen --dev
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
docker build --build-arg SNAPSHOT_ID="$snapshot_id" -t seoul-education-travel-map:0.1.0 apps/travel-map
