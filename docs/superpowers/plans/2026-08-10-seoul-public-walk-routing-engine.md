# Seoul Public Walk Routing Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 서울시 공공 보행 네트워크를 검증된 불변 snapshot으로 컴파일하고, 서울 안에서는 공공망으로 최단시간·최단거리 도보 경로를 반환하며 범위 밖·단절 구간은 Stage A의 카카오 도보 provider로 안전하게 넘긴다.

**Architecture:** 서울 열린데이터광장의 WGS84 노드·링크 CSV와 공식 유형코드 XLSX를 오프라인 빌드 파이프라인에서 검증해 메모리 효율적인 CSR 그래프와 geometry SQLite로 만든다. 런타임 `SeoulPublicWalkProvider`는 Stage A의 `RouteProvider` 계약만 구현하며, 좌표를 같은 연결요소의 노드에 스냅한 뒤 A*로 거리·시간 경로를 계산한다. 공공망이 답할 수 없는 요청은 직선거리로 꾸미지 않고 빈 `ProviderResult`와 원인 경고를 반환하므로, Stage A provider chain의 다음 `KakaoWalkProvider`가 전체 도보 경로를 계산한다.

**Tech Stack:** Python 3.12, uv, FastAPI 애플리케이션의 immutable dataclass Stage A routing contracts, pydantic-settings, NumPy 2, Shapely 2.1, openpyxl 3.1, 표준 라이브러리 `csv`·`sqlite3`·`heapq`·`hashlib`, pytest, Ruff, mypy

## Global Constraints

