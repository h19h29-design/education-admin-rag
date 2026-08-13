# Seoul education travel map

This public no-login map previews routes and policy calculations. It is not an authorization or payment system. Public deployment is blocked until an approved live institution snapshot exists and manual release review is complete. Never promote test fixtures or synthetic institutions to `resources/institution-snapshots`.

## Local setup and offline checks

Run from the repository root:

```sh
uv sync --project apps/travel-map --frozen --dev
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
```

Copy the template only for local development. Never commit the result or put credentials in shell history, screenshots, issues, or logs.

```sh
cp apps/travel-map/.env.example apps/travel-map/.env
uv run --project apps/travel-map uvicorn app.main:app \
  --env-file apps/travel-map/.env --host 127.0.0.1 --port 8080
```

The application process and operator scripts are intentionally run from the
repository root with an explicit `--env-file apps/travel-map/.env`. The file is
local-only; it never overrides environment variables supplied by the deployment
platform, and neither script prints its values. Production uses the platform
secret manager rather than this file.

Register the exact public HTTPS app domain in Kakao Developers and restrict `KAKAO_JAVASCRIPT_KEY` to that domain. It is browser-only. `KAKAO_REST_API_KEY` is server-only and must never be sent to a browser or used as the JavaScript key. Store these server-side values in the deployment secret manager:

- `KAKAO_REST_API_KEY` for place and Kakao route calls.
- `SEOUL_TRANSIT_SERVICE_KEY` for Seoul transit routing.
- `OPINET_CERT_KEY` for fuel-price lookups.
- `NEIS_API_KEY` and `KINDERGARTEN_API_KEY` only for institution synchronization.

Production also requires explicit canonical HTTPS `ALLOWED_ORIGINS` and exact `ALLOWED_HOSTS`. The process fails before serving when they or runtime route credentials are incomplete.

## Provider extension points

Stage A fixes the registry order as Seoul Transit, Kakao Transit, Kakao Car,
and Kakao Walk. The follow-on plans are
[`docs/superpowers/plans/2026-08-10-seoul-public-road-routing-engine.md`](../../docs/superpowers/plans/2026-08-10-seoul-public-road-routing-engine.md)
and
[`docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md`](../../docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md).

Stage B changes only `build_car_provider_chain()` and retains the WALK-chain
regression test. Stage C changes only `build_walk_provider_chain()` and retains
the CAR-chain regression test. A public provider must not be promoted ahead of
Kakao as primary until it passes gold-route validation, missing-data detection,
and performance and outage fallback verification.

## Institution snapshot synchronization

Production accepts only the normalized snapshot selected by `resources/institution-snapshots/current.json`. Its pointer, approval metadata, hashes, and row schemas are validated at image build and startup. Never copy from `tests/fixtures` to this directory.

The snapshot workflow below is administrator-only. The public map does not
expose review or approval controls.

```sh
# 1. Networked, credentialed: creates .<id>.candidate only.
uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py \
  --env-file /secure/path/travel-map-sync.env

# 2. Credential-free: inspect source counts, observation-date histograms,
#    quarantine IDs, coordinate quality, provenance hashes, and diff.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id '<candidate-id>'

# 3. After a data steward independently records the review, publish exactly
#    the inspected digest. This is the only command that can update current.json.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  --snapshot-id '<candidate-id>' --review-digest '<64-lowercase-hex>' \
  --reviewer-role data-steward
```

The expected NEIS observation-date distribution is `1413/1/1` until official
source dates converge. This histogram is review provenance: it is neither an
automatic rejection nor permission to normalize or collapse distinct dates.
Release remains blocked until step 3 publishes the independently reviewed
digest. A missing or invalid approved snapshot is a release blocker, never
permission to substitute a sample catalog.

## Live smoke and manual approval

The live smoke runs exactly three bounded cases only after opt-in, a valid approved snapshot, and all runtime provider credentials:

```sh
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python \
  apps/travel-map/scripts/smoke-live.py --env-file apps/travel-map/.env
```

Without `TRAVEL_MAP_LIVE_SMOKE=1`, with missing credentials, or with no approved snapshot, it exits `2` and emits one safe status. Success output has only case ID, provider status, route count, decision, and latency. Provider status distinguishes available routes, a provider outage, no route, and an out-of-coverage request that deliberately made no provider call. It never emits institution IDs, names, addresses, coordinates, route IDs, allowance amounts, credentials, headers, or raw provider responses.

Do not approve a release from this smoke alone. Record a manual review of 30 origin/destination pairs stratified across all 25 Seoul districts, institution types, and foundation types. Verify each pair's address and main-gate coordinate, multiple routes, round-trip classification near the 12 km boundary, separation of mobility cost from allowance, source references, and lookup time. A designated reviewer must record approval.

## Quotas, privacy, and rule provenance

Provider `503` and rate-limit results are unavailable data, not a reason to retry aggressively or invent a route. Respect `Retry-After`, stop the affected live check, inspect provider status privately, then retry only after its window. Do not add destination queries, addresses, route geometry, or credentials to logs or telemetry.

The current rule sources are versioned in `resources/rules/local-travel-2026-07-01.json`:

- [국가법령정보센터 여비규정](https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535)
- [국가법령정보센터 서울특별시교육청 조례](https://www.law.go.kr/LSW/ordinInfoP.do?ordinSeq=2099835)
- [인사혁신처 보수·여비 안내](https://www.mpm.go.kr/mpm/info/resultPay/payBoard/?boardId=bbs_0000000000000035&category=%EB%B3%B4%EC%88%98&cntId=693&mode=view)

## Container and release gate

The Docker context is staged from a reviewed workspace: it validates SGIS source and normalized geodata hashes, hash-pinned rule payloads, and `current.json` before Docker is consulted. The staged context contains no `.env`, Git metadata, source/raw provider data, geodata source, institution-source input, tests, E2E files, artifacts, or historical snapshots—only the snapshot selected by `current.json`. The runtime image uses UID `10001` and contains only application code, rules, normalized geodata/manifest, and that one approved institution snapshot. It has a `/healthz` health check and runs in production mode, so invalid settings or artifacts fail closed before serving traffic.

Run the single release command only after the approved snapshot and deployment secrets exist:

```sh
./apps/travel-map/scripts/release-gate.sh
```

It completes verified-artifact preflight before checking Docker, then runs offline gates and produces `seoul-education-travel-map:0.1.0` only when `current.json` selects a verified snapshot. Supply production secrets and allow-lists through the platform secret manager; never bake them into the image or a saved command.

For an explicit build and local production run, prepare the same minimal context
first. The supported build context is the staged directory below; do not run
`docker build apps/travel-map`, which can include retained snapshot history.

```sh
context_parent=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-release.XXXXXX")
trap 'rm -rf -- "$context_parent"' EXIT HUP INT TERM
snapshot_id=$(uv run --project apps/travel-map python \
  apps/travel-map/scripts/prepare-release-context.py \
  --source apps/travel-map --destination "$context_parent/context")
docker build --build-arg SNAPSHOT_ID="$snapshot_id" \
  -t seoul-education-travel-map:0.1.0 "$context_parent/context"
docker run --rm --init -p 8080:8080 \
  --env-file /secure/path/travel-map-production.env \
  seoul-education-travel-map:0.1.0
```
