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
uv run --project apps/travel-map uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Copy the template only for local development. Never commit the result or put credentials in shell history, screenshots, issues, or logs.

```sh
cp apps/travel-map/.env.example apps/travel-map/.env
```

Register the exact public HTTPS app domain in Kakao Developers and restrict `KAKAO_JAVASCRIPT_KEY` to that domain. It is browser-only. `KAKAO_REST_API_KEY` is server-only and must never be sent to a browser or used as the JavaScript key. Store these server-side values in the deployment secret manager:

- `KAKAO_REST_API_KEY` for place and Kakao route calls.
- `SEOUL_TRANSIT_SERVICE_KEY` for Seoul transit routing.
- `OPINET_CERT_KEY` for fuel-price lookups.
- `NEIS_API_KEY` and `KINDERGARTEN_API_KEY` only for institution synchronization.

Production also requires explicit canonical HTTPS `ALLOWED_ORIGINS` and exact `ALLOWED_HOSTS`. The process fails before serving when they or runtime route credentials are incomplete.

## Institution snapshot synchronization

Production accepts only the normalized snapshot selected by `resources/institution-snapshots/current.json`. Its pointer, approval metadata, hashes, and row schemas are validated at image build and startup. Never copy from `tests/fixtures` to this directory.

With NEIS, kindergarten, and Kakao REST synchronization keys configured:

```sh
uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py
uv run --project apps/travel-map python -c 'from app.institutions.snapshot import verify_snapshot; print(verify_snapshot("apps/travel-map/resources/institution-snapshots").manifest.snapshot_id)'
```

Use the synchronizer as the only promotion path. An authorized data reviewer must check source scope, counts, quarantined records, coordinate quality, and the diff before approving the manifest. A missing or invalid approved snapshot is a release blocker, never permission to substitute a sample catalog.

## Live smoke and manual approval

The live smoke runs exactly three bounded cases only after opt-in, a valid approved snapshot, and all runtime provider credentials:

```sh
TRAVEL_MAP_LIVE_SMOKE=1 uv run --project apps/travel-map python apps/travel-map/scripts/smoke-live.py
```

Without `TRAVEL_MAP_LIVE_SMOKE=1`, with missing credentials, or with no approved snapshot, it exits `2` and emits one safe status. Success output has only case ID, provider status, route count, decision, latency, and a representative-route boolean. It never emits institution IDs, names, addresses, coordinates, route IDs, allowance amounts, credentials, headers, or raw provider responses.

Do not approve a release from this smoke alone. Record a manual review of 30 origin/destination pairs stratified across all 25 Seoul districts, institution types, and foundation types. Verify each pair's address and main-gate coordinate, multiple routes, round-trip classification near the 12 km boundary, separation of mobility cost from allowance, source references, and lookup time. A designated reviewer must record approval.

## Quotas, privacy, and rule provenance

Provider `503` and rate-limit results are unavailable data, not a reason to retry aggressively or invent a route. Respect `Retry-After`, stop the affected live check, inspect provider status privately, then retry only after its window. Do not add destination queries, addresses, route geometry, or credentials to logs or telemetry.

The current rule sources are versioned in `resources/rules/local-travel-2026-07-01.json`:

- [국가법령정보센터 여비규정](https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535)
- [국가법령정보센터 서울특별시교육청 조례](https://www.law.go.kr/LSW/ordinInfoP.do?ordinSeq=2099835)
- [인사혁신처 보수·여비 안내](https://www.mpm.go.kr/mpm/info/resultPay/payBoard/?boardId=bbs_0000000000000035&category=%EB%B3%B4%EC%88%98&cntId=693&mode=view)

## Container and release gate

The Docker context is deny-by-default: no `.env`, Git metadata, source/raw provider data, geodata source, institution-source input, tests, E2E files, or artifacts are transferred. The runtime image uses UID `10001` and contains only application code, rules, normalized geodata/manifest, and the current approved institution snapshot. It has a `/healthz` health check and runs in production mode, so invalid settings or artifacts fail closed before serving traffic.

Run the single release command only after the approved snapshot and deployment secrets exist:

```sh
./apps/travel-map/scripts/release-gate.sh
```

It checks Docker before any image build, runs offline gates, and produces `seoul-education-travel-map:0.1.0` only when `current.json` selects a verified snapshot. Supply production secrets and allow-lists through the platform secret manager; never bake them into the image or a saved command.