- 모든 경로는 저장소 루트가 아니라 `apps/travel-map/`을 기준으로 한다.
- 이 계획은 단계 A가 만든 독립 FastAPI 앱과 공통 route 계약 위에 적용하며 기존 루트 HTML·RAG `src/`·`data/`·`tests/`를 수정하지 않는다.
- 공개 도보망의 공식 원천은 [서울시 자치구별 도보 네트워크 공간정보](https://data.seoul.go.kr/dataList/OA-21208/A/1/datasetView.do)다. 원천은 WGS84이며 포털 설명상 2020년 기준이라는 한계를 manifest와 route warning에 보존한다.
- 원천이 제공하는 필드는 `NODE_TYPE`, `NODE_WKT`, `NODE_ID`, `NODE_TYPE_CD`, `LNKG_WKT`, `LNKG_ID`, `LNKG_TYPE_CD`, `BGNG_LNKG_ID`, `END_LNKG_ID`, `LNKG_LEN`, `SGG_CD`, `SGG_NM`, `EMD_CD`, `EMD_NM`이다.
- 런타임 요청 때 서울 열린데이터 API를 호출하지 않는다. 품질 게이트를 통과한 불변 snapshot만 읽는다.
- 공공망 밖, 150m 안에 스냅 노드가 없음, 연결요소 단절, snapshot 손상은 직선 연결로 대체하지 않는다. 구조화 경고를 반환하고 Stage A의 카카오 WALK fallback을 사용한다.
- 원천에는 방향 필드가 없으므로 모든 유효 링크는 양방향으로 컴파일하고 `directionModel=BIDIRECTIONAL_SOURCE_HAS_NO_DIRECTION`을 manifest에 공개한다.
- `WALK` route의 이동비는 0원, `CostStatus.KNOWN`이다. 여비·관내외 법적 판정은 이 provider가 계산하지 않는다.
- 도보시간은 1.25m/s 기본속도와 시설유형별 보수적 속도·횡단 대기시간을 사용한 예상치이며 `WALK_DURATION_ESTIMATED` warning을 항상 붙인다.
- 지도에 반환하는 geometry는 공공 링크 WKT만 이어 붙인다. 원점·목적지와 스냅 노드 사이를 보행 가능하다고 가정한 직선 선분은 만들지 않는다.
- snapshot·manifest·현재 pointer는 원자적으로 전환하고 파일 해시가 하나라도 다르면 이전 snapshot을 계속 제공한다.
- 도보 route 캐시는 Stage A의 장기 TTL 정책을 그대로 사용한다. 새 Redis·사용자 DB·관리자 화면·React를 추가하지 않는다.
- 위치 원문과 정밀 좌표를 애플리케이션 로그에 남기지 않는다. provider warning과 운영 metric에는 코드·snapshot ID·지연시간만 남긴다.
- 공식 데이터의 링크유형 코드가 codebook에 없거나 topology 필드가 모순되면 해당 snapshot을 승격하지 않는다.
- `fixture-v1` snapshot은 CI·개발에서만 허용한다. Stage A `environment=production`이면 공공 provider를 등록하지 않고 검증된 production snapshot이 설치될 때까지 카카오 WALK만 사용한다.

---

## Prerequisite: Stage A Contract to Consume Unchanged

단계 C 작업 전에 다음 Stage A 파일과 계약이 존재해야 한다. 단계 C는 이 파일의 타입 이름이나 필드를 바꾸지 않는다.

`apps/travel-map/app/routing/models.py`:

```python
@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float

class TravelMode(StrEnum):
    TRANSIT = "TRANSIT"
    CAR = "CAR"
    WALK = "WALK"

class CostStatus(StrEnum):
    KNOWN = "KNOWN"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"

class FuelType(StrEnum):
    GASOLINE = "GASOLINE"
    DIESEL = "DIESEL"
    LPG = "LPG"

@dataclass(frozen=True)
class CarAssumptions:
    fuel_type: FuelType
    efficiency_km_per_liter: float
    parking_cost_krw: int

@dataclass(frozen=True)
class RouteCostBreakdown:
    fare_krw: int = 0
    fuel_krw: int = 0
    toll_krw: int = 0
    parking_krw: int = 0

@dataclass(frozen=True)
class ProviderWarning:
    code: str
    message: str
    source: str

@dataclass(frozen=True)
class RouteQuery:
    origin: Coordinate
    destination: Coordinate
    depart_at: datetime
    mode: TravelMode
    car_assumptions: CarAssumptions | None = None

@dataclass(frozen=True)
class RouteOption:
    id: str
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None
    cost_status: CostStatus
    cost_breakdown: RouteCostBreakdown | None
    geometry: tuple[Coordinate, ...]
    source: str
    source_as_of: datetime
    warnings: tuple[str, ...] = ()

@dataclass(frozen=True)
class ProviderResult:
    provider: str
    routes: tuple[RouteOption, ...]
    warnings: tuple[ProviderWarning, ...] = ()
```

`apps/travel-map/app/routing/provider.py`:

```python
class RouteProvider(Protocol):
    name: str
    supported_modes: frozenset[TravelMode]

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        raise NotImplementedError
```

`apps/travel-map/app/routing/bootstrap.py`:

```python
def build_walk_provider_chain(
    settings: Settings,
) -> tuple[RouteProvider, ...]:
    return (KakaoWalkProvider.from_settings(settings),)


def build_route_providers(
    settings: Settings,
) -> dict[TravelMode, tuple[RouteProvider, ...]]:
    # Stage A constructs seoul_transit, kakao_transit, kakao_car, and
    # kakao_walk with its shared HTTP client and server-side settings.
    return {
        TravelMode.TRANSIT: (seoul_transit, kakao_transit),
        TravelMode.CAR: build_car_provider_chain(settings),
        TravelMode.WALK: build_walk_provider_chain(settings),
    }
```

단계 C 완료 후 `build_walk_provider_chain()`만 `(SeoulPublicWalkProvider, KakaoWalkProvider)` 순서로 바뀐다. `build_car_provider_chain()`은 단계 A 또는 B가 구성한 값을 그대로 유지한다. Stage A orchestrator는 `ProviderResult.routes`가 비어 있으면 다음 provider를 호출하고 앞 provider의 `ProviderWarning`을 최종 부분결과에 합치는 계약이어야 한다.

## File Structure

```text
apps/travel-map/
├── app/
│   ├── settings.py                                # Stage A Settings에 도보 snapshot 설정 추가
│   ├── routing/
│   │   └── bootstrap.py                           # WALK provider chain 순서 변경
│   └── providers/
│       └── seoul_walk/
│           ├── __init__.py                        # 공개 provider export
│           ├── models.py                          # 원천·snapshot·graph 내부 타입
│           ├── source.py                          # CSV/XLSX 파싱과 시설유형 분류
│           ├── compiler.py                        # 검증, CSR·geometry DB·manifest 생성
│           ├── snapshot.py                        # 해시 검증과 current pointer 원자 승격
│           ├── graph.py                           # CSR 로드, grid 스냅, 연결요소 조회
│           ├── router.py                          # A* 거리·시간 경로와 geometry 조립
│           ├── provider.py                        # Stage A RouteProvider adapter
│           └── verification.py                    # snapshot·route·성능 release gate
├── resources/
│   └── walk-network/
│       ├── README.md                              # 공식 원천 취득·빌드·승격 runbook
│       ├── current.json                           # 승인 snapshot pointer
│       └── snapshots/
│           └── fixture-v1/                        # CI·개발용 소형 검증 snapshot
│               ├── graph.npz
│               ├── geometry.sqlite3
│               └── manifest.json
├── scripts/
│   ├── build_walk_snapshot.py                     # inbox 원천을 후보 snapshot으로 컴파일
│   ├── promote_walk_snapshot.py                   # 검증된 후보를 current로 원자 승격
│   ├── verify_walk_snapshot.py                    # topology·경로·성능 검증
│   └── compare_walk_providers.py                  # opt-in 카카오 비교 보고서
├── tests/
│   ├── fixtures/walk/
│   │   ├── network.csv
│   │   └── link-types.xlsx                        # 테스트가 결정적으로 생성하는 fixture
│   ├── providers/seoul_walk/
│   │   ├── conftest.py
│   │   ├── test_contract.py
│   │   ├── test_source.py
│   │   ├── test_compiler.py
│   │   ├── test_snapshot.py
│   │   ├── test_graph.py
│   │   ├── test_router.py
│   │   └── test_provider.py
│   └── routing/test_walk_provider_chain.py
├── pyproject.toml
├── uv.lock
└── Dockerfile
```

The fixture snapshot is generated from five nodes and six links in tests; it is not presented as real Seoul coverage. Production deployment replaces only `resources/walk-network/current.json` with a pointer to a fully validated official snapshot.

### Task 1: Lock the Stage A Contract and Define Walk Domain Types

**Files:**
- Modify: `apps/travel-map/pyproject.toml`
- Modify: `apps/travel-map/uv.lock`
- Create: `apps/travel-map/app/providers/seoul_walk/__init__.py`
- Create: `apps/travel-map/app/providers/seoul_walk/models.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_contract.py`

**Interfaces:**
- Consumes: `Coordinate`, `TravelMode`, `RouteQuery`, `RouteOption`, `ProviderWarning`, `ProviderResult` from `app.routing.models`; `RouteProvider` from `app.routing.provider`.
- Produces: `WalkFeature`, `WalkNode`, `WalkLink`, `WalkSource`, `SnapshotManifest`, `StoredLinkGeometry`, `SnapCandidate`, `SnapPair`, `GraphPath`; all later Stage C tasks import these exact names from `app.providers.seoul_walk.models`.

- [ ] **Step 1: Add a failing contract test**

```python
# tests/providers/seoul_walk/test_contract.py
from datetime import datetime
from typing import get_type_hints

from app.routing.models import (
    Coordinate,
    CostStatus,
    ProviderResult,
    ProviderWarning,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.provider import RouteProvider


def test_stage_a_route_contract_is_available_without_redefinition() -> None:
    query = RouteQuery(
        origin=Coordinate(latitude=37.5663, longitude=126.9779),
        destination=Coordinate(latitude=37.5651, longitude=126.9895),
        depart_at=datetime.fromisoformat("2026-08-10T09:00:00+09:00"),
        mode=TravelMode.WALK,
    )
    option = RouteOption(
        id="walk-contract",
        mode=TravelMode.WALK,
        duration_seconds=60,
        distance_meters=75,
        mobility_cost_krw=0,
        cost_status=CostStatus.KNOWN,
        cost_breakdown=RouteCostBreakdown(),
        geometry=(query.origin, query.destination),
        source="SEOUL_WALK_NETWORK",
        source_as_of=query.depart_at,
        warnings=("WALK_DURATION_ESTIMATED",),
    )
    result = ProviderResult(
        provider="SEOUL_WALK_NETWORK",
        routes=(option,),
        warnings=(
            ProviderWarning(
                code="PUBLIC_DATA_2020_BASIS",
                message="서울시 설명상 2020년 기준 보행망입니다.",
                source="SEOUL_WALK_NETWORK",
            ),
        ),
    )

    assert result.routes[0].geometry == (query.origin, query.destination)
    assert get_type_hints(RouteProvider.get_routes)["return"] is ProviderResult
```

- [ ] **Step 2: Run the contract test and verify Stage A is the only prerequisite**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_contract.py -q`

Expected: PASS when Stage A has landed. If it fails because a listed type or field is absent, stop Stage C and reconcile Stage A to the prerequisite block; do not duplicate a private route model under `seoul_walk`.

- [ ] **Step 3: Add build/runtime dependencies with exact version ranges**

```bash
uv add --project apps/travel-map 'numpy>=2.1,<3' 'openpyxl>=3.1,<4'
```

Expected: exit code 0, `pyproject.toml` and `uv.lock` are updated, and Stage A's existing Shapely 2 dependency has no resolver conflict.

- [ ] **Step 4: Define the walk-only internal types**

```python
# app/providers/seoul_walk/models.py
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from app.routing.models import Coordinate


class WalkFeature(IntEnum):
    FOOTWAY = 0
    CROSSWALK = 1
    OVERPASS = 2
    UNDERPASS = 3
    TUNNEL = 4
    INDOOR = 5


class RouteCriterion(StrEnum):
    DISTANCE = "DISTANCE"
    DURATION = "DURATION"


@dataclass(frozen=True, slots=True)
class WalkNode:
    source_id: str
    coordinate: Coordinate
    node_type_code: str


@dataclass(frozen=True, slots=True)
class WalkLink:
    source_id: str
    start_node_id: str
    end_node_id: str
    length_meters: float
    geometry_wkt: str
    link_type_code: str
    feature: WalkFeature
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class WalkSource:
    nodes: tuple[WalkNode, ...]
    links: tuple[WalkLink, ...]
    source_sha256: str
    codebook_sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    schema_version: int
    snapshot_id: str
    source_as_of: datetime
    generated_at: datetime
    source_url: str
    license_id: str
    source_basis: str
    coordinate_reference_system: str
    direction_model: str
    duration_model: str
    duration_parameters: tuple[tuple[str, float], ...]
    node_count: int
    link_count: int
    component_count: int
    largest_component_ratio: float
    quarantined_count: int
    source_sha256: str
    codebook_sha256: str
    graph_sha256: str
    geometry_sha256: str
    graph_path: Path
    geometry_path: Path


@dataclass(frozen=True, slots=True)
class StoredLinkGeometry:
    link_index: int
    source_id: str
    start_node_id: str
    end_node_id: str
    geometry_wkt: str


@dataclass(frozen=True, slots=True)
class SnapCandidate:
    node_index: int
    component_id: int
    distance_meters: float


@dataclass(frozen=True, slots=True)
class SnapPair:
    origin: SnapCandidate
    destination: SnapCandidate


@dataclass(frozen=True, slots=True)
class GraphPath:
    criterion: RouteCriterion
    node_indexes: tuple[int, ...]
    edge_indexes: tuple[int, ...]
    distance_meters: float
    duration_seconds: float
    geometry: tuple[Coordinate, ...]
    feature_counts: tuple[tuple[WalkFeature, int], ...]
```

```python
# app/providers/seoul_walk/__init__.py during Tasks 1–6
__all__: list[str] = []
```

Task 7 replaces this temporary empty export with the completed provider export shown in that task.

- [ ] **Step 5: Run type and contract tests**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_contract.py -q && uv run --project apps/travel-map mypy apps/travel-map/app/providers/seoul_walk/models.py`

Expected: `1 passed` and `Success: no issues found`.

- [ ] **Step 6: Commit the contract boundary**

```bash
git add apps/travel-map/pyproject.toml apps/travel-map/uv.lock apps/travel-map/app/providers/seoul_walk apps/travel-map/tests/providers/seoul_walk/test_contract.py
git commit -m "feat(travel-map): define public walk routing domain"
```

### Task 2: Parse and Validate the Official CSV and Type Codebook

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/source.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/conftest.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_source.py`
- Create: `apps/travel-map/tests/fixtures/walk/network.csv`
- Create during tests: `apps/travel-map/tests/fixtures/walk/link-types.xlsx`

**Interfaces:**
- Consumes: `WalkFeature`, `WalkNode`, `WalkLink`, `WalkSource` from Task 1.
- Produces: `read_codebook(path: Path, observed_codes: frozenset[str]) -> dict[str, str]`, `classify_link_label(label: str) -> WalkFeature`, `load_walk_source(csv_path: Path, codebook_path: Path) -> WalkSource`.

- [ ] **Step 1: Create a deterministic five-node official-schema CSV fixture**

```csv
NODE_TYPE,NODE_WKT,NODE_ID,NODE_TYPE_CD,LNKG_WKT,LNKG_ID,LNKG_TYPE_CD,BGNG_LNKG_ID,END_LNKG_ID,LNKG_LEN,SGG_CD,SGG_NM,EMD_CD,EMD_NM
NODE,POINT (126.9700 37.5600),N1,NORMAL,,,,,,,11140,중구,11140520,소공동
NODE,POINT (126.9710 37.5600),N2,NORMAL,,,,,,,11140,중구,11140520,소공동
NODE,POINT (126.9720 37.5600),N3,NORMAL,,,,,,,11140,중구,11140520,소공동
NODE,POINT (126.9710 37.5602),N4,NORMAL,,,,,,,11140,중구,11140520,소공동
NODE,POINT (126.9730 37.5600),N5,NORMAL,,,,,,,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9700 37.5600, 126.9710 37.5600),L1,10,N1,N2,88.2,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9710 37.5600, 126.9720 37.5600),L2,20,N2,N3,88.2,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9700 37.5600, 126.9710 37.5602),L3,10,N1,N4,90.5,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9710 37.5602, 126.9720 37.5600),L4,10,N4,N3,90.5,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9720 37.5600, 126.9730 37.5600),L5,30,N3,N5,88.2,11140,중구,11140520,소공동
LINK,,,,LINESTRING (126.9710 37.5600, 126.9730 37.5600),L6,40,N2,N5,176.4,11140,중구,11140520,소공동
```

- [ ] **Step 2: Generate the XLSX fixture and write failing parser tests**

```python
# tests/providers/seoul_walk/conftest.py
from pathlib import Path

import pytest
from openpyxl import Workbook


@pytest.fixture
def walk_codebook(tmp_path: Path) -> Path:
    path = tmp_path / "link-types.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "링크유형"
    sheet.append(("LNKG_TYPE_CD", "유형명"))
    sheet.append(("10", "일반 보행로"))
    sheet.append(("20", "대로변 횡단보도"))
    sheet.append(("30", "보행자 육교"))
    sheet.append(("40", "지하 연결통로"))
    workbook.save(path)
    return path
```

```python
# tests/providers/seoul_walk/test_source.py
from pathlib import Path

import pytest

from app.providers.seoul_walk.models import WalkFeature
from app.providers.seoul_walk.source import load_walk_source


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/walk/network.csv"


def test_loads_nodes_links_and_feature_labels(walk_codebook: Path) -> None:
    source = load_walk_source(FIXTURE, walk_codebook)

    assert len(source.nodes) == 5
    assert len(source.links) == 6
    assert source.links[1].feature is WalkFeature.CROSSWALK
    assert source.links[4].feature is WalkFeature.OVERPASS
    assert source.links[5].feature is WalkFeature.UNDERPASS
    assert len(source.source_sha256) == 64
    assert len(source.codebook_sha256) == 64


