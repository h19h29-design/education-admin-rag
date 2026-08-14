# Seoul Address Alias Coordinate Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `서울특별시`, `서울시`, `서울`의 첫 주소 토큰만 동등하게 비교하여 카카오 주소 검색의 안전한 단일 exact 결과를 수용하고, 기존 98% 좌표 품질 및 사람 승인 경계를 유지한다.

**Architecture:** 카카오 Local provider 내부에 공백 정규화와 서울 첫 토큰 canonicalization을 분리한 작은 pure helper를 둔다. legacy institution geocoder는 한 번의 기존 주소 검색 응답에서 canonical 도로명 주소가 정확히 하나인 결과만 선택하고, 좌표 수치·provenance·후보 coverage/98% gate는 기존 경계를 그대로 통과시킨다. 운영 동기화와 기존 후보·포인터는 이 구현 계획에서 실행하거나 변경하지 않는다.

**Tech Stack:** Python 3.12, httpx, dataclasses, pytest/pytest-asyncio, Ruff, mypy, FastAPI application tests, Playwright.

## Global Constraints

- 구현 기준 명세는 `docs/superpowers/specs/2026-08-14-seoul-address-alias-coordinate-recovery-design.md`다.
- 첫 토큰 `서울특별시`, `서울시`, `서울`만 canonical `서울`로 바꾼다.
- 도로명, 구, 건물번호, 상세 문자열, 다른 시·도는 완전 일치를 유지한다.
- 한 주소당 Kakao address API 요청은 기존처럼 정확히 한 번이다. keyword/fallback/page 추가 요청을 만들지 않는다.
- canonical exact `road_address.address_name` 결과가 정확히 하나일 때만 좌표를 수용한다.
- 좌표는 finite WGS84 범위여야 하고 후보 생성 시 기존 서울 coverage 검증을 다시 통과해야 한다.
- 좌표 성공률 98%와 별도 review digest/`data-steward` 승인 절차를 완화하지 않는다.
- 기관명, 주소, 좌표, 응답 원문, API key, 인증 헤더는 로그·문서·테스트 산출물에 넣지 않는다.
- 기존 ignored 후보 `20260814T123000Z`, transaction, `current.json`, 운영 env를 수정·삭제·승인하지 않는다.
- 구현 완료 후에도 live sync/API 호출을 실행하지 않는다. 별도 사용자 승인이 있어야 candidate-only 운영 동기화를 한 번 실행할 수 있다.
- 새 런타임 의존성을 추가하지 않는다.

## File Structure

- Modify `apps/travel-map/app/providers/kakao_local.py`: 공백 정규화, 서울 첫 토큰 canonicalization, exact 단일 결과와 finite 좌표 검증.
- Modify `apps/travel-map/tests/institutions/test_sync.py`: institution geocoder의 주소 별칭, 단일 결과, 불일치, 요청 횟수, provenance 회귀.
- Modify `apps/travel-map/tests/providers/test_kakao_local.py`: provider 경계의 잘못된 좌표와 모호한 결과 회귀.
- Modify `apps/travel-map/README.md`: 관리자 전용 좌표 복구 원칙과 별도 live 승인 절차.

---

### Task 1: 제한된 서울 주소 canonicalization

**Files:**
- Modify: `apps/travel-map/app/providers/kakao_local.py:165-205,297-298`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:4376-4431`

**Interfaces:**
- Consumes: exact `str` 주소 from `KakaoLocalClient._geocode_impl()`.
- Produces: `_normalize_address_whitespace(value: str) -> str` and `_canonicalize_road_address(value: str) -> str`.
- Preserves: non-Seoul addresses and every token after the first token.

- [ ] **Step 1: Write failing pure canonicalization tests**

Import the provider module as the existing `kakao_module` alias and add this parameterized contract to `test_sync.py`:

```python
@pytest.mark.parametrize(
    ("left", "right", "matches"),
    (
        ("서울특별시 종로구 송월길 48", "서울 종로구 송월길 48", True),
        ("서울시  종로구  송월길 48", "서울 종로구 송월길 48", True),
        ("서울 종로구 송월길 48", "서울특별시 종로구 송월길 48", True),
        ("서울특별시 종로구 송월길 48", "서울 종로구 송월길 49", False),
        ("서울특별시 종로구 송월길 48", "서울 중구 송월길 48", False),
        ("경기도 가평군 교육원로 1", "서울 가평군 교육원로 1", False),
        ("기관 서울특별시 종로구 송월길 48", "기관 서울 종로구 송월길 48", False),
    ),
)
def test_kakao_road_address_canonicalization_is_limited_to_seoul_prefix(
    left: str,
    right: str,
    matches: bool,
) -> None:
    assert (
        kakao_module._canonicalize_road_address(left)
        == kakao_module._canonicalize_road_address(right)
    ) is matches
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -q -k 'road_address_canonicalization_is_limited'
```

Expected: FAIL because `_canonicalize_road_address` does not exist.

- [ ] **Step 3: Implement the minimal pure helpers**

Replace `_normalize_address` with these helpers in `kakao_local.py`:

```python
_SEOUL_ADDRESS_PREFIXES = frozenset({"서울", "서울시", "서울특별시"})