def test_rejects_observed_code_missing_from_codebook(
    tmp_path: Path, walk_codebook: Path
) -> None:
    csv_path = tmp_path / "unknown.csv"
    csv_path.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(",L6,40,", ",L6,99,"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="codebook에 없는 LNKG_TYPE_CD: 99"):
        load_walk_source(csv_path, walk_codebook)
```

- [ ] **Step 3: Run the source tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_source.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.source'`.

- [ ] **Step 4: Implement strict schema parsing, generic codebook lookup, and feature classification**

```python
# app/providers/seoul_walk/source.py
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from shapely import wkt
from shapely.geometry import LineString, Point

from app.providers.seoul_walk.models import (
    WalkFeature,
    WalkLink,
    WalkNode,
    WalkSource,
)
from app.routing.models import Coordinate

REQUIRED_COLUMNS = frozenset(
    {
        "NODE_TYPE",
        "NODE_WKT",
        "NODE_ID",
        "NODE_TYPE_CD",
        "LNKG_WKT",
        "LNKG_ID",
        "LNKG_TYPE_CD",
        "BGNG_LNKG_ID",
        "END_LNKG_ID",
        "LNKG_LEN",
        "SGG_CD",
        "SGG_NM",
        "EMD_CD",
        "EMD_NM",
    }
)

FEATURE_KEYWORDS = (
    ("횡단보도", WalkFeature.CROSSWALK),
    ("육교", WalkFeature.OVERPASS),
    ("터널", WalkFeature.TUNNEL),
    ("지하도", WalkFeature.UNDERPASS),
    ("지하연결", WalkFeature.UNDERPASS),
    ("건물", WalkFeature.INDOOR),
)

SPEED_METERS_PER_SECOND = {
    WalkFeature.FOOTWAY: 1.25,
    WalkFeature.CROSSWALK: 1.20,
    WalkFeature.OVERPASS: 1.00,
    WalkFeature.UNDERPASS: 1.00,
    WalkFeature.TUNNEL: 1.10,
    WalkFeature.INDOOR: 1.10,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_link_label(label: str) -> WalkFeature:
    normalized = re.sub(r"\s+", "", label)
    for keyword, feature in FEATURE_KEYWORDS:
        if keyword in normalized:
            return feature
    return WalkFeature.FOOTWAY


def _normalize_code(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_codebook(path: Path, observed_codes: frozenset[str]) -> dict[str, str]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    found: dict[str, str] = {}
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                values = [value for value in row if value is not None]
                for index, value in enumerate(values):
                    code = _normalize_code(value)
                    if code not in observed_codes:
                        continue
                    labels = [
                        str(item).strip()
                        for item in values[index + 1 :]
                        if _normalize_code(item) not in observed_codes
                    ]
                    if labels:
                        found[code] = labels[0]
    finally:
        workbook.close()
    missing = sorted(observed_codes - found.keys())
    if missing:
        raise ValueError(
            "codebook에 없는 LNKG_TYPE_CD: " + ", ".join(missing)
        )
    return found


def load_walk_source(csv_path: Path, codebook_path: Path) -> WalkSource:
    decoded: str | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = csv_path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise ValueError("CSV 인코딩은 UTF-8 또는 CP949여야 함")
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    columns = frozenset(reader.fieldnames or ())
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ValueError("필수 CSV 열 누락: " + ", ".join(missing))
    rows = tuple(reader)

    observed_codes = frozenset(
        row["LNKG_TYPE_CD"].strip()
        for row in rows
        if row["LNKG_TYPE_CD"].strip()
    )
    labels = read_codebook(codebook_path, observed_codes)
    nodes: list[WalkNode] = []
    links: list[WalkLink] = []

    for row_number, row in enumerate(rows, start=2):
        node_id = row["NODE_ID"].strip()
        if node_id:
            point = wkt.loads(row["NODE_WKT"])
            if not isinstance(point, Point):
                raise ValueError(f"{row_number}행 NODE_WKT가 POINT가 아님")
            nodes.append(
                WalkNode(
                    source_id=node_id,
                    coordinate=Coordinate(latitude=point.y, longitude=point.x),
                    node_type_code=row["NODE_TYPE_CD"].strip(),
                )
            )

        link_id = row["LNKG_ID"].strip()
        if link_id:
            geometry = wkt.loads(row["LNKG_WKT"])
            if not isinstance(geometry, LineString):
                raise ValueError(f"{row_number}행 LNKG_WKT가 LINESTRING이 아님")
            length = float(row["LNKG_LEN"])
            if length <= 0:
                raise ValueError(f"{row_number}행 LNKG_LEN은 양수여야 함")
            code = row["LNKG_TYPE_CD"].strip()
            feature = classify_link_label(labels[code])
            crossing_wait = 15.0 if feature is WalkFeature.CROSSWALK else 0.0
            links.append(
                WalkLink(
                    source_id=link_id,
                    start_node_id=row["BGNG_LNKG_ID"].strip(),
                    end_node_id=row["END_LNKG_ID"].strip(),
                    length_meters=length,
                    geometry_wkt=geometry.wkt,
                    link_type_code=code,
                    feature=feature,
                    duration_seconds=(length / SPEED_METERS_PER_SECOND[feature])
                    + crossing_wait,
                )
            )

    return WalkSource(
        nodes=tuple(nodes),
        links=tuple(links),
        source_sha256=_sha256(csv_path),
        codebook_sha256=_sha256(codebook_path),
    )
```

Add one CP949-encoded copy of the CSV fixture to the parametrized tests and assert that it yields the same ordered node IDs, link IDs, and semantic fields as the UTF-8 fixture. The byte-level source hashes must differ because the encodings differ.

- [ ] **Step 5: Add malformed WKT, missing-column, nonpositive-length, and duplicate-ID tests**

Add four parametrized cases to `test_source.py`. Each case writes a changed copy of the fixture and asserts the exact messages `필수 CSV 열 누락`, `NODE_WKT가 POINT가 아님`, `LNKG_LEN은 양수여야 함`, and `중복 NODE_ID` respectively. Implement duplicate node/link ID rejection in `load_walk_source` immediately before returning:

```python
    node_counts = Counter(node.source_id for node in nodes)
    link_counts = Counter(link.source_id for link in links)
    duplicate_nodes = sorted(
        item for item, count in node_counts.items() if count > 1
    )
    duplicate_links = sorted(
        item for item, count in link_counts.items() if count > 1
    )
    if duplicate_nodes:
        raise ValueError("중복 NODE_ID: " + ", ".join(duplicate_nodes))
    if duplicate_links:
        raise ValueError("중복 LNKG_ID: " + ", ".join(duplicate_links))
```

- [ ] **Step 6: Run parser tests and static checks**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_source.py -q && uv run --project apps/travel-map ruff check apps/travel-map/app/providers/seoul_walk/source.py apps/travel-map/tests/providers/seoul_walk`

Expected: all source tests PASS and Ruff reports `All checks passed!`.

- [ ] **Step 7: Commit the source adapter**

```bash
git add apps/travel-map/app/providers/seoul_walk/source.py apps/travel-map/tests/providers/seoul_walk apps/travel-map/tests/fixtures/walk/network.csv
git commit -m "feat(travel-map): parse Seoul public walk network"
```

### Task 3: Compile a Quality-Gated CSR Snapshot

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/compiler.py`
- Create: `apps/travel-map/scripts/build_walk_snapshot.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_compiler.py`

**Interfaces:**
- Consumes: `load_walk_source(csv_path, codebook_path) -> WalkSource` from Task 2.
- Produces: `compile_snapshot(source: WalkSource, output_root: Path, source_as_of: datetime, profile: BuildProfile) -> Path`; candidate directory contains `graph.npz`, `geometry.sqlite3`, `manifest.json`.

- [ ] **Step 1: Write failing compilation and quality-gate tests**

```python
# tests/providers/seoul_walk/test_compiler.py
from datetime import datetime
from pathlib import Path

import json
import numpy as np
import pytest

from app.providers.seoul_walk.compiler import BuildProfile, compile_snapshot
from app.providers.seoul_walk.source import load_walk_source


def test_compiles_bidirectional_csr_and_manifest(
    tmp_path: Path, walk_codebook: Path
) -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/walk/network.csv"
    source = load_walk_source(fixture, walk_codebook)
    snapshot = compile_snapshot(
        source=source,
        output_root=tmp_path,
        source_as_of=datetime.fromisoformat("2026-08-10T00:00:00+09:00"),
        profile=BuildProfile.FIXTURE,
    )
    graph = np.load(snapshot / "graph.npz", allow_pickle=False)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))

    assert graph["node_ids"].shape == (5,)
    assert graph["to_node"].shape == (12,)
    assert graph["offsets"].shape == (6,)
    assert manifest["nodeCount"] == 5
    assert manifest["linkCount"] == 6
    assert manifest["directionModel"] == "BIDIRECTIONAL_SOURCE_HAS_NO_DIRECTION"
    assert manifest["sourceBasis"] == "SEOUL_OPEN_DATA_DESCRIPTION_2020_BASIS"


def test_rejects_missing_endpoint_before_writing_snapshot(
    tmp_path: Path, walk_codebook: Path
) -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/walk/network.csv"
    source = load_walk_source(fixture, walk_codebook)
    broken = source.__class__(
        nodes=source.nodes,
        links=(source.links[0].__class__(
            source_id="BROKEN",
            start_node_id="N1",
            end_node_id="N404",
            length_meters=10.0,
            geometry_wkt="LINESTRING (126.9700 37.5600, 126.9701 37.5600)",
            link_type_code="10",
            feature=source.links[0].feature,
            duration_seconds=8.0,
        ),),
        source_sha256=source.source_sha256,
        codebook_sha256=source.codebook_sha256,
    )

    with pytest.raises(ValueError, match="존재하지 않는 링크 끝점: N404"):
        compile_snapshot(
            source=broken,
            output_root=tmp_path,
            source_as_of=datetime.fromisoformat("2026-08-10T00:00:00+09:00"),
            profile=BuildProfile.FIXTURE,
        )
    assert list(tmp_path.iterdir()) == []
```

Extend `tests/providers/seoul_walk/conftest.py` with the snapshot fixtures consumed by Tasks 4–7:

```python
from datetime import datetime

from app.providers.seoul_walk.compiler import BuildProfile, compile_snapshot
from app.providers.seoul_walk.source import load_walk_source


@pytest.fixture
def compiled_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
    walk_codebook: Path,
) -> Path:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/walk/network.csv"
    source = load_walk_source(fixture, walk_codebook)
    return compile_snapshot(
        source=source,
        output_root=tmp_path_factory.mktemp("compiled-walk"),
        source_as_of=datetime.fromisoformat("2026-08-10T00:00:00+09:00"),
        profile=BuildProfile.FIXTURE,
    )


@pytest.fixture
def second_compiled_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
    walk_codebook: Path,
) -> Path:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/walk/network.csv"
    source = load_walk_source(fixture, walk_codebook)
    return compile_snapshot(
        source=source,
        output_root=tmp_path_factory.mktemp("second-compiled-walk"),
        source_as_of=datetime.fromisoformat("2026-08-10T00:00:00+09:00"),
        profile=BuildProfile.FIXTURE,
    )
```

- [ ] **Step 2: Run compiler tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_compiler.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.compiler'`.

- [ ] **Step 3: Implement deterministic validation and CSR compilation**

Implement these exact types and public entry point in `compiler.py`:

```python
class BuildProfile(StrEnum):
    FIXTURE = "fixture"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    minimum_rows: int
    minimum_largest_component_ratio: float


THRESHOLDS = {
    BuildProfile.FIXTURE: QualityThresholds(1, 0.80),
    BuildProfile.PRODUCTION: QualityThresholds(400_000, 0.95),
}


def compile_snapshot(
    source: WalkSource,
    output_root: Path,
    source_as_of: datetime,
    profile: BuildProfile,
) -> Path:
    validate_source(source, THRESHOLDS[profile])
    snapshot_id = (
        f"seoul-walk-{source_as_of:%Y%m%d}-"
        f"{source.source_sha256[:12]}"
    )
    arrays, geometries, component_count, largest_ratio = build_csr(source)
    validate_compiled_graph(
        source=source,
        component_count=component_count,
        largest_component_ratio=largest_ratio,
        thresholds=THRESHOLDS[profile],
    )
    candidate = output_root / f".{snapshot_id}.building"
    final = output_root / snapshot_id
    candidate.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(candidate / "graph.npz", **arrays)
    write_geometry_database(candidate / "geometry.sqlite3", geometries)
    write_manifest(
        candidate=candidate,
        source=source,
        source_as_of=source_as_of,
        snapshot_id=snapshot_id,
        component_count=component_count,
        largest_component_ratio=largest_ratio,
    )
    candidate.replace(final)
    return final
```

`build_csr` must sort source node IDs and link IDs so identical inputs produce byte-identical node/edge ordering. Persist these arrays with the exact dtypes:

```python
arrays = {
    "node_ids": np.asarray(node_ids),
    "latitudes": np.asarray(latitudes, dtype=np.float64),
    "longitudes": np.asarray(longitudes, dtype=np.float64),
    "component_ids": np.asarray(component_ids, dtype=np.int32),
    "offsets": np.asarray(offsets, dtype=np.int64),
    "to_node": np.asarray(to_node, dtype=np.int32),
    "length_meters": np.asarray(length_meters, dtype=np.float32),
    "duration_seconds": np.asarray(duration_seconds, dtype=np.float32),
    "link_indexes": np.asarray(link_indexes, dtype=np.int32),
    "features": np.asarray(features, dtype=np.uint8),
    "link_ids": np.asarray(link_ids),
}
```

Each source link contributes two adjacency entries. Compute connected components with a union-find over source links before CSR assembly. `validate_source` must reject duplicate IDs, missing endpoints, non-finite coordinates, coordinates outside longitude `126.50..127.35` or latitude `37.30..37.80`, nonpositive lengths, production row count below 400,000, and largest component ratio below the selected threshold.

- [ ] **Step 4: Persist link geometry and a complete hash manifest**

Create `geometry.sqlite3` with this schema and insert links inside one transaction:

```sql
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE link_geometry (
    link_index INTEGER PRIMARY KEY,
    link_id TEXT NOT NULL UNIQUE,
    start_node_id TEXT NOT NULL,
    end_node_id TEXT NOT NULL,
    geometry_wkt TEXT NOT NULL
);
```

Write `manifest.json` with camelCase keys:

```json
{
  "schemaVersion": 1,
  "snapshotId": "seoul-walk-20260810-a1b2c3d4e5f6",
  "sourceUrl": "https://data.seoul.go.kr/dataList/OA-21208/A/1/datasetView.do",
  "license": "KOGL_TYPE_1_ATTRIBUTION",
  "sourceAsOf": "2026-08-10T00:00:00+09:00",
  "generatedAt": "2026-08-10T07:00:00Z",
  "sourceBasis": "SEOUL_OPEN_DATA_DESCRIPTION_2020_BASIS",
  "coordinateReferenceSystem": "EPSG:4326",
  "directionModel": "BIDIRECTIONAL_SOURCE_HAS_NO_DIRECTION",
  "durationModel": "WALK_SPEED_V1",
  "durationParameters": {
    "footwayMetersPerSecond": 1.25,
    "crosswalkMetersPerSecond": 1.2,
    "crosswalkWaitSecondsPerLink": 15.0,
    "overpassMetersPerSecond": 1.0,
    "underpassMetersPerSecond": 1.0,
    "tunnelMetersPerSecond": 1.1,
    "indoorMetersPerSecond": 1.1
  },
  "nodeCount": 5,
  "linkCount": 6,
  "componentCount": 1,
  "largestComponentRatio": 1.0,
  "quarantinedCount": 0,
  "sourceSha256": "1111111111111111111111111111111111111111111111111111111111111111",
  "codebookSha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "graphSha256": "3333333333333333333333333333333333333333333333333333333333333333",
  "geometrySha256": "4444444444444444444444444444444444444444444444444444444444444444"
}
```

The illustrative `snapshotId` above uses a concrete example suffix. `write_manifest` always uses the actual `source.source_sha256[:12]`, and tests assert the concrete fixture value produced at runtime.

- [ ] **Step 5: Add the build CLI with exact input paths and exit codes**

```python
# scripts/build_walk_snapshot.py
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

from app.providers.seoul_walk.compiler import BuildProfile, compile_snapshot
from app.providers.seoul_walk.source import load_walk_source


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--source-csv", type=Path, required=True)
    parser.add_argument("--codebook-xlsx", type=Path, required=True)
    parser.add_argument("--source-as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--profile", choices=[item.value for item in BuildProfile], required=True
    )
    args = parser.parse_args()
    source = load_walk_source(args.source_csv, args.codebook_xlsx)
    snapshot = compile_snapshot(
        source,
        args.output_root,
        args.source_as_of,
        BuildProfile(args.profile),
    )
    print(snapshot.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Keep this as a directly executable Stage A-style script. Run it from the repository root with `uv run --project apps/travel-map python apps/travel-map/scripts/build_walk_snapshot.py`; do not add an importable `scripts` package or a second CLI framework.

- [ ] **Step 6: Run fixture compilation twice and verify deterministic content**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_compiler.py -q`

Expected: all compiler tests PASS. The deterministic test compiles into two temporary roots and asserts equal `graphSha256`, `geometrySha256`, node ordering, and link ordering.

- [ ] **Step 7: Commit the snapshot compiler**

```bash
git add apps/travel-map/app/providers/seoul_walk/compiler.py apps/travel-map/scripts/build_walk_snapshot.py apps/travel-map/tests/providers/seoul_walk/test_compiler.py
git commit -m "feat(travel-map): compile quality-gated walk graph snapshots"
```

### Task 4: Verify and Atomically Promote Walk Snapshots

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/snapshot.py`
- Create: `apps/travel-map/scripts/promote_walk_snapshot.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_snapshot.py`
- Create: `apps/travel-map/resources/walk-network/current.json`

**Interfaces:**
- Consumes: candidate directory contract from Task 3.
- Produces: `verify_snapshot(snapshot_dir: Path) -> SnapshotManifest`, `promote_snapshot(snapshot_dir: Path, current_pointer: Path) -> SnapshotManifest`, `resolve_current_snapshot(current_pointer: Path) -> tuple[Path, SnapshotManifest]`.

- [ ] **Step 1: Write failing integrity and atomic-promotion tests**

```python
# tests/providers/seoul_walk/test_snapshot.py
import json
import os
from pathlib import Path

import pytest

from app.providers.seoul_walk.snapshot import (
    promote_snapshot,
    resolve_current_snapshot,
    verify_snapshot,
)


def test_promotes_verified_snapshot_atomically(
    compiled_snapshot: Path, tmp_path: Path
) -> None:
    pointer = tmp_path / "current.json"
    manifest = promote_snapshot(compiled_snapshot, pointer)
    resolved, resolved_manifest = resolve_current_snapshot(pointer)

    assert resolved == compiled_snapshot.resolve()
    assert resolved_manifest.snapshot_id == manifest.snapshot_id
    stored = json.loads(pointer.read_text(encoding="utf-8"))
    assert stored == {
        "schemaVersion": 1,
        "snapshotPath": os.path.relpath(
            compiled_snapshot.resolve(), pointer.parent.resolve()
        ),
        "snapshotId": manifest.snapshot_id,
    }


def test_corruption_does_not_replace_existing_pointer(
    compiled_snapshot: Path, second_compiled_snapshot: Path, tmp_path: Path
) -> None:
    pointer = tmp_path / "current.json"
    first = promote_snapshot(compiled_snapshot, pointer)
    (second_compiled_snapshot / "graph.npz").write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="graphSha256 불일치"):
        promote_snapshot(second_compiled_snapshot, pointer)

    _, still_current = resolve_current_snapshot(pointer)
    assert still_current.snapshot_id == first.snapshot_id
```

- [ ] **Step 2: Run snapshot tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_snapshot.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.snapshot'`.

- [ ] **Step 3: Implement full hash verification and pointer resolution**