def _normalize_address_whitespace(value: str) -> str:
    return " ".join(value.split())


def _canonicalize_road_address(value: str) -> str:
    normalized = _normalize_address_whitespace(value)
    prefix, separator, remainder = normalized.partition(" ")
    if separator and prefix in _SEOUL_ADDRESS_PREFIXES:
        return f"서울 {remainder}"
    return normalized
```

Do not case-fold, Unicode-normalize, remove punctuation, or rewrite any later token.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command. Expected: all selected cases PASS.

- [ ] **Step 5: Commit the canonicalization unit**

```bash
git add apps/travel-map/app/providers/kakao_local.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "fix: canonicalize Seoul address aliases"
```

---

### Task 2: 단일 exact 결과와 좌표 경계 적용

**Files:**
- Modify: `apps/travel-map/app/providers/kakao_local.py:124-204`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:4376-4455`
- Modify: `apps/travel-map/tests/providers/test_kakao_local.py:1-275`

**Interfaces:**
- Consumes: `_canonicalize_road_address(value: str) -> str` from Task 1.
- Produces: `KakaoLocalClient.geocode(address: str) -> GeocodeResult | None` with one-request canonical exact matching.
- Preserves: `GeocodeResult.road_address` as normalized caller input and `confidence="EXACT_ROAD_ADDRESS"`.

- [ ] **Step 1: Write failing Seoul alias geocoder integration test**

Add a test that uses `MockTransport`, records every request, returns one Kakao-style abbreviated road address, and checks the original address remains authoritative:

```python
@pytest.mark.asyncio
async def test_kakao_geocoder_accepts_one_seoul_prefix_alias_without_fallback() -> None:
    requested = "서울특별시  종로구 송월길 48"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.params["query"] == requested
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "x": "126.9680",
                        "y": "37.5710",
                        "road_address": {
                            "address_name": "서울 종로구 송월길 48"
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(api_key="test-key", client=http)
        result = await client.geocode(requested)
        provenance = client.provenance()

    assert result == GeocodeResult(
        road_address="서울특별시 종로구 송월길 48",
        latitude=37.571,
        longitude=126.968,
        confidence="EXACT_ROAD_ADDRESS",
    )
    assert len(seen) == 1
    assert provenance.fetched_row_count == 1
    assert provenance.matched_row_count == 1
```

Import `GeocodeResult` beside `KakaoLocalClient` in the test module.

- [ ] **Step 2: Write failing ambiguity, mismatch, and coordinate tests**

Add parameterized provider-boundary tests that assert:

```python
def kakao_address_document(
    address: str,
    *,
    x: str = "126.968",
    y: str = "37.571",
) -> dict[str, object]:
    return {
        "x": x,
        "y": y,
        "road_address": {"address_name": address},
    }


@pytest.mark.parametrize(
    "documents",
    (
        [],
        [
            kakao_address_document("서울 종로구 송월길 49"),
        ],
        [
            kakao_address_document("서울 종로구 송월길 48"),
            kakao_address_document("서울특별시 종로구 송월길 48"),
        ],
        [{"x": "126.968", "y": "37.571", "road_address": None}],
    ),
)
@pytest.mark.asyncio
async def test_kakao_geocoder_rejects_nonexact_or_ambiguous_alias_results(
    documents: list[object],
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"documents": documents})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(api_key="test-key", client=http)
        result = await client.geocode("서울특별시 종로구 송월길 48")

    assert result is None
    assert requests == 1


@pytest.mark.parametrize(
    ("x", "y"),
    (
        ("nan", "37.571"),
        ("126.968", "inf"),
        ("181", "37.571"),
        ("126.968", "91"),
    ),
)
@pytest.mark.asyncio
async def test_kakao_geocoder_rejects_invalid_coordinate(
    x: str,
    y: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "documents": [
                    kakao_address_document(
                        "서울 종로구 송월길 48",
                        x=x,
                        y=y,
                    )
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(api_key="test-key", client=http)
        with pytest.raises(
            SourceDataError,
            match="Kakao Local coordinates are invalid",
        ):
            await client.geocode("서울특별시 종로구 송월길 48")
```

Keep this helper and both tests in the test file. Each invalid coordinate must raise
`SourceDataError` containing only `Kakao Local coordinates are invalid`.

- [ ] **Step 3: Run both focused tests and verify RED**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/providers/test_kakao_local.py \
  -q -k 'seoul_prefix_alias or nonexact_or_ambiguous_alias or geocoder_rejects_invalid_coordinate'
```

Expected: alias acceptance and non-finite/range coordinate cases FAIL under the old equality/parser.

- [ ] **Step 4: Apply canonical comparison and safe coordinate parsing**

In `_geocode_impl()` retain the original request parameter, but compare using the Task 1 helper:

```python
normalized_input = _normalize_address_whitespace(address)
canonical_input = _canonicalize_road_address(address)
exact: list[dict[object, object]] = []
for document in documents:
    if type(document) is not dict:
        raise SourceDataError("Kakao Local document is invalid")
    road = document.get("road_address")
    if type(road) is not dict:
        continue
    road_name = road.get("address_name")
    if (
        type(road_name) is str
        and road_name.strip()
        and _canonicalize_road_address(road_name) == canonical_input
    ):
        exact.append(document)