`verify_snapshot` must parse every manifest field into `SnapshotManifest`, derive `graph_path=snapshot_dir / "graph.npz"` and `geometry_path=snapshot_dir / "geometry.sqlite3"`, reject unknown `schemaVersion`, recompute SHA-256 of both artifact files, open `graph.npz` with `allow_pickle=False`, run `PRAGMA integrity_check` on geometry SQLite, and ensure array lengths agree with `nodeCount` and twice `linkCount`.

```python
import json
import os


def resolve_current_snapshot(
    current_pointer: Path,
) -> tuple[Path, SnapshotManifest]:
    payload = json.loads(current_pointer.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("지원하지 않는 walk pointer schemaVersion")
    stored_path = Path(payload["snapshotPath"])
    snapshot_dir = (
        stored_path
        if stored_path.is_absolute()
        else current_pointer.parent / stored_path
    ).resolve()
    manifest = verify_snapshot(snapshot_dir)
    if payload.get("snapshotId") != manifest.snapshot_id:
        raise ValueError("pointer snapshotId와 manifest 불일치")
    return snapshot_dir, manifest
```

- [ ] **Step 4: Implement atomic promotion without deleting prior snapshots**

```python
def promote_snapshot(
    snapshot_dir: Path, current_pointer: Path
) -> SnapshotManifest:
    manifest = verify_snapshot(snapshot_dir.resolve())
    relative_snapshot_path = os.path.relpath(
        snapshot_dir.resolve(), current_pointer.parent.resolve()
    )
    payload = {
        "schemaVersion": 1,
        "snapshotPath": relative_snapshot_path,
        "snapshotId": manifest.snapshot_id,
    }
    current_pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = current_pointer.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, current_pointer)
    return manifest
```

Do not remove candidate or previous snapshot directories in this function. Retention is a separate recoverability concern outside Stage C.

- [ ] **Step 5: Add the promotion CLI and a committed fixture pointer**

`promote_walk_snapshot.py` accepts exactly `--snapshot-dir` and `--current-pointer`, calls `promote_snapshot`, prints the promoted snapshot ID, and returns 0. Generate the fixture snapshot from Task 3 under `resources/walk-network/snapshots/fixture-v1/`, then write a repository-relative pointer:

```json
{
  "schemaVersion": 1,
  "snapshotPath": "snapshots/fixture-v1",
  "snapshotId": "fixture-v1"
}
```

`resolve_current_snapshot` resolves relative `snapshotPath` against `current.json`의 parent directory. The test fixture builder renames the generated directory to `fixture-v1` and writes `snapshotId=fixture-v1` in its manifest; graph and geometry hashes remain the hashes of the unchanged artifacts.

Add the shared committed-pointer fixture:

```python
# tests/providers/seoul_walk/conftest.py
@pytest.fixture
def fixture_pointer() -> Path:
    app_root = Path(__file__).resolve().parents[3]
    pointer = app_root / "resources/walk-network/current.json"
    _, manifest = resolve_current_snapshot(pointer)
    assert manifest.snapshot_id == "fixture-v1"
    return pointer
```

- [ ] **Step 6: Run snapshot and corruption tests**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_snapshot.py -q`

Expected: all tests PASS; corruption cases cover graph hash, geometry hash, SQLite integrity, array length, and pointer/manifest ID mismatch.

- [ ] **Step 7: Commit atomic snapshot activation**

```bash
git add apps/travel-map/app/providers/seoul_walk/snapshot.py apps/travel-map/scripts/promote_walk_snapshot.py apps/travel-map/tests/providers/seoul_walk/test_snapshot.py apps/travel-map/resources/walk-network
git commit -m "feat(travel-map): verify and promote walk snapshots atomically"
```

### Task 5: Load the Graph and Snap Both Endpoints to One Connected Component

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/graph.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_graph.py`

**Interfaces:**
- Consumes: `resolve_current_snapshot`, `SnapshotManifest`, `Coordinate`, CSR array schema from Tasks 1–4.
- Produces: `WalkGraph.open(current_pointer: Path) -> WalkGraph`, `WalkGraph.snap_candidates(coordinate: Coordinate, max_distance_meters: float, limit: int) -> tuple[SnapCandidate, ...]`, `WalkGraph.snap_pair(origin: Coordinate, destination: Coordinate, max_distance_meters: float = 150.0) -> SnapPair | None`, `WalkGraph.haversine_between_nodes(left_index: int, right_index: int) -> float`, and `WalkGraph.load_link_geometries(link_indexes: tuple[int, ...]) -> dict[int, StoredLinkGeometry]`.

- [ ] **Step 1: Write failing nearest-node and component-aware pair tests**

```python
# tests/providers/seoul_walk/test_graph.py
from pathlib import Path

from app.providers.seoul_walk.graph import WalkGraph
from app.routing.models import Coordinate


def test_snap_pair_selects_nearest_candidates_in_same_component(
    fixture_pointer: Path,
) -> None:
    graph = WalkGraph.open(fixture_pointer)
    pair = graph.snap_pair(
        Coordinate(latitude=37.56001, longitude=126.97001),
        Coordinate(latitude=37.56001, longitude=126.97299),
    )

    assert pair is not None
    assert graph.node_ids[pair.origin.node_index] == "N1"
    assert graph.node_ids[pair.destination.node_index] == "N5"
    assert pair.origin.component_id == pair.destination.component_id


def test_snap_pair_returns_none_beyond_150_meters(fixture_pointer: Path) -> None:
    graph = WalkGraph.open(fixture_pointer)
    pair = graph.snap_pair(
        Coordinate(latitude=37.5700, longitude=126.9800),
        Coordinate(latitude=37.5600, longitude=126.9730),
    )

    assert pair is None
```

- [ ] **Step 2: Run graph tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_graph.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.graph'`.

- [ ] **Step 3: Implement immutable CSR loading and a 0.002-degree grid index**

```python
class WalkGraph:
    GRID_SIZE_DEGREES = 0.002

    @classmethod
    def open(cls, current_pointer: Path) -> "WalkGraph":
        snapshot_dir, manifest = resolve_current_snapshot(current_pointer)
        arrays = np.load(snapshot_dir / "graph.npz", allow_pickle=False)
        return cls(snapshot_dir, manifest, arrays)

    def _cell(self, coordinate: Coordinate) -> tuple[int, int]:
        return (
            math.floor(coordinate.latitude / self.GRID_SIZE_DEGREES),
            math.floor(coordinate.longitude / self.GRID_SIZE_DEGREES),
        )
```

The constructor stores NumPy arrays read-only with `array.flags.writeable = False` and builds `dict[tuple[int, int], tuple[int, ...]]` once. `load_link_geometries` opens `file:.../geometry.sqlite3?mode=ro&immutable=1` inside the current worker thread, executes one query, and closes it with a context manager; no SQLite connection crosses `asyncio.to_thread` boundaries. It never mutates the snapshot.

`haversine_between_nodes` calls the same WGS84 helper used by snapping. `load_link_geometries` deduplicates and sorts requested source link indexes, issues parameterized `SELECT` queries in chunks of 500 against the read-only SQLite connection, returns every requested row as `StoredLinkGeometry`, and raises `ValueError("선택 경로의 geometry 누락")` if the result count differs from the requested unique count.

- [ ] **Step 4: Implement exact haversine filtering and same-component pairing**

```python
def snap_pair(
    self,
    origin: Coordinate,
    destination: Coordinate,
    max_distance_meters: float = 150.0,
) -> SnapPair | None:
    origin_candidates = self.snap_candidates(origin, max_distance_meters, 5)
    destination_candidates = self.snap_candidates(destination, max_distance_meters, 5)
    compatible = [
        SnapPair(origin_item, destination_item)
        for origin_item in origin_candidates
        for destination_item in destination_candidates
        if origin_item.component_id == destination_item.component_id
    ]
    if not compatible:
        return None
    return min(
        compatible,
        key=lambda item: (
            item.origin.distance_meters + item.destination.distance_meters,
            item.origin.node_index,
            item.destination.node_index,
        ),
    )
```

`snap_candidates` visits enough surrounding grid cells to cover `max_distance_meters`, computes WGS84 haversine distance for every candidate, rejects distances over the limit, sorts by `(distance_meters, node_index)`, and returns at most `limit` items.

- [ ] **Step 5: Add cross-component and deterministic-tie tests**

Create a second fixture snapshot with one isolated two-node component. Assert that individually nearest nodes in different components are skipped when the second-nearest compatible pair is within 150m, and that no compatible pair returns `None`. Add two equidistant nodes and assert the lower stable node index wins.

- [ ] **Step 6: Run graph tests and a load smoke test**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_graph.py -q && uv run --project apps/travel-map python -c 'from pathlib import Path; from app.providers.seoul_walk.graph import WalkGraph; print(WalkGraph.open(Path("apps/travel-map/resources/walk-network/current.json")).manifest.snapshot_id)'`

Expected: graph tests PASS and stdout is `fixture-v1`.

- [ ] **Step 7: Commit graph loading and snapping**

```bash
git add apps/travel-map/app/providers/seoul_walk/graph.py apps/travel-map/tests/providers/seoul_walk/test_graph.py
git commit -m "feat(travel-map): snap walk queries to connected public graph"
```

### Task 6: Calculate Distance and Duration Routes with A*

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/router.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_router.py`

**Interfaces:**
- Consumes: `WalkGraph`, `SnapPair`, `RouteCriterion`, CSR arrays, read-only geometry database.
- Produces: `PublicWalkRouter(graph: WalkGraph, max_snap_distance_meters: float = 150.0)`, `PublicWalkRouter.route(origin: Coordinate, destination: Coordinate) -> tuple[GraphPath, ...]`; returns shortest-distance and fastest-time paths, removing duplicate edge sequences.

- [ ] **Step 1: Write failing route selection, geometry, and unreachable tests**

```python
# tests/providers/seoul_walk/test_router.py
from pathlib import Path

from app.providers.seoul_walk.graph import WalkGraph
from app.providers.seoul_walk.models import RouteCriterion, WalkFeature
from app.providers.seoul_walk.router import PublicWalkRouter
from app.routing.models import Coordinate


ORIGIN = Coordinate(latitude=37.5600, longitude=126.9700)
DESTINATION = Coordinate(latitude=37.5600, longitude=126.9730)


def test_returns_distinct_shortest_and_fastest_paths(fixture_pointer: Path) -> None:
    router = PublicWalkRouter(WalkGraph.open(fixture_pointer))
    routes = router.route(ORIGIN, DESTINATION)

    assert [route.criterion for route in routes] == [
        RouteCriterion.DURATION,
        RouteCriterion.DISTANCE,
    ]
    fastest, shortest = routes
    assert fastest.edge_indexes != shortest.edge_indexes
    assert fastest.duration_seconds < shortest.duration_seconds
    assert shortest.distance_meters < fastest.distance_meters
    assert shortest.geometry[0] == Coordinate(latitude=37.5600, longitude=126.9700)
    assert shortest.geometry[-1] == Coordinate(latitude=37.5600, longitude=126.9730)
    assert (WalkFeature.CROSSWALK, 1) in shortest.feature_counts


def test_returns_empty_tuple_when_no_compatible_snap_pair(
    fixture_pointer: Path,
) -> None:
    router = PublicWalkRouter(WalkGraph.open(fixture_pointer))

    assert router.route(
        Coordinate(latitude=37.7000, longitude=127.2000), DESTINATION
    ) == ()
```

- [ ] **Step 2: Run router tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_router.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.router'`.

- [ ] **Step 3: Implement A* with admissible distance and duration heuristics**

```python
class PublicWalkRouter:
    def __init__(
        self,
        graph: WalkGraph,
        max_snap_distance_meters: float = 150.0,
    ) -> None:
        if max_snap_distance_meters <= 0:
            raise ValueError("max_snap_distance_meters must be positive")
        self.graph = graph
        self.max_snap_distance_meters = max_snap_distance_meters
```

`route()` passes `self.max_snap_distance_meters` to `graph.snap_pair`; this is the only runtime snap threshold.

```python
def _a_star(
    graph: WalkGraph,
    start: int,
    goal: int,
    criterion: RouteCriterion,
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    frontier: list[tuple[float, float, int]] = [(0.0, 0.0, start)]
    best_cost = {start: 0.0}
    previous: dict[int, tuple[int, int]] = {}

    while frontier:
        _, cost, node = heapq.heappop(frontier)
        if cost != best_cost.get(node):
            continue
        if node == goal:
            return reconstruct_path(previous, start, goal)
        for edge_index in range(graph.offsets[node], graph.offsets[node + 1]):
            neighbor = int(graph.to_node[edge_index])
            edge_cost = (
                float(graph.length_meters[edge_index])
                if criterion is RouteCriterion.DISTANCE
                else float(graph.duration_seconds[edge_index])
            )
            candidate = cost + edge_cost
            if candidate >= best_cost.get(neighbor, math.inf):
                continue
            best_cost[neighbor] = candidate
            previous[neighbor] = (node, edge_index)
            straight_line = graph.haversine_between_nodes(neighbor, goal)
            heuristic = (
                straight_line
                if criterion is RouteCriterion.DISTANCE
                else straight_line / 1.25
            )
            heapq.heappush(
                frontier, (candidate + heuristic, candidate, neighbor)
            )
    return None
```

`reconstruct_path` walks `previous` from goal to start and reverses node and edge lists. A duration heuristic of straight-line distance divided by 1.25m/s is admissible because every configured facility speed is at most 1.25m/s and penalties are nonnegative.

- [ ] **Step 4: Assemble public link geometry in traversal order**

Retrieve all selected source link rows in one SQLite query keyed by `link_index`. For a traversal whose current node is the source link's end node, reverse the WKT coordinate order. Remove only an immediately repeated joint coordinate; retain all other shape points.

```python
def join_coordinates(
    ordered_segments: tuple[tuple[Coordinate, ...], ...]
) -> tuple[Coordinate, ...]:
    joined: list[Coordinate] = []
    for segment in ordered_segments:
        if joined and segment and joined[-1] == segment[0]:
            joined.extend(segment[1:])
        else:
            joined.extend(segment)
    if len(joined) < 2:
        raise ValueError("walk route geometry must contain at least two points")
    return tuple(joined)
```

Do not prepend origin or append destination straight lines. `GraphPath.distance_meters` is the sum of source `LNKG_LEN`; `duration_seconds` is the sum of compiled edge duration. Feature counts count unique traversed source links, not duplicated CSR directions.

- [ ] **Step 5: Return fastest first, shortest second, and deduplicate identical paths**

```python
def route(
    self, origin: Coordinate, destination: Coordinate
) -> tuple[GraphPath, ...]:
    pair = self.graph.snap_pair(
        origin,
        destination,
        max_distance_meters=self.max_snap_distance_meters,
    )
    if pair is None:
        return ()
    candidates = (
        self._build_path(pair, RouteCriterion.DURATION),
        self._build_path(pair, RouteCriterion.DISTANCE),
    )
    routes: list[GraphPath] = []
    seen: set[tuple[int, ...]] = set()
    for candidate in candidates:
        if candidate is None or candidate.edge_indexes in seen:
            continue
        seen.add(candidate.edge_indexes)
        routes.append(candidate)
    return tuple(routes)
```

- [ ] **Step 6: Add reverse-link, crosswalk-penalty, same-node, and malformed-geometry tests**

Assert that reverse traversal returns coordinates from destination to origin, a crosswalk route can be shorter but slower than an all-footway route, origin/destination snapping to one node returns no fabricated zero-length route, and malformed geometry in a candidate snapshot fails verification before runtime.

- [ ] **Step 7: Run router tests and full Stage C unit slice**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_router.py apps/travel-map/tests/providers/seoul_walk/test_graph.py apps/travel-map/tests/providers/seoul_walk/test_snapshot.py -q`

Expected: all selected tests PASS with no warning about unclosed SQLite connections.

- [ ] **Step 8: Commit A* routing**

```bash
git add apps/travel-map/app/providers/seoul_walk/router.py apps/travel-map/tests/providers/seoul_walk/test_router.py
git commit -m "feat(travel-map): route public walk graph by time and distance"
```

### Task 7: Adapt to `RouteProvider` and Insert the Stage A Fallback Chain

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/provider.py`
- Modify: `apps/travel-map/app/providers/seoul_walk/__init__.py`
- Modify: `apps/travel-map/app/settings.py`
- Modify: `apps/travel-map/app/routing/bootstrap.py`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_provider.py`
- Create: `apps/travel-map/tests/routing/test_walk_provider_chain.py`

**Interfaces:**
- Consumes: Stage A `RouteProvider` protocol and route models, `PublicWalkRouter.route`, `WalkGraph.open`, `build_walk_provider_chain(settings) -> tuple[RouteProvider, ...]`, the Stage A Kakao walk provider, and Stage A `RouteOrchestrator` fallback behavior.
- Consumes: Stage A `CoverageService.classify(point) -> CoverageState`; public routing is allowed only when both endpoints are `CoverageState.SEOUL`.
- Produces: `SeoulPublicWalkProvider.from_settings(settings: Settings) -> SeoulPublicWalkProvider`, `async get_routes(query: RouteQuery) -> ProviderResult`; `build_walk_provider_chain` returns `(SeoulPublicWalkProvider, KakaoWalkProvider)` and `build_route_providers` consumes it without changing the CAR chain.

- [ ] **Step 1: Write a failing provider normalization test**

```python
# tests/providers/seoul_walk/test_provider.py
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from app.providers.seoul_walk.provider import SeoulPublicWalkProvider
from app.policy.coverage import CoverageService
from app.routing.models import (
    CarAssumptions,
    Coordinate,
    CostStatus,
    FuelType,
    RouteCostBreakdown,
    RouteQuery,
    TravelMode,
)


@pytest.mark.asyncio
async def test_normalizes_public_paths_to_stage_a_route_options(
    fixture_pointer: Path,
) -> None:
    app_root = Path(__file__).resolve().parents[3]
    coverage = CoverageService.from_resources(app_root / "resources/geodata")
    provider = SeoulPublicWalkProvider.from_pointer(fixture_pointer, coverage)
    query = RouteQuery(
        origin=Coordinate(latitude=37.5600, longitude=126.9700),
        destination=Coordinate(latitude=37.5600, longitude=126.9730),
        depart_at=datetime.fromisoformat("2026-08-10T09:00:00+09:00"),
        mode=TravelMode.WALK,
        car_assumptions=CarAssumptions(
            fuel_type=FuelType.GASOLINE,
            efficiency_km_per_liter=12.0,
            parking_cost_krw=5_000,
        ),
    )
    result = await provider.get_routes(query)
    without_car_assumptions = await provider.get_routes(
        replace(query, car_assumptions=None)
    )

    assert provider.supported_modes == frozenset({TravelMode.WALK})
    assert result.provider == "SEOUL_WALK_NETWORK"
    assert 1 <= len(result.routes) <= 2
    assert all(route.mode is TravelMode.WALK for route in result.routes)
    assert all(route.mobility_cost_krw == 0 for route in result.routes)
    assert all(route.cost_status is CostStatus.KNOWN for route in result.routes)
    assert all(
        route.cost_breakdown == RouteCostBreakdown() for route in result.routes
    )
    assert [route.id for route in result.routes] == [
        route.id for route in without_car_assumptions.routes
    ]
    assert all(route.source == "SEOUL_WALK_NETWORK" for route in result.routes)
    assert all("WALK_DURATION_ESTIMATED" in route.warnings for route in result.routes)
```

- [ ] **Step 2: Write a failing fallback-chain integration test**

```python
# tests/routing/test_walk_provider_chain.py
from datetime import datetime
from pathlib import Path

import pytest