if len(exact) != 1:
    return None
```

Parse coordinates once, reject non-finite/out-of-range values, then append provenance only after validation:

```python
try:
    latitude = float(_required_string(selected, "y"))
    longitude = float(_required_string(selected, "x"))
except ValueError as exc:
    raise SourceDataError("Kakao Local coordinates are invalid") from exc
if (
    not isfinite(latitude)
    or not isfinite(longitude)
    or not -90.0 <= latitude <= 90.0
    or not -180.0 <= longitude <= 180.0
):
    raise SourceDataError("Kakao Local coordinates are invalid")
result = GeocodeResult(
    road_address=normalized_input,
    latitude=latitude,
    longitude=longitude,
    confidence="EXACT_ROAD_ADDRESS",
)
self._accepted.append(result)
return result
```

- [ ] **Step 5: Run focused provider/institution tests and verify GREEN**

Run the Step 3 command, then:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/providers/test_kakao_local.py \
  -q -k 'kakao_geocode or kakao_geocoder or missing_coordinate'
```

Expected: all selected tests PASS; request count remains one; provenance matched count changes only for accepted records.

- [ ] **Step 6: Commit the provider boundary**

```bash
git add apps/travel-map/app/providers/kakao_local.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/providers/test_kakao_local.py
git commit -m "fix: accept exact Seoul address aliases"
```

---

### Task 3: 관리자 문서와 전체 fail-closed 검증

**Files:**
- Modify: `apps/travel-map/README.md:55-122`

**Interfaces:**
- Consumes: provider behavior and tests from Tasks 1-2.
- Produces: administrator-only operating guidance; no user-facing policy change.
- Preserves: candidate-only sync, separate review/approval, and blocked release without approved `current.json`.

- [ ] **Step 1: Add the administrator-only recovery note**

In the existing administrator snapshot workflow section, add these exact operational facts:

```markdown
Coordinate recovery remains fail-closed. The geocoder treats only the leading
`서울특별시`, `서울시`, and `서울` tokens as equivalent; the district, road name,
building number, and every remaining token must match exactly, and exactly one
Kakao road-address result must remain. It does not issue fallback or keyword
requests and does not lower the 98% quality gate.

Completing offline tests does not authorize another live sync. Obtain explicit
approval for one candidate-only run, inspect only aggregate coordinate-quality
and provenance counts, then use the separate review and approval commands. Never
approve a candidate that still reports a coordinate-quality issue.
```

- [ ] **Step 2: Run formatting, static, and warning-strict Python gates**

Run:

```bash
uv sync --project apps/travel-map --frozen --dev
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
uv run --project apps/travel-map ruff format --check \
  apps/travel-map/app/providers/kakao_local.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/providers/test_kakao_local.py
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
```

Expected: every command exits 0. Do not format unrelated baseline files.

- [ ] **Step 3: Run browser and release-boundary regression**

Run:

```bash
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
set +e
apps/travel-map/scripts/release-gate.sh
gate_status=$?
set -e
test "$gate_status" -eq 2
test ! -e apps/travel-map/resources/institution-snapshots/current.json
```

Expected: Playwright exits 0; release gate exits 2 with `BLOCKED_INVALID_RELEASE_ARTIFACT`; `current.json` remains absent. Docker and live APIs are not reached.

- [ ] **Step 4: Perform privacy and artifact review**

Run:

```bash
git diff --check
git status --short
! git diff --name-only -- apps/travel-map | \
  rg '(\.env($|\.)|institution-snapshots|task-5-live)'
! git diff -- apps/travel-map | \
  rg 'KakaoAK [A-Za-z0-9_-]{8,}'
```

Expected: no credential literal, raw provider response, candidate, transaction, env file, or `current.json` is staged. Existing ignored live artifacts remain untouched.

- [ ] **Step 5: Commit documentation and verification boundary**

```bash
git add apps/travel-map/README.md
git diff --cached --check
git commit -m "docs: document coordinate recovery gate"
```

- [ ] **Step 6: Stop before any live operation**

Report the offline test totals, commit SHAs, unchanged candidate/current state, and the calculated requirement of at least 109 additional active institutions. Ask for a separate explicit authorization before any candidate-only live sync; do not call review or approval automatically.

## Self-Review

- Spec coverage: Tasks 1-2 cover the exact alias allowlist, strict remaining tokens, single result, one request, coordinate validation, and provenance; Task 3 covers administrator docs, full gates, artifact privacy, release fail-closed, and the live-operation stop.
- Placeholder scan: every test and implementation step contains its complete helper, handler, assertion, and command; no deferred implementation marker remains.
- Type consistency: both tasks use `_normalize_address_whitespace(value: str) -> str`, `_canonicalize_road_address(value: str) -> str`, `KakaoLocalClient.geocode(address: str) -> GeocodeResult | None`, and exact `dict[object, object]` response candidates.