from app.providers.seoul_walk.provider import SeoulPublicWalkProvider
from app.policy.coverage import CoverageService
from app.routing.bootstrap import build_car_provider_chain, build_route_providers
from app.routing.models import (
    Coordinate,
    CostStatus,
    ProviderResult,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.orchestrator import RouteOrchestrator
from app.settings import Settings


class FakeKakaoWalkProvider:
    name = "KAKAO_WALK"
    supported_modes = frozenset({TravelMode.WALK})

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            routes=(RouteOption(
                id="kakao-walk-fallback",
                mode=TravelMode.WALK,
                duration_seconds=900,
                distance_meters=1_000,
                mobility_cost_krw=0,
                cost_status=CostStatus.KNOWN,
                cost_breakdown=RouteCostBreakdown(),
                geometry=(query.origin, query.destination),
                source="KAKAO",
                source_as_of=query.depart_at,
            ),),
        )


@pytest.mark.asyncio
async def test_outside_public_graph_falls_back_to_kakao(
    fixture_pointer: Path,
) -> None:
    app_root = Path(__file__).resolve().parents[2]
    coverage = CoverageService.from_resources(app_root / "resources/geodata")
    public = SeoulPublicWalkProvider.from_pointer(fixture_pointer, coverage)
    fake_kakao_walk_provider = FakeKakaoWalkProvider()
    orchestrator = RouteOrchestrator(
        {TravelMode.WALK: (public, fake_kakao_walk_provider)},
        max_concurrency=1,
    )
    collection = await orchestrator.collect(
        RouteQuery(
            origin=Coordinate(latitude=37.5600, longitude=126.9700),
            destination=Coordinate(latitude=37.7000, longitude=127.2000),
            depart_at=datetime.fromisoformat("2026-08-10T09:00:00+09:00"),
            mode=TravelMode.WALK,
        ),
        {TravelMode.WALK},
    )

    assert public.name == "SEOUL_WALK_NETWORK"
    assert collection.routes[0].source == "KAKAO"
    assert "WALK_GRAPH_OUTSIDE_SEOUL" in {
        warning.code for warning in collection.warnings
    }


def test_bootstrap_prefers_public_walk_before_kakao(settings: Settings) -> None:
    providers = build_route_providers(settings)

    assert [provider.name for provider in providers[TravelMode.WALK]] == [
        "SEOUL_WALK_NETWORK",
        "KAKAO_WALK",
    ]


def test_walk_integration_preserves_existing_car_chain(settings: Settings) -> None:
    before = [provider.name for provider in build_car_provider_chain(settings)]
    providers = build_route_providers(settings)
    assert [provider.name for provider in providers[TravelMode.CAR]] == before
```

The first test exercises the unchanged Stage A orchestrator without network I/O. The second freezes the exact Stage C registry order through the unchanged `build_route_providers(settings)` signature.

- [ ] **Step 3: Run provider tests and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_provider.py apps/travel-map/tests/routing/test_walk_provider_chain.py -q`

Expected: FAIL because `SeoulPublicWalkProvider` and the two-provider WALK chain do not exist.

- [ ] **Step 4: Implement stable RouteOption IDs and warning semantics**

```python
from app.policy.coverage import CoverageService
from app.policy.models import CoverageState


class SeoulPublicWalkProvider:
    name = "SEOUL_WALK_NETWORK"
    supported_modes = frozenset({TravelMode.WALK})

    def __init__(
        self,
        router: PublicWalkRouter,
        manifest: SnapshotManifest,
        coverage: CoverageService,
    ) -> None:
        self.router = router
        self.manifest = manifest
        self.coverage = coverage

    @classmethod
    def from_pointer(
        cls, pointer: Path, coverage: CoverageService
    ) -> "SeoulPublicWalkProvider":
        graph = WalkGraph.open(pointer)
        router = PublicWalkRouter(graph, max_snap_distance_meters=150.0)
        return cls(router, graph.manifest, coverage)

    @classmethod
    def from_settings(cls, settings: Settings) -> "SeoulPublicWalkProvider":
        graph = WalkGraph.open(settings.walk_network_pointer)
        if (
            settings.environment == "production"
            and graph.manifest.snapshot_id == "fixture-v1"
        ):
            raise ValueError("production 환경에서 fixture walk snapshot 사용 금지")
        router = PublicWalkRouter(
            graph,
            max_snap_distance_meters=settings.walk_snap_max_distance_meters,
        )
        app_root = Path(__file__).resolve().parents[3]
        coverage = CoverageService.from_resources(app_root / "resources/geodata")
        return cls(router, graph.manifest, coverage)

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if query.mode is not TravelMode.WALK:
            return ProviderResult(
                provider=self.name,
                routes=(),
                warnings=(ProviderWarning(
                    code="UNSUPPORTED_MODE",
                    message="서울 공공 도보 provider는 WALK 요청만 지원합니다.",
                    source=self.name,
                ),),
            )
        if (
            self.coverage.classify(query.origin) is not CoverageState.SEOUL
            or self.coverage.classify(query.destination) is not CoverageState.SEOUL
        ):
            return ProviderResult(
                provider=self.name,
                routes=(),
                warnings=(ProviderWarning(
                    code="WALK_GRAPH_OUTSIDE_SEOUL",
                    message="서울 밖 구간은 보완 도보 제공자를 조회합니다.",
                    source=self.name,
                ),),
            )
        paths = await asyncio.to_thread(
            self.router.route, query.origin, query.destination
        )
        if not paths:
            return ProviderResult(
                provider=self.name,
                routes=(),
                warnings=(ProviderWarning(
                    code="WALK_GRAPH_COVERAGE_MISS",
                    message="공공 보행망에서 연결 가능한 경로를 찾지 못해 보완 제공자를 조회합니다.",
                    source=self.name,
                ),),
            )
        return ProviderResult(
            provider=self.name,
            routes=tuple(self._to_route_option(query, path) for path in paths),
            warnings=(ProviderWarning(
                code="PUBLIC_DATA_2020_BASIS",
                message="서울시 원천 설명상 2020년 기준 보행망입니다.",
                source=self.name,
            ),),
        )
```

`_to_route_option` rounds duration and distance up with `math.ceil`, sets `mobility_cost_krw=0`, `cost_status=CostStatus.KNOWN`, and `cost_breakdown=RouteCostBreakdown()`, uses `manifest.source_as_of`, and creates the ID with SHA-256 of `snapshot_id`, criterion, ordered edge indexes, and origin/destination rounded to five decimals. It deliberately ignores `query.car_assumptions`: fuel type, fuel efficiency, toll, and parking cannot alter a WALK route. Prefix IDs with `route-walk-seoul-` and use the first 16 hex characters. Set route warnings to:

```python
(
    "WALK_DURATION_ESTIMATED",
    "PUBLIC_DATA_2020_BASIS",
    "WALK_ENDPOINTS_SNAPPED_TO_NETWORK",
)
```

- [ ] **Step 5: Add settings and change only the WALK provider chain**

```python
# app/settings.py addition to Settings
walk_network_pointer: Path = Path(__file__).resolve().parents[1] / Path(
    "resources/walk-network/current.json"
)
walk_snap_max_distance_meters: float = 150.0
```

```python
# app/routing/bootstrap.py WALK extension point after Stage C
def build_walk_provider_chain(
    settings: Settings,
) -> tuple[RouteProvider, ...]:
    kakao_walk = KakaoWalkProvider.from_settings(settings)
    public_walk = SeoulPublicWalkProvider.from_settings(settings)
    return (public_walk, kakao_walk)
```

`build_route_providers(settings)`의 TRANSIT와 CAR 표현은 수정하지 않고 WALK key가 이 helper를 호출하는 기존 조립 계약을 유지한다.

Pass `settings.walk_snap_max_distance_meters` into `WalkGraph.snap_pair` through `PublicWalkRouter`; do not retain a second hardcoded 150m value in runtime code.

- [ ] **Step 6: Handle missing or corrupt production snapshots without blocking app startup**

Wrap only public snapshot construction in `build_route_providers`. On `FileNotFoundError`, `ValueError`, `sqlite3.DatabaseError`, or `OSError`, register `(kakao_walk,)` and emit an operational warning code `WALK_SNAPSHOT_UNAVAILABLE` without logging coordinates or secrets. Do not catch `Exception`; programming errors must fail tests and deployment health checks.

```python
def build_walk_provider_chain(
    settings: Settings,
) -> tuple[RouteProvider, ...]:
    kakao_walk = KakaoWalkProvider.from_settings(settings)
    try:
        public_walk = SeoulPublicWalkProvider.from_settings(settings)
    except (FileNotFoundError, ValueError, sqlite3.DatabaseError, OSError) as error:
        logger.warning(
            "public walk snapshot unavailable",
            extra={
                "warning_code": "WALK_SNAPSHOT_UNAVAILABLE",
                "error_type": type(error).__name__,
            },
        )
        return (kakao_walk,)
    return (public_walk, kakao_walk)
```

Add tests for missing pointer, graph hash mismatch, production `fixture-v1` rejection, unsupported mode, and route ID stability. Assert each failure either yields empty public routes or a Kakao-only chain, never a fabricated public route.

Replace the temporary Task 1 export at this point:

```python
# app/providers/seoul_walk/__init__.py
from app.providers.seoul_walk.provider import SeoulPublicWalkProvider

__all__ = ["SeoulPublicWalkProvider"]
```

- [ ] **Step 7: Run provider, fallback, and Stage A orchestrator tests**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_provider.py apps/travel-map/tests/routing/test_walk_provider_chain.py apps/travel-map/tests/routing -q`

Expected: all tests PASS; the in-coverage fixture query uses `SEOUL_WALK_NETWORK`, and the outside/disconnected query uses `KAKAO` with the public warning retained.

- [ ] **Step 8: Commit provider integration**

```bash
git add apps/travel-map/app/providers/seoul_walk apps/travel-map/app/settings.py apps/travel-map/app/routing/bootstrap.py apps/travel-map/tests/providers/seoul_walk/test_provider.py apps/travel-map/tests/routing/test_walk_provider_chain.py
git commit -m "feat(travel-map): prefer public walk routes with Kakao fallback"
```

### Task 8: Add Operations, Regression, Performance, and Deployment Gates

**Files:**
- Create: `apps/travel-map/app/providers/seoul_walk/verification.py`
- Create: `apps/travel-map/scripts/verify_walk_snapshot.py`
- Create: `apps/travel-map/scripts/compare_walk_providers.py`
- Create: `apps/travel-map/resources/walk-network/README.md`
- Modify: `apps/travel-map/Dockerfile`
- Modify: `apps/travel-map/README.md`
- Create: `apps/travel-map/tests/providers/seoul_walk/test_verification.py`
- Modify: `apps/travel-map/tests/routing/test_walk_provider_chain.py`

**Interfaces:**
- Consumes: Tasks 3–7 snapshot, router, provider, and Stage A Kakao provider.
- Produces: reproducible `verify_walk_snapshot.py` JSON report and exit code; opt-in public-vs-Kakao comparison report; release runbook.

- [ ] **Step 1: Write failing verification-report tests**

```python
# tests/providers/seoul_walk/test_verification.py
from pathlib import Path

from app.providers.seoul_walk.verification import verify


def test_verification_report_is_machine_readable(fixture_pointer: Path) -> None:
    report = verify(fixture_pointer, repetitions=20)

    assert report["snapshotId"] == "fixture-v1"
    assert report["integrity"] == "PASS"
    assert report["routeChecks"]["connected"] == "PASS"
    assert report["routeChecks"]["disconnected"] == "PASS"
    assert report["performance"]["p95Milliseconds"] < 500.0
    assert report["result"] == "PASS"
```

- [ ] **Step 2: Run the verification test and verify RED**

Run: `uv run --project apps/travel-map pytest apps/travel-map/tests/providers/seoul_walk/test_verification.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.seoul_walk.verification'`.

- [ ] **Step 3: Implement deterministic integrity, connectivity, and p95 checks**

`verify(pointer, repetitions)` must:

1. call `resolve_current_snapshot` and include every manifest count;
2. select the first and last node of the largest component for a connected path check;
3. if two components exist, assert the router returns no path across them; otherwise record `NOT_APPLICABLE_SINGLE_COMPONENT` without failing;
4. repeat the connected route `repetitions` times with `time.perf_counter_ns`;
5. sort durations and select `ceil(0.95 * n) - 1` for p95;
6. fail when fixture p95 exceeds 500ms or production p95 exceeds 2,000ms;
7. print sorted, indented UTF-8 JSON and return exit 0 only when `result` is `PASS`.

Put this logic in `app.providers.seoul_walk.verification.verify`. `scripts/verify_walk_snapshot.py` is a thin `argparse` wrapper that calls the same function, serializes the returned mapping, and exits from its `result`, so tests and CLI cannot report different logic.

- [ ] **Step 4: Implement the opt-in public-vs-Kakao comparison without logging query coordinates**

`compare_walk_providers.py` reads the approved institution snapshot through Stage A's repository, chooses 30 active `siteId` values deterministically across distinct districts, and pairs each with the next site in the sorted sample. It calls the two providers in-process and writes only these fields per pair:

```json
{
  "originSiteId": "neis:B10:7010057:main",
  "destinationSiteId": "sen:headquarters:main",
  "publicStatus": "ROUTE|NO_ROUTE",
  "kakaoStatus": "ROUTE|NO_ROUTE",
  "publicDistanceMeters": 1200,
  "kakaoDistanceMeters": 1250,
  "distanceRatio": 0.96,
  "publicDurationSeconds": 980,
  "kakaoDurationSeconds": 940
}
```

Do not write names, addresses, coordinates, provider keys, or raw responses. Exit nonzero when fewer than 27 of 30 pairs have public routes, when fewer than 27 pairs have Kakao routes, or when more than 3 comparable pairs have a distance ratio outside `0.5..2.0`. The report is a promotion gate, not an automated claim that Kakao is legally authoritative.

- [ ] **Step 5: Write the exact official-data build and promotion runbook**

`resources/walk-network/README.md` must contain these commands and explain that the operator downloads the CSV and `도보네트워크_링크노드유형코드.xlsx` from the official dataset page into the exact inbox filenames before running them:

```bash
uv run --project apps/travel-map python apps/travel-map/scripts/build_walk_snapshot.py \
  --source-csv apps/travel-map/resources/walk-network/inbox/seoul-walk-network.csv \
  --codebook-xlsx apps/travel-map/resources/walk-network/inbox/walk-link-node-type-codes.xlsx \
  --source-as-of 2026-08-10T00:00:00+09:00 \
  --output-root apps/travel-map/resources/walk-network/snapshots \
  --profile production | tee /tmp/seoul-walk-snapshot-path.txt

read -r WALK_SNAPSHOT_PATH < /tmp/seoul-walk-snapshot-path.txt

uv run --project apps/travel-map python apps/travel-map/scripts/promote_walk_snapshot.py \
  --snapshot-dir "$WALK_SNAPSHOT_PATH" \
  --current-pointer /tmp/seoul-walk-candidate-current.json

uv run --project apps/travel-map python apps/travel-map/scripts/verify_walk_snapshot.py \
  --current-pointer /tmp/seoul-walk-candidate-current.json \
  --repetitions 100

uv run --project apps/travel-map python apps/travel-map/scripts/compare_walk_providers.py \
  --current-pointer /tmp/seoul-walk-candidate-current.json \
  --institution-pointer apps/travel-map/resources/institution-snapshots/current.json \
  --output apps/travel-map/reports/walk-provider-comparison.json

uv run --project apps/travel-map python apps/travel-map/scripts/promote_walk_snapshot.py \
  --snapshot-dir "$WALK_SNAPSHOT_PATH" \
  --current-pointer apps/travel-map/resources/walk-network/current.json
```

The build command writes its one concrete snapshot path to `/tmp/seoul-walk-snapshot-path.txt`, and `read` passes that exact path first to a temporary candidate pointer. Integrity, route, performance, and Kakao comparison gates all run against that temporary pointer; only their success permits the final `current.json` promotion. Also document rollback as promoting the previously verified snapshot directory back to `current.json`, never deleting the current or prior data first.

After a successful promotion, restart or roll the FastAPI deployment once so each worker opens the new immutable graph, then run `/healthz` and one in-coverage WALK request. Existing workers continue using their already-open prior snapshot safely until replaced.

- [ ] **Step 6: Include immutable resources in the Docker image**

Add the following after the existing Stage A application copy steps:

```dockerfile
COPY resources/walk-network/current.json /app/resources/walk-network/current.json
COPY resources/walk-network/snapshots /app/resources/walk-network/snapshots
```

Run: `docker build -t seoul-travel-map:walk-stage-c apps/travel-map`

Expected: exit code 0. Then run `docker run --rm seoul-travel-map:walk-stage-c python scripts/verify_walk_snapshot.py --current-pointer resources/walk-network/current.json --repetitions 20`; expected JSON has `"result": "PASS"`.

- [ ] **Step 7: Run the complete offline verification suite**

Run:

```bash
uv run --project apps/travel-map ruff check apps/travel-map/app apps/travel-map/scripts apps/travel-map/tests
uv run --project apps/travel-map mypy apps/travel-map/app apps/travel-map/scripts
uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map python apps/travel-map/scripts/verify_walk_snapshot.py \
  --current-pointer apps/travel-map/resources/walk-network/current.json \
  --repetitions 20
```

Expected: Ruff `All checks passed!`; mypy `Success: no issues found`; all pytest tests PASS; verification JSON ends with `"result": "PASS"`.

- [ ] **Step 8: Run the production opt-in gate before changing the deployed pointer**

Run the complete runbook gate sequence using the official files and a server-side Kakao key already configured by Stage A. Expected build output is one concrete snapshot directory, verification exits 0, comparison includes 30 pairs and exits 0, and no report contains coordinates or secrets. If any gate fails, do not run the final promotion command; leave the deployed `current.json` unchanged and keep the Stage A Kakao-only WALK chain operational.

- [ ] **Step 9: Commit operations and release gates**

```bash
git add apps/travel-map/app/providers/seoul_walk/verification.py apps/travel-map/scripts/verify_walk_snapshot.py apps/travel-map/scripts/compare_walk_providers.py apps/travel-map/resources/walk-network/README.md apps/travel-map/Dockerfile apps/travel-map/README.md apps/travel-map/tests/providers/seoul_walk/test_verification.py apps/travel-map/tests/routing/test_walk_provider_chain.py
git commit -m "test(travel-map): gate public walk routing releases"
```

## Final Acceptance Checklist

- [ ] Official CSV and codebook hashes, source date, 2020-basis warning, license/source URL, counts, connectivity, duration model, and bidirectional assumption exist in `manifest.json`.
- [ ] Unknown link type, missing endpoint, duplicate ID, malformed WKT, bad length, out-of-bounds coordinate, small production source, weak largest component, bad artifact hash, and corrupt SQLite each block promotion.
- [ ] Both endpoints are `CoverageState.SEOUL` and snap within 150m to nodes in one connected component; otherwise the public provider returns no route.
- [ ] A* returns a fastest and shortest public path when they differ and one path when they are identical.
- [ ] Crosswalk, overpass, underpass, tunnel, and indoor labels affect the explicit estimated-duration model; they never imply barrier-free accessibility.
- [ ] Route geometry contains only official link geometry in traversal order and does not fabricate endpoint connectors.
- [ ] `SeoulPublicWalkProvider` supports only `TravelMode.WALK`, returns zero known mobility cost, and emits stable Stage A `RouteOption` objects.
- [ ] Stage A WALK chain order is public first and Kakao second; outside-Seoul, no-snap, disconnected, missing-snapshot, and corrupt-snapshot cases remain usable through Kakao.
- [ ] Public route failure is never converted to a 0m/0s path and never used as a straight-line legal-distance substitute.
- [ ] Offline CI, fixture snapshot verification, Docker smoke test, 30-pair opt-in comparison, and rollback instructions all pass before production pointer promotion.
- [ ] No raw destination query, address, precise coordinate, Kakao key, Seoul API key, or raw provider response is written to logs or reports.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-10-seoul-public-walk-routing-engine.md`. Execute this plan only after Stage A is merged and its prerequisite contract test passes.

Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, dispatch a fresh subagent per task, and review contract compliance and code quality between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, implement tasks in order, and stop at each commit checkpoint for review.
