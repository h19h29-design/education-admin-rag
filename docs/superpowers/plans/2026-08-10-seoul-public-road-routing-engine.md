# 공공 자동차 경로 엔진 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 국가 표준 노드·링크와 서울 TOPIS를 사용해 서울 및 서울 경계 외곽 12km 지원영역의 자동차 최단시간·최단거리·최저비용 경로를 계산하고, Stage A의 카카오 자동차 경로를 안전한 fallback으로 유지하면서 공공 제공자를 승격할 수 있게 한다.

**Architecture:** 빌드 작업이 공공 도로 원천을 검증·정규화해 버전형 SQLite 그래프로 만들고, 런타임은 이를 읽기 전용으로 적재해 링크 투영 스냅과 A*/Yen 경로 탐색을 수행한다. TOPIS 속도는 짧은 TTL의 가중치 overlay로만 적용하고, 공공 그래프·교통정보가 없거나 품질 게이트를 통과하지 못하면 Stage A의 카카오 `RouteProvider`가 결과를 제공한다. 이 계획은 Stage A의 공통 경로 모델과 제공자 Protocol을 변경하지 않고 `CAR` 구현체로 소비한다.

**Tech Stack:** Python 3.12, FastAPI 앱의 기존 asyncio 런타임, Pydantic v2, httpx, 표준 라이브러리 `sqlite3`/`heapq`/`csv`, 빌드 전용 pyogrio·Shapely·pyproj, pytest·pytest-asyncio·Ruff, Docker

## Global Constraints

- 모든 경로는 저장소 루트 기준 `apps/travel-map/` 아래에 두며 기존 루트 단일 HTML·RAG 코드와 파일을 공유하지 않는다.
- Stage A가 제공하는 `app/routing/models.py`의 `Coordinate`, `TravelMode`, `CostStatus`, `FuelType`, `CarAssumptions`, `RouteCostBreakdown`, `RouteQuery`, `RouteOption`, `ProviderWarning`, `ProviderResult`를 재정의하거나 필드를 추가하지 않는다.
- Stage A가 제공하는 `app/routing/provider.py`의 `RouteProvider` Protocol, 즉 `name: str`, `supported_modes: frozenset[TravelMode]`, `async def get_routes(self, query: RouteQuery) -> ProviderResult`를 그대로 구현한다.
- `RouteQuery`의 필드는 `origin`, `destination`, `depart_at`, `mode`, `car_assumptions`이며 공공 자동차 제공자는 `TravelMode.CAR`만 지원한다.
- `RouteOption`의 필드는 `id`, `mode`, `duration_seconds`, `distance_meters`, `mobility_cost_krw`, `cost_status`, `cost_breakdown`, `geometry`, `source`, `source_as_of`, `warnings`이며 `mobility_cost_krw`는 `int | None`, `cost_breakdown`은 `RouteCostBreakdown | None`, `geometry`는 `tuple[Coordinate, ...]`, `warnings`는 `tuple[str, ...]`다. 비용을 알 수 없을 때는 숫자 0이 아니라 `None`과 `CostStatus.UNKNOWN`을 함께 사용한다.
- 서울 행정경계와 외곽 12km GeoJSON은 Stage A의 승인·해시 검증된 리소스를 소비하며 직선거리나 경계 버퍼를 법령상 12km 거리로 사용하지 않는다.
- 법령 판정용 왕복거리는 `기관→출장지`와 `출장지→기관`의 일반적인 최단 네트워크 경로를 각각 조회해 합산하며 편도 거리의 두 배로 계산하지 않는다.
- 공공 도로 제공자 결과가 없거나 불완전하면 0m·0초·0원 경로를 만들지 않고 명시적 warning과 카카오 fallback을 반환한다.
- 자동차 이동비는 법정 여비와 별도인 예상값이며 `거리 ÷ 사용자가 확인한 기준연비 × 선택 유종의 조회 유가 + 원천에서 확인된 통행료 + 사용자가 입력한 주차비`를 계산한다. 주차비는 모든 후보에 동일한 최종 비용으로 한 번 더하고 경로 탐색 edge weight에는 넣지 않는다.
- 표준 노드·링크만으로 차량통행 제한, 회전제약, 통행료, TOPIS 링크 매핑이 완전하다고 가정하지 않는다. 원천별 필드와 완전성은 live probe report와 graph manifest에 기록하고 안전 필드가 부족하면 `public_primary` 승격을 차단한다.
- 외부 키는 서버 환경변수에만 두고 목적지 원문·정밀 좌표·전체 경로를 로그 또는 비교 리포트에 기록하지 않는다.
- 공개 API 런타임 호출은 timeout, 제한된 재시도, TTL cache, stale-if-error를 사용하고 응답을 스키마 검증한다.
- React, Redis, 사용자 데이터베이스, 관리자 화면, 사용자 식별 쿠키를 추가하지 않는다.

---

## Stage A 소비 계약

이 계획을 시작하기 전에 `cd apps/travel-map && pytest -q`가 통과해야 한다. 구현자는 아래 import가 성공하는지 Task 1의 계약 테스트로 고정하고, Stage B 안에 호환 shim을 만들지 않는다.

```python
from app.routing.models import (
    CarAssumptions,
    Coordinate,
    CostStatus,
    FuelType,
    ProviderResult,
    ProviderWarning,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.provider import RouteProvider
```

Stage A의 `RouteQuery.origin`과 `destination`은 WGS84 `Coordinate(latitude: float, longitude: float)`이고, `depart_at`은 timezone-aware `datetime`이다. `car_assumptions`는 `CarAssumptions(fuel_type: FuelType, efficiency_km_per_liter: float, parking_cost_krw: int) | None`이다. `ProviderResult`는 `provider: str`, `routes: tuple[RouteOption, ...]`, `warnings: tuple[ProviderWarning, ...]`를 가지며 `ProviderWarning`은 `code: str`, `message: str`, `source: str`를 가진다. `RouteOption.cost_breakdown`은 `RouteCostBreakdown(fare_krw=0, fuel_krw=0, toll_krw=0, parking_krw=0) | None`이고, `geometry`에는 시작점부터 끝점까지 WGS84 `Coordinate` tuple을 넣으며 Stage A의 API serializer가 이를 GeoJSON으로 바꾼다. 제공자 등록 지점은 `app/routing/bootstrap.py`의 `build_route_providers(settings) -> dict[TravelMode, tuple[RouteProvider, ...]]`이며 자동차 전용 확장점은 `build_car_provider_chain(settings) -> tuple[RouteProvider, ...]`이다. Stage A의 CAR 기본값은 `(KakaoCarProvider,)`이고 단계 B는 이 helper만 변경하며 `build_walk_provider_chain()`을 수정하지 않는다.

## 파일 구조

```text
apps/travel-map/
├── app/
│   ├── settings.py                                    # Stage A 설정에 도로 제공자 모드 추가
│   ├── routing/bootstrap.py                           # 공공 primary/shadow와 카카오 fallback 순서 등록
│   └── providers/public_road/
│       ├── __init__.py                                # 공개 팩토리 export
│       ├── settings.py                                # 환경변수와 경로 엔진 상수
│       ├── models.py                                  # 도로 그래프·스냅·탐색 내부 타입
│       ├── source_probe.py                            # 공식 원천 실제 필드·완전성 실증
│       ├── compiler.py                                # 공공 원천을 검증된 SQLite로 컴파일
│       ├── graph.py                                   # 읽기 전용 그래프 적재·무결성 확인
│       ├── snap.py                                    # 좌표를 방향성 링크에 투영
│       ├── weights.py                                 # TOPIS/기준속도/비용 edge 가중치
│       ├── search.py                                  # A*와 Yen 복수경로
│       ├── traffic.py                                 # TOPIS 페이지 수집·cache·stale 처리
│       ├── fuel.py                                    # 오피넷 평균유가와 마지막 정상값
│       ├── provider.py                                # Stage A RouteProvider adapter
│       ├── shadow.py                                  # 카카오 반환 + 공공 비식별 shadow 비교
│       └── comparison.py                              # 골드 비교 지표·승격 판정
├── scripts/
│   ├── probe_public_road_sources.py                   # 키가 필요한 opt-in 원천 실증 CLI
│   ├── build_public_road_graph.py                     # 그래프 빌드 CLI
│   └── compare_road_providers.py                      # opt-in 실제 비교 CLI
├── resources/road-network/
│   ├── source-register.json                           # 공식 원천·라이선스·필드 매핑
│   ├── source-contract-report.json                    # live probe 결과와 입력 hash
│   ├── current.json                                   # 활성 snapshot 포인터
│   └── fuel-price-fallback.json                       # 날짜가 표시된 마지막 정상 유가
├── tests/
│   ├── fixtures/public-road/                          # 작은 방향성 그래프·TOPIS·오피넷 fixture
│   ├── providers/public_road/                         # 단위 테스트
│   └── integration/                                   # bootstrap/fallback/왕복 판정 테스트
├── pyproject.toml                                     # build extra와 테스트 설정
├── Dockerfile                                         # 승인 graph snapshot 포함
└── README.md                                          # 데이터 빌드·승격·운영 절차
```

큰 원본 도로 파일과 생성된 운영 SQLite는 Git에 넣지 않는다. 테스트용 10개 이내 노드 fixture와 source register, `current.json`, 유가 fallback만 추적한다. 운영 이미지는 CI가 검증 완료된 snapshot artifact를 내려받고 manifest SHA-256을 확인한 뒤 포함한다.

### Task 1: Stage A 계약과 공공 도로 설정 고정

**Files:**
- Create: `apps/travel-map/app/providers/public_road/__init__.py`
- Create: `apps/travel-map/app/providers/public_road/settings.py`
- Create: `apps/travel-map/tests/providers/public_road/test_contract_and_settings.py`
- Modify: `apps/travel-map/app/settings.py`

**Interfaces:**
- Consumes: Stage A `RouteProvider`, `RouteQuery`, `ProviderResult`, `TravelMode`; `app.settings.Settings`.
- Produces: `RoadProviderMode`, `PublicRoadSettings`, `public_road_settings(settings: Settings) -> PublicRoadSettings`; 환경변수 `ROAD_PROVIDER_MODE`, `PUBLIC_ROAD_GRAPH_POINTER`, `PUBLIC_ROAD_PROMOTION_REPORT`, `TOPIS_API_KEY`, `TOPIS_BASE_URL`, `OPINET_API_KEY`, `OPINET_BASE_URL`.

- [ ] **Step 1: 계약과 설정 기본값의 실패 테스트 작성**

```python
from dataclasses import fields
from pathlib import Path

from app.settings import Settings
from app.providers.public_road.settings import RoadProviderMode, public_road_settings
from app.routing.models import (
    CarAssumptions,
    ProviderResult,
    ProviderWarning,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)


def test_stage_a_route_contract_is_the_only_public_contract() -> None:
    assert {item.name for item in fields(RouteQuery)} == {
        "origin", "destination", "depart_at", "mode", "car_assumptions"
    }
    assert {item.name for item in fields(RouteOption)} == {
        "id", "mode", "duration_seconds", "distance_meters",
        "mobility_cost_krw", "cost_status", "cost_breakdown", "geometry", "source",
        "source_as_of", "warnings"
    }
    assert {item.name for item in fields(CarAssumptions)} == {
        "fuel_type", "efficiency_km_per_liter", "parking_cost_krw"
    }
    assert {item.name for item in fields(RouteCostBreakdown)} == {
        "fare_krw", "fuel_krw", "toll_krw", "parking_krw"
    }
    assert {item.name for item in fields(ProviderWarning)} == {
        "code", "message", "source"
    }
    assert {item.name for item in fields(ProviderResult)} == {
        "provider", "routes", "warnings"
    }
    assert TravelMode.CAR.value == "CAR"


def test_public_road_settings_default_to_kakao(monkeypatch) -> None:
    monkeypatch.delenv("ROAD_PROVIDER_MODE", raising=False)
    settings = public_road_settings(Settings())
    assert settings.mode is RoadProviderMode.KAKAO
    assert settings.graph_pointer == Path("resources/road-network/current.json")
    assert settings.promotion_report == Path("resources/road-network/promotion-report.json")
    assert settings.snap_warn_meters == 100
    assert settings.snap_fail_meters == 500
    assert settings.max_routes_per_objective == 2
```

- [ ] **Step 2: 계약 테스트가 새 모듈 부재로 실패하는지 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_contract_and_settings.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.public_road'`.

- [ ] **Step 3: 환경 설정 타입과 검증 구현**

```python
# app/providers/public_road/settings.py
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.settings import Settings


class RoadProviderMode(StrEnum):
    KAKAO = "kakao"
    PUBLIC_SHADOW = "public_shadow"
    PUBLIC_PRIMARY = "public_primary"


@dataclass(frozen=True)
class PublicRoadSettings:
    mode: RoadProviderMode
    graph_pointer: Path
    promotion_report: Path
    topis_api_key: str | None
    topis_base_url: str
    opinet_api_key: str | None
    opinet_base_url: str
    snap_warn_meters: int = 100
    snap_fail_meters: int = 500
    traffic_ttl_seconds: int = 300
    traffic_stale_seconds: int = 900
    provider_timeout_seconds: float = 3.0
    max_routes_per_objective: int = 2


def public_road_settings(settings: Settings) -> PublicRoadSettings:
    return PublicRoadSettings(
        mode=RoadProviderMode(settings.road_provider_mode),
        graph_pointer=Path(settings.public_road_graph_pointer),
        promotion_report=Path(settings.public_road_promotion_report),
        topis_api_key=settings.topis_api_key,
        topis_base_url=settings.topis_base_url.rstrip("/"),
        opinet_api_key=settings.opinet_api_key,
        opinet_base_url=settings.opinet_base_url.rstrip("/"),
    )
```

`app/settings.py`의 기존 Pydantic `Settings`에 다음 필드를 정확히 추가한다.

```python
road_provider_mode: str = "kakao"
public_road_graph_pointer: str = "resources/road-network/current.json"
public_road_promotion_report: str = "resources/road-network/promotion-report.json"
topis_api_key: str | None = None
topis_base_url: str = "https://openapi.seoul.go.kr:8088"
opinet_api_key: str | None = None
opinet_base_url: str = "https://www.opinet.co.kr/api"
```

- [ ] **Step 4: 설정 테스트가 통과하는지 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_contract_and_settings.py -q`

Expected: `2 passed`.

- [ ] **Step 5: 타입·lint 회귀 확인**

Run: `cd apps/travel-map && ruff check app/providers/public_road app/settings.py tests/providers/public_road/test_contract_and_settings.py`

Expected: `All checks passed!`.

- [ ] **Step 6: 계약과 설정 커밋**

```bash
git add apps/travel-map/app/settings.py apps/travel-map/app/providers/public_road apps/travel-map/tests/providers/public_road/test_contract_and_settings.py
git commit -m "feat(travel-map): define public road provider settings"
```

### Task 2: 공식 도로·TOPIS 원천 계약 실증 게이트

**Files:**
- Create: `apps/travel-map/app/providers/public_road/source_probe.py`
- Create: `apps/travel-map/scripts/probe_public_road_sources.py`
- Create: `apps/travel-map/resources/road-network/source-register.json`
- Create: `apps/travel-map/tests/fixtures/public-road/probe-nodes.geojson`
- Create: `apps/travel-map/tests/fixtures/public-road/probe-links.geojson`
- Create: `apps/travel-map/tests/fixtures/public-road/probe-turns.csv`
- Create: `apps/travel-map/tests/fixtures/public-road/probe-topis-mapping.csv`
- Create: `apps/travel-map/tests/fixtures/public-road/probe-topis-response.json`
- Create: `apps/travel-map/tests/providers/public_road/test_source_probe.py`

**Interfaces:**
- Consumes: [ITS 국가교통정보센터 표준 노드·링크](https://www.its.go.kr/opendata/intro) 배포 파일, 서울시 교통소통 표준링크 매핑 파일, [서울시 실시간 도로 소통 정보](https://data.seoul.go.kr/dataList/datasetView.do?currentPageNo=1&infId=OA-13291&serviceKind=1&srvType=A)의 실제 키 응답.
- Produces: `probe_sources(nodes_path: Path, links_path: Path, turns_path: Path | None, topis_mapping_path: Path, topis_sample: Mapping[str, object]) -> SourceProbeReport`; `source-contract-report.json`; `SourceProbeReport.build_allowed`, `primary_eligible`, `field_mapping`, `vehicle_access_coverage`, `turn_restriction_coverage`, `topis_mapping_coverage`, `toll_coverage`, `failures`.

- [ ] **Step 1: 확인된 필드와 누락 안전필드의 실패 테스트 작성**

```python
from app.providers.public_road.source_probe import probe_sources


def test_probe_maps_core_fields_and_reports_safety_coverage(fixture_dir):
    report = probe_sources(
        nodes_path=fixture_dir / "probe-nodes.geojson",
        links_path=fixture_dir / "probe-links.geojson",
        turns_path=fixture_dir / "probe-turns.csv",
        topis_mapping_path=fixture_dir / "probe-topis-mapping.csv",
        topis_sample=load_json(fixture_dir / "probe-topis-response.json"),
    )
    assert report.build_allowed is True
    assert report.primary_eligible is True
    assert report.field_mapping["topis.speed"] == "PRCS_SPD"
    assert report.vehicle_access_coverage == 1.0
    assert report.turn_restriction_coverage == 1.0
    assert report.topis_mapping_coverage == 1.0


def test_probe_blocks_primary_when_turn_contract_is_missing(fixture_dir):
    report = probe_sources(
        nodes_path=fixture_dir / "probe-nodes.geojson",
        links_path=fixture_dir / "probe-links.geojson",
        turns_path=None,
        topis_mapping_path=fixture_dir / "probe-topis-mapping.csv",
        topis_sample=load_json(fixture_dir / "probe-topis-response.json"),
    )
    assert report.build_allowed is True
    assert report.primary_eligible is False
    assert "TURN_RESTRICTIONS_UNVERIFIED" in report.failures
```

- [ ] **Step 2: probe 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_source_probe.py -q`

Expected: FAIL with missing `source_probe` module.

- [ ] **Step 3: alias가 아니라 실제 발견 필드를 고정하는 probe 구현**

원천별 허용 후보는 node ID `NODE_ID|node_id`, link ID `LINK_ID|link_id`, 시작/끝 `F_NODE|f_node`/`T_NODE|t_node`, 길이 `LENGTH|length`, 제한속도 `MAX_SPD|max_spd`, 도로등급 `ROAD_RANK|road_rank`, 차량제한 `REST_VEH|rest_veh`, 회전 진입/진출 `IN_LINK_ID|in_link_id`/`OUT_LINK_ID|out_link_id`, 매핑 service/standard `SERVICE_LINK_ID|service_link_id`/`STANDARD_LINK_ID|standard_link_id`, TOPIS `LINK_ID`, `PRCS_SPD`, `PRCS_TRV_TIME`다. 후보 중 실제 존재하는 이름 하나를 report에 기록하고 compiler는 report에 기록된 이름만 사용한다. core ID·양 끝·geometry·양수 길이 중 하나라도 없으면 `build_allowed=False`; 차량접근 또는 회전제약의 의미·코드표가 source register와 일치하지 않으면 `primary_eligible=False`다.

```python
@dataclass(frozen=True)
class SourceProbeReport:
    schema_version: int
    probed_at: datetime
    build_allowed: bool
    primary_eligible: bool
    field_mapping: dict[str, str]
    vehicle_access_coverage: float
    turn_restriction_coverage: float
    topis_mapping_coverage: float
    toll_coverage: float
    topis_request_mode: str
    failures: tuple[str, ...]
    input_sha256: dict[str, str]
```

`topis_request_mode`는 무필터 1,000건 probe가 성공하면 `BULK`, 공식 link별 sample만 성공하면 `PER_LINK`, 둘 다 실패하면 `UNAVAILABLE`이다. `primary_eligible`은 `vehicle_access_coverage == 1.0`, `turn_restriction_coverage == 1.0`, TOPIS sample에서 링크·속도·여행시간 확인, 표준링크 매핑 coverage `>=0.90`을 모두 만족할 때만 true다. 통행료 coverage는 별도로 기록하고 1.0 미만이면 비용에 확인된 통행료만 더하며 `TOLL_COVERAGE_INCOMPLETE`를 경고한다.

- [ ] **Step 4: source register에 원천과 검증 상태를 명시**

`source-register.json`에는 ITS 원천 URL, 서울 TrafficInfo 데이터셋 URL, 서울 교통소통 표준링크 매핑 데이터셋 URL, 각 원천의 라이선스 URL, field 후보와 의미 코드표, `verificationState: "LIVE_PROBE_REQUIRED"`를 기록한다. 키·샘플 원문 응답·다운로드 토큰은 저장하지 않는다.

- [ ] **Step 5: opt-in live probe CLI 구현**

```python
parser.add_argument("--nodes", type=Path, required=True)
parser.add_argument("--links", type=Path, required=True)
parser.add_argument("--turns", type=Path)
parser.add_argument("--topis-mapping", type=Path, required=True)
parser.add_argument("--topis-sample-link-id", required=True)
parser.add_argument("--topis-fixture", type=Path)
parser.add_argument("--output", type=Path, required=True)
```

CLI는 `TOPIS_API_KEY`를 환경에서 읽고 공식 문서의 link별 endpoint `/{key}/json/TrafficInfo/1/1/{link_id}`를 먼저 호출한다. JSON이 지원되지 않으면 `xml` endpoint를 호출해 같은 세 필드를 확인한다. endpoint나 정확 필드가 실응답과 다르면 `TOPIS_CONTRACT_UNVERIFIED`를 report에 기록하고 exit code 3으로 종료한다. API 키가 없으면 외부 호출 전에 exit code 2와 `TOPIS_API_KEY is required for live source probe`만 출력한다.

- [ ] **Step 6: fixture probe와 누락 필드 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_source_probe.py -q`

Expected: `6 passed`; core 누락은 build 차단, 차량·회전 누락은 primary만 차단, 통행료 누락은 경고로 구분.

- [ ] **Step 7: fixture CLI report 생성 확인**

Run: `cd apps/travel-map && python scripts/probe_public_road_sources.py --nodes tests/fixtures/public-road/probe-nodes.geojson --links tests/fixtures/public-road/probe-links.geojson --turns tests/fixtures/public-road/probe-turns.csv --topis-mapping tests/fixtures/public-road/probe-topis-mapping.csv --topis-sample-link-id L-AB --topis-fixture tests/fixtures/public-road/probe-topis-response.json --output /tmp/public-road-source-contract.json`

Expected: exit code 0, `buildAllowed=true`, `primaryEligible=true`, input SHA-256 5개가 출력되고 API 키를 요구하지 않음. `--topis-fixture`는 `tests/fixtures/public-road` 아래 경로만 허용하며 앱 런타임에서는 노출하지 않는다.

- [ ] **Step 8: 원천 실증 게이트 커밋**

```bash
git add apps/travel-map/app/providers/public_road/source_probe.py apps/travel-map/scripts/probe_public_road_sources.py apps/travel-map/resources/road-network/source-register.json apps/travel-map/tests/fixtures/public-road/probe-nodes.geojson apps/travel-map/tests/fixtures/public-road/probe-links.geojson apps/travel-map/tests/fixtures/public-road/probe-turns.csv apps/travel-map/tests/fixtures/public-road/probe-topis-mapping.csv apps/travel-map/tests/fixtures/public-road/probe-topis-response.json apps/travel-map/tests/providers/public_road/test_source_probe.py
git commit -m "feat(travel-map): gate public road source contracts"
```

### Task 3: 공공 노드·링크를 결정론적 SQLite snapshot으로 컴파일

**Files:**
- Create: `apps/travel-map/app/providers/public_road/models.py`
- Create: `apps/travel-map/app/providers/public_road/compiler.py`
- Create: `apps/travel-map/scripts/build_public_road_graph.py`
- Create: `apps/travel-map/tests/fixtures/public-road/nodes.geojson`
- Create: `apps/travel-map/tests/fixtures/public-road/links.geojson`
- Create: `apps/travel-map/tests/fixtures/public-road/turns.csv`
- Create: `apps/travel-map/tests/fixtures/public-road/topis-mapping.csv`
- Create: `apps/travel-map/tests/fixtures/public-road/source-contract-report.json`
- Create: `apps/travel-map/tests/fixtures/public-road/support-area.geojson`
- Create: `apps/travel-map/tests/providers/public_road/test_compiler.py`
- Modify: `apps/travel-map/pyproject.toml`

**Interfaces:**
- Consumes: Task 2의 승인 `SourceProbeReport`와 그 report에 기록된 실제 필드명·입력 SHA-256; Stage A 승인 지원영역 GeoJSON; 노드·링크·회전제약·TOPIS service↔standard link 매핑 원본.
- Produces: `compile_graph(nodes_path: Path, links_path: Path, turns_path: Path | None, topis_mapping_path: Path, source_contract_path: Path, support_area_path: Path, output_dir: Path, source_as_of: date) -> GraphManifest`; `output_dir / manifest.snapshot_id / "road-graph.sqlite3"`, 같은 디렉터리의 `manifest.json`, `output_dir / "current.json"`.

- [ ] **Step 1: 단방향·지원영역 clipping·manifest 실패 테스트 작성**

```python
import json
import sqlite3
from datetime import date

from app.providers.public_road.compiler import compile_graph


def test_compile_graph_keeps_directed_links_and_excludes_outside(tmp_path, fixture_dir):
    manifest = compile_graph(
        nodes_path=fixture_dir / "nodes.geojson",
        links_path=fixture_dir / "links.geojson",
        turns_path=fixture_dir / "turns.csv",
        topis_mapping_path=fixture_dir / "topis-mapping.csv",
        source_contract_path=fixture_dir / "source-contract-report.json",
        support_area_path=fixture_dir / "support-area.geojson",
        output_dir=tmp_path,
        source_as_of=date(2026, 8, 1),
    )
    database = tmp_path / manifest.snapshot_id / "road-graph.sqlite3"
    with sqlite3.connect(database) as connection:
        links = connection.execute(
            "SELECT link_id, from_node_id, to_node_id FROM links ORDER BY link_id"
        ).fetchall()
    assert links == [
        ("L-AB", "A", "B"),
        ("L-BC", "B", "C"),
        ("L-CB", "C", "B"),
        ("L-CD", "C", "D"),
    ]
    saved = json.loads((tmp_path / manifest.snapshot_id / "manifest.json").read_text())
    assert saved["schemaVersion"] == 1
    assert saved["sourceAsOf"] == "2026-08-01"
    assert saved["nodeCount"] == 4
    assert saved["linkCount"] == 4
    assert saved["vehicleAccessCoverage"] == 1.0
    assert saved["turnRestrictionCoverage"] == 1.0
    assert saved["primaryEligible"] is True
    assert len(saved["sha256"]) == 64
```

Fixture에는 지원영역 안 A-B-C-D와 영역 밖 E를 두고, 링크 레코드 하나가 한 방향 edge 하나라는 원천 의미를 보존한다. source contract는 `REST_VEH="0"`만 승용차 통행 가능으로 정의한다. `REST_VEH="1"` 링크와 양 끝 노드가 없는 링크를 넣어 rejection count가 각각 1인지, `L-AB→L-BC` 허용과 `L-AB→L-BX` 금지 회전이 turns table에 저장되는지 별도 assertion한다.

- [ ] **Step 2: 컴파일러 테스트의 import 실패 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_compiler.py -q`

Expected: FAIL with `ModuleNotFoundError` for `compiler`.

- [ ] **Step 3: 내부 graph 타입과 SQLite schema 구현**

```python
# app/providers/public_road/models.py
from dataclasses import dataclass
from datetime import date, datetime

from app.routing.models import Coordinate


@dataclass(frozen=True)
class GraphManifest:
    snapshot_id: str
    schema_version: int
    source_as_of: date
    node_count: int
    link_count: int
    rejected_link_count: int
    vehicle_access_coverage: float
    turn_restriction_coverage: float
    topis_mapping_coverage: float
    toll_coverage: float
    primary_eligible: bool
    sha256: str


@dataclass(frozen=True)
class RoadNode:
    node_id: str
    coordinate: Coordinate


@dataclass(frozen=True)
class RoadEdge:
    link_id: str
    source_link_id: str
    from_node_id: str
    to_node_id: str
    length_meters: float
    max_speed_kph: float
    road_rank: str
    toll_krw: int
    toll_known: bool
    topis_link_id: str | None
    inside_seoul: bool
    coordinates: tuple[Coordinate, ...]


@dataclass(frozen=True)
class TrafficSnapshot:
    observed_at: datetime
    speed_kph_by_link: dict[str, float]
    stale: bool
    limited: bool


@dataclass(frozen=True)
class FuelPriceQuote:
    price_krw_per_liter: int
    stale: bool
    observed_on: date | None = None
```

SQLite schema는 `metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)`, `nodes(node_id TEXT PRIMARY KEY, latitude REAL, longitude REAL)`, `links(link_id TEXT PRIMARY KEY, from_node_id TEXT, to_node_id TEXT, length_meters REAL, max_speed_kph REAL, road_rank TEXT, toll_krw INTEGER, toll_known INTEGER, topis_link_id TEXT, inside_seoul INTEGER, geometry_json TEXT)`, `turns(in_link_id TEXT, out_link_id TEXT, allowed INTEGER, PRIMARY KEY(in_link_id, out_link_id))`로 고정한다. compiler는 Shapely로 support polygon과 link geometry의 교차 여부를 확인하고 좌표를 EPSG:4326으로 변환한다. 차량 접근은 Task 2 report의 코드표로 `allowed`가 확인된 링크만 포함하고, 양 끝 노드 존재·양수 길이·두 점 이상 geometry도 검증한다. 회전제약이 없는 원천을 `모든 회전 허용`으로 해석하지 않으며 manifest의 `primaryEligible`을 false로 둔다. 통행료 필드가 확인된 링크만 `toll_known=1`이고 나머지는 0원으로 계산하되 `toll_known=0`을 보존한다. `RoadGraph`가 base edge를 만들 때 `source_link_id=link_id`를 넣고 query-local split edge도 같은 `source_link_id`를 보존한다.

- [ ] **Step 4: 결정론적 SQLite와 빌드 CLI 구현**

compiler는 table과 row를 ID 순서로 삽입하고 `PRAGMA page_size=4096`, `journal_mode=DELETE`, `auto_vacuum=NONE`, `VACUUM`을 적용한다. 생성시각은 SQLite에 넣지 않고 manifest에만 기록하며 snapshot ID는 입력 hash·sourceAsOf·schema version으로 계산한다. CLI는 다음 고정 옵션을 제공한다.

```python
parser.add_argument("--nodes", type=Path, required=True)
parser.add_argument("--links", type=Path, required=True)
parser.add_argument("--turns", type=Path)
parser.add_argument("--topis-mapping", type=Path, required=True)
parser.add_argument("--source-contract", type=Path, required=True)
parser.add_argument("--support-area", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--source-as-of", type=date.fromisoformat, required=True)
```

`pyproject.toml`의 build dependency group에 `pyogrio>=0.10,<1`, `shapely>=2.0,<3`, `pyproj>=3.7,<4`를 추가한다. 런타임 dependency에는 이 세 패키지를 넣지 않는다.

- [ ] **Step 5: fixture graph를 컴파일해 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_compiler.py -q`

Expected: compiler assertions including `4 links`, `2 rejected links`, 회전제약 2건, 안정적인 SHA-256 all PASS.

- [ ] **Step 6: 같은 입력의 snapshot ID와 hash가 같은지 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_compiler.py::test_same_inputs_produce_same_snapshot_id_and_hash -q`

Expected: 두 실행 모두 동일한 `snapshotId`와 `sha256`를 출력하고 exit code 0.

- [ ] **Step 7: 컴파일러 커밋**

```bash
git add apps/travel-map/app/providers/public_road/models.py apps/travel-map/app/providers/public_road/compiler.py apps/travel-map/scripts/build_public_road_graph.py apps/travel-map/tests/fixtures/public-road/nodes.geojson apps/travel-map/tests/fixtures/public-road/links.geojson apps/travel-map/tests/fixtures/public-road/turns.csv apps/travel-map/tests/fixtures/public-road/topis-mapping.csv apps/travel-map/tests/fixtures/public-road/source-contract-report.json apps/travel-map/tests/fixtures/public-road/support-area.geojson apps/travel-map/tests/providers/public_road/test_compiler.py apps/travel-map/pyproject.toml
git commit -m "feat(travel-map): compile public road graph snapshots"
```

### Task 4: snapshot 무결성 검증과 읽기 전용 graph 적재

**Files:**
- Create: `apps/travel-map/app/providers/public_road/graph.py`
- Create: `apps/travel-map/tests/providers/public_road/test_graph.py`

**Interfaces:**
- Consumes: Task 3 `road-graph.sqlite3`, `manifest.json`, `current.json`, `GraphManifest`, `RoadNode`, `RoadEdge`.
- Produces: `RoadGraph.open(pointer: Path) -> RoadGraph`; `RoadGraph.node(node_id: str) -> RoadNode`; `RoadGraph.outgoing(node_id: str) -> tuple[RoadEdge, ...]`; `RoadGraph.turn_allowed(in_link_id: str | None, out_link_id: str) -> bool`; `RoadGraph.candidate_edges(coordinate: Coordinate, radius_meters: int) -> tuple[RoadEdge, ...]`; `GraphIntegrityError`.

- [ ] **Step 1: 정상 적재와 손상 거부 실패 테스트 작성**

```python
import json

import pytest

from app.providers.public_road.graph import GraphIntegrityError, RoadGraph


def test_open_verifies_hash_and_builds_directed_adjacency(compiled_graph):
    graph = RoadGraph.open(compiled_graph.pointer)
    assert [edge.link_id for edge in graph.outgoing("B")] == ["L-BC"]
    assert [edge.link_id for edge in graph.outgoing("C")] == ["L-CB", "L-CD"]
    assert graph.turn_allowed("L-AB", "L-BC") is True
    assert graph.turn_allowed("L-AB", "L-BX") is False
    assert graph.manifest.node_count == 4


def test_open_rejects_tampered_database(compiled_graph):
    current = json.loads(compiled_graph.pointer.read_text())
    database = compiled_graph.pointer.parent / current["snapshotId"] / "road-graph.sqlite3"
    database.write_bytes(database.read_bytes() + b"tampered")
    with pytest.raises(GraphIntegrityError, match="SHA-256 mismatch"):
        RoadGraph.open(compiled_graph.pointer)
```

- [ ] **Step 2: graph 테스트가 미구현 import로 실패하는지 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_graph.py -q`

Expected: FAIL with `ModuleNotFoundError` for `graph`.

- [ ] **Step 3: hash/schema 검증과 adjacency 적재 구현**

`RoadGraph.open()`은 포인터의 상대 snapshot 경로가 `resources/road-network` 밖으로 나가지 못하게 `Path.resolve().is_relative_to()`로 검사하고, manifest schema version 1과 실제 DB SHA-256을 확인한 후 SQLite URI `mode=ro&immutable=1`로 연다. node와 link를 정렬된 순서로 읽어 `dict[str, tuple[RoadEdge, ...]]` adjacency, `(in_link_id, out_link_id) -> allowed` 회전표, 0.01도 cell 기반 edge bounding-box index를 만든다. FK 누락, 중복 ID, 비양수 길이, 좌표 범위 오류, turns가 참조하는 미존재 link는 `GraphIntegrityError`로 startup 전에 거부한다. manifest가 회전제약 완전성을 확인한 경우 명시된 금지 회전은 false, 나머지는 true이고, 미확인 manifest는 shadow에서만 탐색하도록 `primary_eligible=False`를 유지한다.

```python
@classmethod
def open(cls, pointer: Path) -> "RoadGraph":
    pointer_path = pointer.resolve(strict=True)
    root = pointer_path.parent
    current = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_dir = (root / current["snapshotId"]).resolve(strict=True)
    if not snapshot_dir.is_relative_to(root):
        raise GraphIntegrityError("snapshot path escapes road-network root")
    manifest = _load_manifest(snapshot_dir / "manifest.json")
    database = snapshot_dir / "road-graph.sqlite3"
    if _sha256(database) != manifest.sha256:
        raise GraphIntegrityError("SHA-256 mismatch for road graph")
    return cls._from_database(database, manifest)
```

- [ ] **Step 4: 정상·손상·path traversal 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_graph.py -q`

Expected: `4 passed`; 회전표, 손상, `../` 포인터는 각각 기대값 또는 명시적 `GraphIntegrityError`.

- [ ] **Step 5: graph 적재 커밋**

```bash
git add apps/travel-map/app/providers/public_road/graph.py apps/travel-map/tests/providers/public_road/test_graph.py
git commit -m "feat(travel-map): load verified public road graphs"
```

### Task 5: 좌표를 링크에 투영하고 query-local 가상 노드 생성

**Files:**
- Create: `apps/travel-map/app/providers/public_road/snap.py`
- Create: `apps/travel-map/tests/providers/public_road/test_snap.py`

**Interfaces:**
- Consumes: `RoadGraph.candidate_edges`, `Coordinate`, directed `RoadEdge` geometry; `PublicRoadSettings.snap_warn_meters`, `snap_fail_meters`.
- Produces: `snap_to_edge(graph: RoadGraph, coordinate: Coordinate, fail_meters: int) -> SnappedPoint`; `build_query_overlay(graph: RoadGraph, origin: SnappedPoint, destination: SnappedPoint) -> QueryGraph`; `SnapError`; `SnappedPoint(edge_id, fraction, projected, offset_meters)`.

- [ ] **Step 1: 중간 링크 투영·단방향·동일 링크 실패 테스트 작성**

```python
import pytest

from app.providers.public_road.snap import SnapError, build_query_overlay, snap_to_edge
from app.routing.models import Coordinate


def test_snap_projects_to_middle_of_directed_edge(compiled_road_graph):
    point = Coordinate(latitude=37.50005, longitude=127.00500)
    snapped = snap_to_edge(compiled_road_graph, point, fail_meters=500)
    assert snapped.edge_id == "L-AB"
    assert snapped.fraction == pytest.approx(0.5, abs=0.02)
    assert snapped.offset_meters < 10


def test_overlay_preserves_one_way_when_both_points_share_edge(compiled_road_graph):
    first = snap_to_edge(compiled_road_graph, Coordinate(37.5, 127.002), 500)
    second = snap_to_edge(compiled_road_graph, Coordinate(37.5, 127.008), 500)
    overlay = build_query_overlay(compiled_road_graph, first, second)
    assert overlay.has_edge("__origin__", "__destination__")
    assert not overlay.has_edge("__destination__", "__origin__")


def test_snap_rejects_point_farther_than_limit(compiled_road_graph):
    with pytest.raises(SnapError, match="more than 500m"):
        snap_to_edge(compiled_road_graph, Coordinate(37.7, 127.3), 500)
```

- [ ] **Step 2: snap 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_snap.py -q`

Expected: FAIL with missing `snap` module.

- [ ] **Step 3: WGS84 segment projection과 가상 edge 분할 구현**

각 candidate polyline segment를 국소 equirectangular 미터 좌표로 투영해 가장 가까운 점과 전체 link 길이 기준 fraction을 구한다. 동률은 `offset_meters`, `link_id`, segment index 순으로 고른다. query overlay는 원래 graph를 변경하지 않고 `__origin__`, `__destination__` 가상 노드를 추가하고 directed edge의 앞·뒤 길이, 시간, 확인된 통행료를 fraction으로 분할한다. 통행료는 진입 쪽 split에 한 번만 붙이고 두 좌표가 같은 링크면 진행 방향에 맞는 직접 가상 edge를 만든다. 모든 split edge는 원본의 `source_link_id`를 보존해 Task 8의 회전제약 검사가 가상 edge ID가 아니라 공식 link ID를 사용하게 한다.

```python
@dataclass(frozen=True)
class SnappedPoint:
    edge_id: str
    fraction: float
    projected: Coordinate
    offset_meters: float


def snap_to_edge(graph: RoadGraph, coordinate: Coordinate, fail_meters: int) -> SnappedPoint:
    candidates = graph.candidate_edges(coordinate, fail_meters)
    ranked = sorted(
        (_project_onto_edge(coordinate, edge) for edge in candidates),
        key=lambda item: (item.offset_meters, item.edge_id, item.fraction),
    )
    if not ranked or ranked[0].offset_meters > fail_meters:
        raise SnapError(f"nearest car link is more than {fail_meters}m away")
    return ranked[0]
```

- [ ] **Step 4: snap 경계와 방향성 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_snap.py -q`

Expected: `3 passed`.

- [ ] **Step 5: snap 구현 커밋**

```bash
git add apps/travel-map/app/providers/public_road/snap.py apps/travel-map/tests/providers/public_road/test_snap.py
git commit -m "feat(travel-map): snap car routes to directed links"
```

### Task 6: TOPIS 실시간 속도 수집과 stale-if-error cache

**Files:**
- Create: `apps/travel-map/app/providers/public_road/traffic.py`
- Create: `apps/travel-map/tests/fixtures/public-road/topis-link-response.json`
- Create: `apps/travel-map/tests/fixtures/public-road/topis-bulk-response.json`
- Create: `apps/travel-map/tests/fixtures/public-road/topis-error.json`
- Create: `apps/travel-map/tests/providers/public_road/test_traffic.py`

**Interfaces:**
- Consumes: `PublicRoadSettings.topis_base_url`, `topis_api_key`, timeout/TTL; Task 2 report의 실제 endpoint type·필드 매핑·`topis_request_mode`; `TrafficSnapshot`.
- Produces: `TopisTrafficClient.get_snapshot(link_ids: frozenset[str], now: datetime) -> TrafficSnapshot | None`; warning codes `TOPIS_KEY_MISSING`, `TOPIS_UNAVAILABLE`, `TOPIS_STALE`, `TOPIS_SCHEMA_INVALID`, `TOPIS_LINK_LIMITED`.

- [ ] **Step 1: link별/BULK 모드·schema·stale fallback 실패 테스트 작성**

```python
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.providers.public_road.traffic import TopisTrafficClient


@pytest.mark.asyncio
async def test_topis_parses_link_speed_and_observed_time(
    topis_transport, topis_per_link_source_contract
):
    client = TopisTrafficClient(
        api_key="test-key",
        base_url="https://openapi.seoul.go.kr:8088",
        http=httpx.AsyncClient(transport=topis_transport),
        source_contract=topis_per_link_source_contract,
        ttl_seconds=300,
        stale_seconds=900,
    )
    snapshot = await client.get_snapshot(
        frozenset({"SVC-AB", "SVC-BC"}),
        datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    assert snapshot is not None
    assert snapshot.speed_kph_by_link == {"SVC-AB": 28.0, "SVC-BC": 16.5}
    assert snapshot.observed_at == datetime(2026, 8, 10, 0, 59, tzinfo=UTC)
    assert snapshot.stale is False


@pytest.mark.asyncio
async def test_topis_returns_recent_stale_snapshot_after_timeout(cached_topis_client):
    now = datetime(2026, 8, 10, 1, 6, tzinfo=UTC)
    cached_topis_client.fail_next_request(httpx.ReadTimeout("timeout"))
    snapshot = await cached_topis_client.get_snapshot(frozenset({"SVC-AB"}), now)
    assert snapshot is not None
    assert snapshot.stale is True
    assert now - snapshot.observed_at < timedelta(seconds=900)
```

- [ ] **Step 2: TOPIS 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_traffic.py -q`

Expected: FAIL with missing `traffic` module.

- [ ] **Step 3: source probe가 승인한 요청형식만 사용하는 client 구현**

`BULK`이면 Task 2가 실증한 `/{key}/{type}/TrafficInfo/{start}/{end}/`를 page size 1,000으로 호출하고 요청된 link ID만 남긴다. `PER_LINK`이면 공식 sample 형식 `/{key}/{type}/TrafficInfo/1/1/{link_id}`를 최대 64개, 동시성 8로 호출한다. `UNAVAILABLE`이면 네트워크 호출 없이 `None`이다. parser는 report에 기록된 필드명만 읽고 `RESULT.CODE == "INFO-000"`, 요청 link 일치, `0 < speed <= 160`, 여행시간 양수를 검증한다. 응답에 관측시각 필드가 공식적으로 없으면 HTTP `Date` header를 사용하고, 그것도 없으면 응답 완료시각을 `observed_at`으로 표시한다. 마지막 정상값은 link별 300초 fresh/900초 stale cache로 저장한다.

```python
async def get_snapshot(self, link_ids: frozenset[str], now: datetime) -> TrafficSnapshot | None:
    requested = frozenset(sorted(link_ids)[:64])
    cached = self._cached_values(requested, now)
    missing = requested - cached.keys()
    if not missing:
        return self._to_snapshot(cached, now)
    try:
        fetched = await self._fetch(missing, now)
    except (httpx.TimeoutException, httpx.HTTPError, TopisSchemaError):
        fetched = {}
    combined = self._stale_values(requested, now) | cached | fetched
    return None if not combined else self._to_snapshot(combined, now)
```

2회 요청은 100ms 고정 backoff로 한 번만 재시도한다. 64개를 넘긴 link는 호출하지 않고 반환 snapshot에 `limited=True`를 기록해 provider가 `TOPIS_LINK_LIMITED`를 표시한다. cache key와 운영 metric에는 link ID 개수만 기록하고 ID 목록은 로그로 남기지 않는다.

- [ ] **Step 4: TOPIS 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_traffic.py -q`

Expected: BULK와 PER_LINK, invalid zero speed, timeout retry, 64개 제한, stale expiry를 포함해 `7 passed`.

- [ ] **Step 5: traffic client 커밋**

```bash
git add apps/travel-map/app/providers/public_road/traffic.py apps/travel-map/tests/fixtures/public-road/topis-link-response.json apps/travel-map/tests/fixtures/public-road/topis-bulk-response.json apps/travel-map/tests/fixtures/public-road/topis-error.json apps/travel-map/tests/providers/public_road/test_traffic.py
git commit -m "feat(travel-map): overlay TOPIS traffic speeds"
```

### Task 7: 기준속도·실시간속도·최저비용 edge 가중치

**Files:**
- Create: `apps/travel-map/app/providers/public_road/weights.py`
- Create: `apps/travel-map/tests/providers/public_road/test_weights.py`

**Interfaces:**
- Consumes: Task 3 `RoadEdge`, `TrafficSnapshot`, `FuelPriceQuote`; Stage A `CarAssumptions.efficiency_km_per_liter`.
- Produces: `SearchObjective` values `FASTEST`, `SHORTEST`, `CHEAPEST`; `EdgeWeights(duration_seconds, distance_meters, fuel_cost_krw, toll_krw, variable_cost_krw)`; `weight_edge(edge, traffic, fuel_price: FuelPriceQuote | None, efficiency_km_per_liter: float) -> EdgeWeights`.

- [ ] **Step 1: 서울 TOPIS와 외곽 기준속도 실패 테스트 작성**

```python
import pytest

from app.providers.public_road.models import FuelPriceQuote
from app.providers.public_road.weights import weight_edge


def test_topis_speed_is_clamped_to_road_limit(seoul_edge, traffic_snapshot):
    traffic_snapshot.speed_kph_by_link[seoul_edge.topis_link_id] = 120.0
    weights = weight_edge(seoul_edge, traffic_snapshot, FuelPriceQuote(1700, False), 10.0)
    assert weights.duration_seconds == pytest.approx(
        seoul_edge.length_meters / (seoul_edge.max_speed_kph / 3.6)
    )


def test_outside_edge_uses_road_rank_factor(outside_local_edge):
    weights = weight_edge(outside_local_edge, None, FuelPriceQuote(1700, False), 10.0)
    expected_speed_kph = max(10.0, outside_local_edge.max_speed_kph * 0.45)
    assert weights.duration_seconds == pytest.approx(
        outside_local_edge.length_meters / (expected_speed_kph / 3.6)
    )
    assert weights.variable_cost_krw == pytest.approx(
        outside_local_edge.length_meters / 1000 / 10 * 1700 + outside_local_edge.toll_krw
    )
```

- [ ] **Step 2: weights 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_weights.py -q`

Expected: FAIL with missing `weights` module.

- [ ] **Step 3: 고정된 도로등급 factor와 비음수 가중치 구현**

```python
ROAD_RANK_SPEED_FACTORS = {
    "101": 0.85,
    "102": 0.75,
    "103": 0.70,
    "104": 0.60,
    "105": 0.50,
    "106": 0.45,
    "107": 0.45,
}
DEFAULT_SPEED_FACTOR = 0.50
MIN_SPEED_KPH = 10.0
def weight_edge(edge: RoadEdge, traffic: TrafficSnapshot | None, fuel_price: FuelPriceQuote | None, efficiency_km_per_liter: float) -> EdgeWeights:
    if not 1.0 <= efficiency_km_per_liter <= 50.0:
        raise ValueError("efficiency_km_per_liter must be between 1 and 50")
    observed = None if traffic is None or edge.topis_link_id is None else traffic.speed_kph_by_link.get(edge.topis_link_id)
    if edge.inside_seoul and observed is not None:
        speed_kph = min(edge.max_speed_kph, max(MIN_SPEED_KPH, observed))
    else:
        factor = ROAD_RANK_SPEED_FACTORS.get(edge.road_rank, DEFAULT_SPEED_FACTOR)
        speed_kph = max(MIN_SPEED_KPH, edge.max_speed_kph * factor)
    fuel_cost = 0.0 if fuel_price is None else (
        edge.length_meters / 1000 / efficiency_km_per_liter
    ) * fuel_price.price_krw_per_liter
    return EdgeWeights(
        duration_seconds=edge.length_meters / (speed_kph / 3.6),
        distance_meters=edge.length_meters,
        fuel_cost_krw=fuel_cost,
        toll_krw=edge.toll_krw,
        variable_cost_krw=fuel_cost + edge.toll_krw,
    )
```

유가가 `None`이면 FASTEST·SHORTEST 탐색용 cost만 0으로 두고 CHEAPEST 탐색은 호출하지 않는다. 확인되지 않은 통행료는 compiler가 `toll_krw=0`, `toll_known=False`로 보존하므로 산식에는 더하지 않고 provider가 `TOLL_COVERAGE_INCOMPLETE`를 표시한다.

- [ ] **Step 4: 등급 전체와 경계값 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_weights.py -q`

Expected: `6 passed`, 모든 duration/distance/cost가 양수이고 외곽 edge에 TOPIS 값이 적용되지 않음.

- [ ] **Step 5: 가중치 커밋**

```bash
git add apps/travel-map/app/providers/public_road/weights.py apps/travel-map/tests/providers/public_road/test_weights.py
git commit -m "feat(travel-map): calculate public road route weights"
```

### Task 8: A*와 Yen 알고리즘으로 세 목적의 복수 경로 탐색

**Files:**
- Create: `apps/travel-map/app/providers/public_road/search.py`
- Create: `apps/travel-map/tests/providers/public_road/test_search.py`

**Interfaces:**
- Consumes: `QueryGraph`, `RoadEdge`, `EdgeWeights`, `SearchObjective`, `TrafficSnapshot`, `FuelPriceQuote`.
- Produces: `find_path(graph, origin_id, destination_id, objective, traffic, fuel_price: FuelPriceQuote | None, efficiency_km_per_liter: float, blocked_edges=frozenset(), blocked_nodes=frozenset()) -> RoadPath | None`; `find_alternatives(..., objective, efficiency_km_per_liter, limit: int) -> tuple[RoadPath, ...]`; `RoadPath(edge_ids, coordinates, duration_seconds, distance_meters, fuel_cost_krw, toll_krw, variable_cost_krw)`.

- [ ] **Step 1: 목적별 최적경로·단방향·복수경로 실패 테스트 작성**

```python
from app.providers.public_road.search import find_alternatives, find_path
from app.providers.public_road.models import FuelPriceQuote
from app.providers.public_road.weights import SearchObjective


def test_objectives_choose_different_routes(query_graph, traffic_snapshot):
    fuel = FuelPriceQuote(price_krw_per_liter=1700, stale=False)
    fastest = find_path(query_graph, "__origin__", "__destination__", SearchObjective.FASTEST, traffic_snapshot, fuel, 10.0)
    shortest = find_path(query_graph, "__origin__", "__destination__", SearchObjective.SHORTEST, traffic_snapshot, fuel, 10.0)
    cheapest = find_path(query_graph, "__origin__", "__destination__", SearchObjective.CHEAPEST, traffic_snapshot, fuel, 10.0)
    assert fastest.edge_ids == ("L-FAST-1", "L-FAST-2")
    assert shortest.edge_ids == ("L-SHORT-1", "L-SHORT-2")
    assert cheapest.edge_ids == ("L-FREE-1", "L-FREE-2")


def test_yen_returns_unique_loopless_paths_in_weight_order(query_graph, traffic_snapshot):
    paths = find_alternatives(
        query_graph,
        "__origin__",
        "__destination__",
        SearchObjective.FASTEST,
        traffic_snapshot,
        FuelPriceQuote(1700, False),
        10.0,
        limit=2,
    )
    assert len(paths) == 2
    assert paths[0].duration_seconds <= paths[1].duration_seconds
    assert len({path.edge_ids for path in paths}) == 2


def test_search_rejects_prohibited_turn(query_graph, traffic_snapshot):
    path = find_path(
        query_graph,
        "__origin__",
        "__destination__",
        SearchObjective.SHORTEST,
        traffic_snapshot,
        FuelPriceQuote(1700, False),
        10.0,
    )
    assert ("L-AB", "L-BX") not in set(zip(path.edge_ids, path.edge_ids[1:]))
```

- [ ] **Step 2: search 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_search.py -q`

Expected: FAIL with missing `search` module.

- [ ] **Step 3: admissible heuristic를 쓰는 결정론적 A* 구현**

FASTEST heuristic는 대권거리/그래프 최고 제한속도, SHORTEST는 대권거리, CHEAPEST는 `대권거리(km) ÷ query.efficiency_km_per_liter × 리터당 가격`을 쓰고 통행료 하한은 0원으로 둔다. 세 heuristic은 실제 edge cost를 넘지 않는지 synthetic graph로 검증한다. 탐색 state는 `(node_id, incoming_source_link_id)`로 구성해 `graph.turn_allowed(incoming_source_link_id, edge.source_link_id)`가 false인 전이를 버린다. heap 동점 키는 `(estimated_total, accumulated_weight, node_id, incoming_source_link_id or "")`로 고정한다. edge와 node block set을 받아 Yen spur search가 같은 함수를 사용하게 한다.

CHEAPEST objective에 `fuel_price=None`이 들어오면 `ValueError("fuel price is required for CHEAPEST search")`로 즉시 거부한다. FASTEST·SHORTEST는 유가 없이도 탐색할 수 있다.

```python
def _objective_value(weights: EdgeWeights, objective: SearchObjective) -> float:
    if objective is SearchObjective.FASTEST:
        return weights.duration_seconds
    if objective is SearchObjective.SHORTEST:
        return weights.distance_meters
    return weights.variable_cost_krw


def find_path(graph, origin_id, destination_id, objective, traffic, fuel_price, efficiency_km_per_liter, blocked_edges=frozenset(), blocked_nodes=frozenset()):
    origin_state = (origin_id, None)
    frontier = [(0.0, 0.0, origin_id, "", origin_state)]
    best = {origin_state: 0.0}
    previous: dict[tuple[str, str | None], tuple[tuple[str, str | None], RoadEdge]] = {}
    while frontier:
        _, accumulated, node_id, _, state = heappop(frontier)
        if node_id == destination_id:
            return _reconstruct(
                previous, origin_state, state, traffic,
                fuel_price, efficiency_km_per_liter,
            )
        if accumulated != best.get(state):
            continue
        for edge in graph.outgoing(node_id):
            edge_key = (edge.from_node_id, edge.to_node_id, edge.link_id)
            if edge_key in blocked_edges or edge.to_node_id in blocked_nodes or not graph.turn_allowed(state[1], edge.source_link_id):
                continue
            next_state = (edge.to_node_id, edge.source_link_id)
            next_cost = accumulated + _objective_value(
                weight_edge(edge, traffic, fuel_price, efficiency_km_per_liter), objective
            )
            if next_cost < best.get(next_state, float("inf")):
                best[next_state] = next_cost
                previous[next_state] = (state, edge)
                estimate = next_cost + _heuristic(
                    graph, edge.to_node_id, destination_id, objective,
                    fuel_price, efficiency_km_per_liter,
                )
                heappush(frontier, (estimate, next_cost, edge.to_node_id, edge.source_link_id, next_state))
    return None
```

- [ ] **Step 4: Yen k-shortest와 95% 중복 제거 구현**

각 objective에서 최대 2개 loopless path를 구한다. 이전 경로의 각 spur 위치마다 동일 root를 공유하는 다음 edge를 차단하고 A*를 재실행한다. edge ID 집합 Jaccard가 `>= 0.95`인 후보는 같은 경로로 보고 버리며 `(objective weight, duration, distance, edge_ids)`로 정렬한다. FASTEST·SHORTEST·CHEAPEST 결과를 합칠 때도 같은 중복 규칙을 사용한다.

- [ ] **Step 5: 검색·도달불가·결정론 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_search.py -q`

Expected: `8 passed`; 금지회전이 경로에 없고, 역방향 단방향 요청은 `None`, 같은 입력 20회 결과의 edge ID 순서가 동일.

- [ ] **Step 6: 경로 탐색 커밋**

```bash
git add apps/travel-map/app/providers/public_road/search.py apps/travel-map/tests/providers/public_road/test_search.py
git commit -m "feat(travel-map): search alternative public car routes"
```

### Task 9: 오피넷 평균유가와 자동차 예상 이동비 산정

**Files:**
- Create: `apps/travel-map/app/providers/public_road/fuel.py`
- Create: `apps/travel-map/resources/road-network/fuel-price-fallback.json`
- Create: `apps/travel-map/tests/fixtures/public-road/opinet-average.json`
- Create: `apps/travel-map/tests/fixtures/public-road/opinet-fallback.json`
- Create: `apps/travel-map/tests/providers/public_road/test_fuel.py`

**Interfaces:**
- Consumes: `PublicRoadSettings.opinet_base_url`, `opinet_api_key`; Task 3 `FuelPriceQuote`.
- Produces: `OpinetFuelPriceClient.get_quote(fuel_type: FuelType, now: datetime) -> FuelPriceQuote`; fallback JSON `{enabled, quotes: [{fuelType, priceKrwPerLiter, observedOn, source}]}`; warning states represented by `FuelPriceQuote.stale`.

- [ ] **Step 1: 정상 유가·fallback 만료 실패 테스트 작성**

```python
from datetime import UTC, datetime

import pytest

from app.providers.public_road.fuel import FuelPriceUnavailable, OpinetFuelPriceClient
from app.routing.models import FuelType


@pytest.mark.asyncio
async def test_opinet_reads_gasoline_national_average(opinet_client):
    quote = await opinet_client.get_quote(
        FuelType.GASOLINE, datetime(2026, 8, 10, tzinfo=UTC)
    )
    assert quote.price_krw_per_liter == 1700
    assert quote.observed_on.isoformat() == "2026-08-10"
    assert quote.stale is False


@pytest.mark.asyncio
async def test_opinet_uses_dated_fallback_for_at_most_seven_days(failing_opinet_client):
    quote = await failing_opinet_client.get_quote(
        FuelType.GASOLINE, datetime(2026, 8, 16, tzinfo=UTC)
    )
    assert quote.price_krw_per_liter == 1700
    assert quote.stale is True
    with pytest.raises(FuelPriceUnavailable):
        await failing_opinet_client.get_quote(
            FuelType.GASOLINE, datetime(2026, 8, 18, tzinfo=UTC)
        )
```

- [ ] **Step 2: fuel 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_fuel.py -q`

Expected: FAIL with missing `fuel` module.

- [ ] **Step 3: 오피넷 응답 검증과 일 단위 cache 구현**

`FuelType.GASOLINE -> B027`, `DIESEL -> D047`, `LPG -> K015`로 매핑해 `/avgAllPrice.do?out=json&prodcd={product_code}&code={api_key}`를 3초 timeout으로 호출하고 `RESULT.OIL[].PRODCD`가 요청 코드와 일치하는 행의 `PRICE`와 `TRADE_DT`를 읽는다. 가격은 `500..5000`원/L, 날짜는 미래가 아니어야 한다. 네트워크·schema 실패 시 fallback의 `enabled=true`이고 동일 유종 `observedOn`이 7일 이내일 때만 stale quote를 반환한다. 저장소 운영 파일은 `{"enabled": false, "quotes": []}`로 시작한다. 테스트는 `tests/fixtures/public-road/opinet-fallback.json`의 GASOLINE `2026-08-10`, `1700`, `OPINET_TEST_FIXTURE`를 명시적으로 주입한다. CI가 실제 마지막 정상 응답의 날짜·값·원천 hash를 승인한 경우에만 운영 파일의 `enabled`를 true로 갱신한다.

- [ ] **Step 4: 가격과 이동비 산술 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_fuel.py tests/providers/public_road/test_weights.py -q`

Expected: `9 passed`; 14,600m, 10km/L, 1,700원/L, 통행료 1,000원은 `3,482원`으로 반올림됨.

- [ ] **Step 5: 유가 제공자 커밋**

```bash
git add apps/travel-map/app/providers/public_road/fuel.py apps/travel-map/resources/road-network/fuel-price-fallback.json apps/travel-map/tests/fixtures/public-road/opinet-average.json apps/travel-map/tests/fixtures/public-road/opinet-fallback.json apps/travel-map/tests/providers/public_road/test_fuel.py
git commit -m "feat(travel-map): estimate car cost with Opinet prices"
```

### Task 10: Stage A `RouteProvider` adapter로 정규 경로 반환

**Files:**
- Create: `apps/travel-map/app/providers/public_road/provider.py`
- Modify: `apps/travel-map/app/providers/public_road/__init__.py`
- Create: `apps/travel-map/tests/providers/public_road/test_provider.py`

**Interfaces:**
- Consumes: Stage A `RouteQuery`, `CarAssumptions`, `FuelType`, `RouteCostBreakdown`, `RouteOption`, `ProviderResult`, `ProviderWarning`, `CostStatus`, `TravelMode`; Tasks 3–9 graph, snap, traffic, fuel, search.
- Produces: `PublicRoadProvider` satisfying `RouteProvider`; `create_public_road_provider(settings: PublicRoadSettings, http: httpx.AsyncClient) -> PublicRoadProvider`; `name == "PUBLIC_ROAD_TOPIS"`, `supported_modes == frozenset({TravelMode.CAR})`.

- [ ] **Step 1: 정상 복수경로와 지원하지 않는 mode 실패 테스트 작성**

```python
from datetime import UTC, datetime

import pytest

from app.routing.models import (
    CarAssumptions,
    Coordinate,
    CostStatus,
    FuelType,
    RouteQuery,
    TravelMode,
)


@pytest.mark.asyncio
async def test_provider_returns_normalized_rankable_car_routes(public_road_provider):
    result = await public_road_provider.get_routes(RouteQuery(
        origin=Coordinate(37.5000, 127.0000),
        destination=Coordinate(37.5100, 127.0200),
        depart_at=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
        mode=TravelMode.CAR,
        car_assumptions=CarAssumptions(
            fuel_type=FuelType.GASOLINE,
            efficiency_km_per_liter=10.0,
            parking_cost_krw=3000,
        ),
    ))
    assert len(result.routes) >= 3
    assert {route.mode for route in result.routes} == {TravelMode.CAR}
    assert all(route.source == "PUBLIC_ROAD_TOPIS" for route in result.routes)
    assert all(isinstance(route.geometry, tuple) for route in result.routes)
    assert all(len(route.geometry) >= 2 for route in result.routes)
    assert all(route.cost_status is CostStatus.ESTIMATED for route in result.routes)
    assert all(route.cost_breakdown.parking_krw == 3000 for route in result.routes)
    assert all(
        route.mobility_cost_krw
        == route.cost_breakdown.fuel_krw
        + route.cost_breakdown.toll_krw
        + route.cost_breakdown.parking_krw
        for route in result.routes
    )
    assert len({route.id for route in result.routes}) == len(result.routes)


@pytest.mark.asyncio
async def test_provider_rejects_non_car_without_zero_route(public_road_provider):
    query = make_route_query(mode=TravelMode.WALK)
    result = await public_road_provider.get_routes(query)
    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["UNSUPPORTED_MODE"]
```

- [ ] **Step 2: adapter 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_provider.py -q`

Expected: FAIL with missing `provider` module.

- [ ] **Step 3: provider orchestration과 stable route ID 구현**

`get_routes()`는 `query.car_assumptions`의 유종·연비·주차비를 사용한다. 값이 `None`이면 `GASOLINE`, `10.0km/L`, `0원`을 적용하고 `CAR_ASSUMPTIONS_DEFAULTED`를 표시한다. 두 좌표를 snap한 query graph에서 먼저 기준속도로 FASTEST·SHORTEST를 계산하고, 유가가 있으면 CHEAPEST도 계산한다. baseline 후보 edge의 TOPIS service link ID를 경로 기여거리 내림차순·ID 오름차순으로 최대 64개 뽑아 Task 6 client와 선택 유종의 유가 client를 병렬 호출한 뒤, traffic overlay가 있으면 세 objective를 다시 계산하고 95% 중복을 제거한다. route ID는 `sha256(snapshot_id + depart_at의 5분 bucket + edge_ids + traffic observed_at)[:20]`으로 만든다. `RouteCostBreakdown`은 `fare_krw=0`, 반올림한 `fuel_krw`, 확인된 `toll_krw`, 입력 `parking_krw`를 분리하고 합계를 `mobility_cost_krw`로 넣는다. 같은 주차비는 경로 순위를 바꾸지 않는다. `geometry`는 시작점부터 끝점까지 WGS84 `Coordinate` tuple이다. offset이 100m 초과면 `SNAP_DISTANCE_HIGH`, TOPIS가 없으면 `TRAFFIC_BASELINE_USED`, stale 유가면 `FUEL_PRICE_STALE`, 통행료 coverage가 1.0 미만이면 `TOLL_COVERAGE_INCOMPLETE`를 표시한다.

```python
class PublicRoadProvider:
    name = "PUBLIC_ROAD_TOPIS"
    supported_modes = frozenset({TravelMode.CAR})

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if query.mode is not TravelMode.CAR:
            warning = ProviderWarning(
                code="UNSUPPORTED_MODE",
                message="public road provider supports CAR only",
                source=self.name,
            )
            return ProviderResult(provider=self.name, routes=(), warnings=(warning,))
        try:
            origin = snap_to_edge(self._graph, query.origin, self._settings.snap_fail_meters)
            destination = snap_to_edge(self._graph, query.destination, self._settings.snap_fail_meters)
        except SnapError as error:
            warning = ProviderWarning(
                code="ROAD_SNAP_FAILED",
                message=str(error),
                source=self.name,
            )
            return ProviderResult(provider=self.name, routes=(), warnings=(warning,))
        assumptions = query.car_assumptions or CarAssumptions(
            fuel_type=FuelType.GASOLINE,
            efficiency_km_per_liter=10.0,
            parking_cost_krw=0,
        )
        graph = build_query_overlay(self._graph, origin, destination)
        fuel_task = asyncio.create_task(
            self._fuel.get_quote(assumptions.fuel_type, query.depart_at)
        )
        baseline_paths = self._collect_paths(
            graph, traffic=None, fuel=None,
            efficiency_km_per_liter=assumptions.efficiency_km_per_liter,
        )
        topis_link_ids = self._select_topis_links(graph, baseline_paths, limit=64)
        traffic_result, fuel_result = await asyncio.gather(
            self._traffic.get_snapshot(topis_link_ids, query.depart_at),
            fuel_task,
            return_exceptions=True,
        )
        traffic = None if isinstance(traffic_result, BaseException) else traffic_result
        fuel = None if isinstance(fuel_result, BaseException) else fuel_result
        paths = self._collect_paths(
            graph, traffic, fuel,
            efficiency_km_per_liter=assumptions.efficiency_km_per_liter,
        )
        return self._to_provider_result(
            query, assumptions, origin, destination, paths, traffic, fuel
        )
```

정규 경로 변환은 다음 산식을 한 곳에서 적용한다. 유가가 없으면 부분합을 총 이동비처럼 보이지 않도록 `cost_breakdown=None`, `mobility_cost_krw=None`, `CostStatus.UNKNOWN`으로 둔다.

```python
def _to_route_option(self, path, assumptions, fuel, source_as_of, warnings):
    if fuel is None:
        breakdown = None
        total_cost = None
        cost_status = CostStatus.UNKNOWN
    else:
        breakdown = RouteCostBreakdown(
            fare_krw=0,
            fuel_krw=int(round(path.fuel_cost_krw)),
            toll_krw=int(round(path.toll_krw)),
            parking_krw=assumptions.parking_cost_krw,
        )
        total_cost = (
            breakdown.fuel_krw + breakdown.toll_krw + breakdown.parking_krw
        )
        cost_status = CostStatus.ESTIMATED
    return RouteOption(
        id=self._stable_route_id(path, source_as_of),
        mode=TravelMode.CAR,
        duration_seconds=max(1, int(round(path.duration_seconds))),
        distance_meters=max(1, int(round(path.distance_meters))),
        mobility_cost_krw=total_cost,
        cost_status=cost_status,
        cost_breakdown=breakdown,
        geometry=path.coordinates,
        source=self.name,
        source_as_of=source_as_of,
        warnings=tuple(sorted(warnings)),
    )
```

- [ ] **Step 4: provider 실패 상태를 개별 warning으로 구현**

graph 미적재는 `ROAD_GRAPH_UNAVAILABLE`, snap 실패는 `ROAD_SNAP_FAILED`, 경로 없음은 `ROAD_PATH_NOT_FOUND`, TOPIS schema 오류는 `TRAFFIC_BASELINE_USED`, 유가 없음은 FASTEST·SHORTEST route를 유지하되 `mobility_cost_krw=None`, `cost_status=UNKNOWN`, `FUEL_PRICE_UNAVAILABLE`로 반환한다. 유가가 있더라도 통행료 coverage 또는 기준속도 사용 때문에 공공 자동차 비용은 `CostStatus.ESTIMATED`이고, 모든 확인필드가 완전하다는 이유로 `KNOWN`으로 승격하지 않는다. duration이나 distance가 0인 path, 좌표 두 개 미만 geometry tuple, NaN은 `ROAD_RESULT_INVALID`로 폐기한다.

- [ ] **Step 5: adapter 정상·부분실패 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_provider.py -q`

Expected: `12 passed`; three fuel types, 사용자 연비·주차비 breakdown, default assumptions, two-pass TOPIS link 선택, graph/snap/path/traffic/fuel/toll 각 실패가 0값 거리·시간 경로 없이 정확한 warning을 반환.

- [ ] **Step 6: Stage A Protocol 정적 호환 검사 실행**

Run: `cd apps/travel-map && python -c 'import inspect; from app.providers.public_road.provider import PublicRoadProvider; assert list(inspect.signature(PublicRoadProvider.get_routes).parameters) == ["self", "query"]; print("RouteProvider signature OK")'`

Expected: `RouteProvider signature OK` and exit code 0. `test_contract_and_settings.py`의 `accepts_provider(provider: RouteProvider) -> RouteProvider` helper에 `public_road_provider`를 전달하는 테스트도 PASS.

- [ ] **Step 7: provider adapter 커밋**

```bash
git add apps/travel-map/app/providers/public_road/__init__.py apps/travel-map/app/providers/public_road/provider.py apps/travel-map/tests/providers/public_road/test_provider.py
git commit -m "feat(travel-map): expose public road route provider"
```

### Task 11: Stage A bootstrap에 public shadow/primary와 카카오 fallback 연결

**Files:**
- Create: `apps/travel-map/app/providers/public_road/shadow.py`
- Modify: `apps/travel-map/app/routing/bootstrap.py`
- Create: `apps/travel-map/tests/providers/public_road/test_shadow.py`
- Create: `apps/travel-map/tests/integration/test_public_road_bootstrap.py`

**Interfaces:**
- Consumes: Stage A `build_route_providers(settings) -> dict[TravelMode, tuple[RouteProvider, ...]]`, `build_car_provider_chain(settings) -> tuple[RouteProvider, ...]`, 기존 `(KakaoCarProvider,)`, generic orchestrator의 순차 empty-result fallback; Task 1 `RoadProviderMode`; Task 10 `PublicRoadProvider`.
- Produces: 비식별 `ShadowAggregate(route_count, fastest_seconds, shortest_meters, cheapest_krw)`; `ComparisonSink` Protocol의 `record(public: ShadowAggregate, kakao: ShadowAggregate) -> None`, `record_failure(code: str) -> None`; `PublicRoadShadowProvider(kakao: RouteProvider, public: RouteProvider, comparison_sink: ComparisonSink) -> RouteProvider`; mode별 CAR tuple: `kakao -> (KakaoCarProvider,)`, `public_shadow -> (PublicRoadShadowProvider,)`, `public_primary -> (PublicRoadProvider, KakaoCarProvider)`.

- [ ] **Step 1: 세 운영모드와 shadow 비교 실패 테스트 작성**

```python
import pytest

from app.providers.public_road.shadow import PublicRoadShadowProvider
from app.routing.bootstrap import build_route_providers
from app.routing.models import TravelMode


@pytest.mark.asyncio
async def test_shadow_returns_kakao_and_compares_public(route_query, fake_public, fake_kakao, comparison_sink):
    provider = PublicRoadShadowProvider(fake_kakao, fake_public, comparison_sink)
    result = await provider.get_routes(route_query)
    assert result.provider == fake_kakao.result.provider
    assert result.routes == fake_kakao.result.routes
    assert comparison_sink.calls[0][0].shortest_meters == min(
        route.distance_meters for route in fake_public.result.routes
    )
    assert comparison_sink.calls[0][1].shortest_meters == min(
        route.distance_meters for route in fake_kakao.result.routes
    )


def test_bootstrap_orders_public_primary_before_kakao(primary_settings, monkeypatch):
    providers = build_route_providers(primary_settings)
    assert [provider.name for provider in providers[TravelMode.CAR]] == [
        "PUBLIC_ROAD_TOPIS", "KAKAO_CAR"
    ]


def test_bootstrap_kakao_mode_does_not_construct_public(kakao_settings, public_factory_spy):
    providers = build_route_providers(kakao_settings)
    assert [provider.name for provider in providers[TravelMode.CAR]] == ["KAKAO_CAR"]
    assert public_factory_spy.call_count == 0


def test_bootstrap_blocks_unapproved_public_primary(unapproved_primary_settings):
    providers = build_route_providers(unapproved_primary_settings)
    assert [provider.name for provider in providers[TravelMode.CAR]] == [
        "PUBLIC_ROAD_UNAVAILABLE", "KAKAO_CAR"
    ]
```

- [ ] **Step 2: bootstrap/shadow 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_shadow.py tests/integration/test_public_road_bootstrap.py -q`

Expected: FAIL with missing `shadow` module or CAR tuple mismatch.

- [ ] **Step 3: 개인정보를 남기지 않는 shadow provider 구현**

`PublicRoadShadowProvider.get_routes()`는 카카오와 공공 provider를 `asyncio.gather(..., return_exceptions=True)`로 병렬 호출한다. 반환값은 항상 카카오 `ProviderResult`다. 두 결과가 모두 유효하면 거리·시간·비용 aggregate만 `ComparisonSink.record()`에 전달하며 query 좌표, 기관명, 목적지, geometry, route ID는 넘기지 않는다. 공공 예외나 empty result는 `comparison_sink.record_failure(code)`만 호출하고 카카오 결과에 shadow용 warning을 추가하지 않는다.

```python
class PublicRoadShadowProvider:
    name = "KAKAO_CAR_WITH_PUBLIC_SHADOW"
    supported_modes = frozenset({TravelMode.CAR})

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        kakao_result, public_result = await asyncio.gather(
            self._kakao.get_routes(query),
            self._public.get_routes(query),
            return_exceptions=True,
        )
        if isinstance(kakao_result, BaseException):
            raise kakao_result
        if isinstance(public_result, BaseException) or not public_result.routes:
            self._sink.record_failure("PUBLIC_SHADOW_UNAVAILABLE")
            return kakao_result
        self._sink.record(_aggregate(public_result), _aggregate(kakao_result))
        return kakao_result
```

- [ ] **Step 4: `build_car_provider_chain`의 CAR tuple을 설정별로 구성**

`app/routing/bootstrap.py`에서 `build_car_provider_chain(settings)`만 수정하고 TRANSIT tuple과 `build_walk_provider_chain(settings)`는 변경하지 않는다. KAKAO는 public graph를 열지 않는다. PUBLIC_SHADOW는 source probe나 promotion 승인 여부와 관계없이 `(PublicRoadShadowProvider(kakao, public, sink),)`로 비교자료를 모은다. PUBLIC_PRIMARY는 graph manifest의 `primaryEligible=true`이고 `PUBLIC_ROAD_PROMOTION_REPORT`의 `approved=true`, `graphSnapshotId`가 현재 graph와 일치할 때만 `(public, kakao)`를 반환한다. 하나라도 아니면 `(PublicRoadUnavailableProvider(code="PUBLIC_ROAD_NOT_PROMOTED"), kakao)`다. graph pointer 무결성 오류는 unavailable provider가 `ProviderWarning(code="ROAD_GRAPH_UNAVAILABLE", message="configured road graph failed integrity validation", source="PUBLIC_ROAD_TOPIS")`를 반환하므로 앱 startup과 카카오 fallback을 막지 않는다. `build_route_providers(settings)`는 기존처럼 이 helper 반환값을 CAR key에 넣는다.

- [ ] **Step 5: bootstrap 순서와 shadow 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_shadow.py tests/integration/test_public_road_bootstrap.py -q`

Expected: `8 passed`; 각 mode의 provider call count, 승인/미승인 tuple 순서, 반환 `provider`가 기대값과 일치.

`test_public_road_bootstrap.py`에는 `build_walk_provider_chain`의 반환 이름 목록이 CAR mode 변경 전후 동일하다는 회귀 테스트를 포함한다. 단계 C가 먼저 적용된 경우에도 공공 도보→카카오 도보 순서를 유지해야 한다.

- [ ] **Step 6: public primary empty-result의 generic fallback 확인**

Run: `cd apps/travel-map && pytest tests/integration/test_public_road_bootstrap.py::test_orchestrator_uses_kakao_after_empty_public_result -q`

Expected: PASS; 공공 결과가 empty일 때 Stage A orchestrator가 두 번째 `KAKAO_CAR`를 호출하고 최종 route source가 카카오이며 `ROAD_GRAPH_UNAVAILABLE` warning을 보존.

- [ ] **Step 7: 기존 전체 provider 회귀 테스트 실행**

Run: `cd apps/travel-map && pytest tests/routing tests/providers tests/integration/test_public_road_bootstrap.py -q`

Expected: all PASS; TRANSIT와 WALK provider tuple과 fixture 결과가 변경되지 않음.

- [ ] **Step 8: bootstrap 통합 커밋**

```bash
git add apps/travel-map/app/providers/public_road/shadow.py apps/travel-map/app/routing/bootstrap.py apps/travel-map/tests/providers/public_road/test_shadow.py apps/travel-map/tests/integration/test_public_road_bootstrap.py
git commit -m "feat(travel-map): register public road provider fallback"
```

### Task 12: 법령 판정용 비대칭 왕복 최단거리 통합 검증

**Files:**
- Modify: `apps/travel-map/app/routing/bootstrap.py`
- Modify: `apps/travel-map/app/services/trip_preview.py`
- Create: `apps/travel-map/tests/integration/test_public_road_round_trip.py`

**Interfaces:**
- Consumes: Stage A `POST /api/v1/trips/preview`; `TripPreviewService.preview(request: TripPreviewRequest) -> TripPreviewResponse`; `build_classification_provider(settings) -> RouteProvider`; 카카오 판정 fallback `KakaoCarProvider(priority="DISTANCE", alternatives=False)`; Task 11의 검증된 `PublicRoadProvider`.
- Produces: 회귀 계약 `classificationDistanceMeters = outbound shortest distance + return shortest distance`; 사용자 선택 route가 판정용 route를 바꾸지 않음.

- [ ] **Step 1: 일방통행으로 왕복거리가 다른 API 실패 테스트 작성**

```python
def test_classification_uses_two_directional_shortest_paths(client, public_road_app):
    response = client.post("/api/v1/trips/preview", json={
        "originSiteId": "neis:B10:fixture-school:main",
        "destination": {
            "name": "경계 인접 출장지",
            "address": "경기도 fixture 1",
            "latitude": 37.5100,
            "longitude": 126.9000,
        },
        "startsAt": "2026-08-10T09:00:00+09:00",
        "returnsAt": "2026-08-10T11:00:00+09:00",
        "policyProfile": "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED",
        "vehicleUse": "NONE",
        "hasOtherLocalTripsToday": False,
        "previousAllowanceKrw": 0,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["classificationDistanceMeters"] == 11_999
    assert body["classification"] == "LOCAL"
    assert public_road_app.queries[0].origin != public_road_app.queries[1].origin


def test_exact_twelve_kilometers_is_non_local_expected(client, public_road_app):
    public_road_app.set_directional_shortest_distances(5_100, 6_900)
    response = client.post(
        "/api/v1/trips/preview",
        json=valid_preview_request(destination=buffer_destination()),
    )
    assert response.status_code == 200
    assert response.json()["classificationDistanceMeters"] == 12_000
    assert response.json()["classification"] == "NON_LOCAL_EXPECTED"


def test_seoul_destination_keeps_local_but_uses_distance_for_two_km(
    client, public_road_app
):
    public_road_app.set_directional_shortest_distances(900, 1_100)
    response = client.post(
        "/api/v1/trips/preview",
        json=valid_preview_request(destination=seoul_destination()),
    )
    body = response.json()
    assert body["classification"] == "LOCAL"
    assert body["classificationDistanceMeters"] == 2_000
    assert body["allowance"]["status"] == "REVIEW_REQUIRED"
    assert body["allowance"]["amountKrw"] is None
```

- [ ] **Step 2: 통합 테스트를 실행해 기존 단순 편도×2 구현을 검출**

Run: `cd apps/travel-map && pytest tests/integration/test_public_road_round_trip.py -q`

Expected: 최초 실행은 `11,998` 또는 한 방향 호출 수 1로 FAIL하고, Stage A가 이미 양방향 계약과 서울 안 2km 분기를 지키면 네 테스트가 바로 PASS.

- [ ] **Step 3: 판정 provider와 카카오 DISTANCE fallback을 bootstrap에 연결**

`build_classification_provider(settings)`는 현재 graph manifest와 promotion report가 승인·일치하면 `PublicRoadProvider`, 아니면 `KakaoCarProvider(priority="DISTANCE", alternatives=False)`를 반환한다. `TripPreviewService`에는 이 primary와 별도 `KakaoCarProvider(priority="DISTANCE", alternatives=False)` fallback을 주입한다. primary가 이미 `KAKAO_CAR_DISTANCE`이면 같은 provider를 두 번 호출하지 않는다.

- [ ] **Step 4: `TripPreviewService.preview`에 비대칭 왕복 조회 구현**

`app/services/trip_preview.py`에서 outbound `RouteQuery(origin=기관, destination=출장지, mode=CAR, car_assumptions=request.car_assumptions)`와 return `RouteQuery(origin=출장지, destination=기관, mode=CAR, car_assumptions=request.car_assumptions)`를 각각 primary에 보낸다. 결과가 empty 또는 exception이면 그 방향만 카카오 DISTANCE fallback에 보낸다. 각 결과의 `distance_meters` 최솟값을 더하며 한 방향도 확보하지 못하면 직선거리로 대체하지 않고 `classification=REVIEW_REQUIRED`, warning `DATA_UNAVAILABLE`을 반환한다. 사용자가 선택한 표시 route ID는 이 두 query에 전달하지 않는다.

```python
async def _classification_distance(self, origin, destination, request):
    outbound = RouteQuery(
        origin=origin,
        destination=destination,
        depart_at=request.starts_at,
        mode=TravelMode.CAR,
        car_assumptions=request.car_assumptions,
    )
    returning = RouteQuery(
        origin=destination,
        destination=origin,
        depart_at=request.returns_at,
        mode=TravelMode.CAR,
        car_assumptions=request.car_assumptions,
    )
    outbound_result, return_result = await asyncio.gather(
        self._classification_routes(outbound),
        self._classification_routes(returning),
    )
    if not outbound_result.routes or not return_result.routes:
        raise ClassificationRouteUnavailable("both route directions are required")
    return (
        min(route.distance_meters for route in outbound_result.routes)
        + min(route.distance_meters for route in return_result.routes)
    )
```

- [ ] **Step 5: 11,999m·12,000m·한 방향 실패 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/integration/test_public_road_round_trip.py -q`

Expected: `4 passed`; 한 방향 no-route fixture는 서울 밖이면 `classification=REVIEW_REQUIRED`, 서울 안이면 `classification=LOCAL`·`allowance.status=REVIEW_REQUIRED`이며 둘 다 `DATA_UNAVAILABLE` warning을 반환.

- [ ] **Step 6: 왕복 판정 회귀 테스트 커밋**

```bash
git add apps/travel-map/app/routing/bootstrap.py apps/travel-map/app/services/trip_preview.py apps/travel-map/tests/integration/test_public_road_round_trip.py
git commit -m "fix(travel-map): use directional round-trip routes"
```

### Task 13: 카카오 골드 비교와 공공 제공자 승격 게이트

**Files:**
- Create: `apps/travel-map/app/providers/public_road/comparison.py`
- Create: `apps/travel-map/scripts/compare_road_providers.py`
- Create: `apps/travel-map/tests/providers/public_road/test_comparison.py`
- Create: `apps/travel-map/tests/fixtures/public-road/comparison-results.json`

**Interfaces:**
- Consumes: 공공·카카오 `ProviderResult`, Task 2 `SourceProbeReport`, Task 3 `GraphManifest`, 승인 기관 snapshot의 `siteId`/routing anchor, Stage A 지원영역 판정.
- Produces: `compare_results(public: ProviderResult, kakao: ProviderResult) -> CaseMetrics`; `evaluate_promotion(cases: Sequence[CaseMetrics], source_contract: SourceProbeReport, graph_manifest: GraphManifest) -> PromotionDecision`; 개인정보 없는 `promotion-report.json`.

- [ ] **Step 1: 비율 지표와 승격 기준 실패 테스트 작성**

```python
from app.providers.public_road.comparison import CaseMetrics, evaluate_promotion


def test_promotion_requires_all_quality_gates(valid_source_contract, eligible_graph_manifest):
    cases = [
        CaseMetrics(route_found=True, snap_failed=False, distance_error_ratio=0.10,
                    duration_error_ratio=0.15, catastrophic=False)
        for _ in range(28)
    ] + [
        CaseMetrics(route_found=False, snap_failed=True, distance_error_ratio=None,
                    duration_error_ratio=None, catastrophic=False),
        CaseMetrics(route_found=True, snap_failed=False, distance_error_ratio=0.20,
                    duration_error_ratio=0.25, catastrophic=False),
    ]
    decision = evaluate_promotion(cases, valid_source_contract, eligible_graph_manifest)
    assert decision.approved is False
    assert decision.failed_gates == ("snap_failure_rate<=0.02",)


def test_clean_thirty_case_sample_is_promotable(
    clean_comparison_cases, valid_source_contract, eligible_graph_manifest
):
    decision = evaluate_promotion(
        clean_comparison_cases, valid_source_contract, eligible_graph_manifest
    )
    assert decision.approved is True
    assert decision.sample_size == 30
```

- [ ] **Step 2: comparison 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_comparison.py -q`

Expected: FAIL with missing `comparison` module.

- [ ] **Step 3: 명시적 승격 기준과 aggregate 리포트 구현**

30개 이상 표본에서 다음 조건을 모두 만족해야 `approved=True`다: source contract와 graph manifest 모두 `primaryEligible=true`, graph가 source contract의 input hash를 참조, TOPIS request mode가 `BULK|PER_LINK`, vehicle access와 turn restriction coverage 각각 1.0, route 성공률 `>=95%`, snap 실패율 `<=2%`, 최단거리 상대오차 median `<=15%`·p95 `<=35%`, 최단시간 상대오차 median `<=20%`·p95 `<=40%`, 거리 또는 시간이 카카오 대비 50% 넘게 차이 나는 catastrophic 비율 `<=10%`. 비교값의 분모는 `max(kakao_value, 1)`이다. report에는 `approved`, 표본 수·자치구/기관유형/설립구분 개수·aggregate percentile·gate 결과·source contract hash·graph snapshot ID·TOPIS 시각만 기록하고 좌표·기관명·목적지·geometry는 기록하지 않는다.

- [ ] **Step 4: 30쌍 층화 비교 CLI 구현**

CLI는 승인 기관 snapshot을 `siteId` 안정 정렬한 뒤 25개 자치구에서 최소 1곳, 교육청기관·유치원·초·중·고·특수/각종과 국립·공립·사립을 포함하도록 결정론적으로 30개 출발지를 선택한다. 목적지는 support area 안에서 동일 자치구 10건, 다른 서울 자치구 10건, 서울 밖 12km buffer 10건이 되도록 Stage A 장소 fixture의 검증 좌표를 매칭한다. `--live` 없이는 저장된 provider fixture만 읽고 외부 호출을 하지 않는다.

Run: `cd apps/travel-map && python scripts/compare_road_providers.py --graph resources/road-network/current.json --source-contract resources/road-network/source-contract-report.json --institutions resources/institution-snapshots/current.json --cases 30 --live --output /tmp/public-road-promotion-report.json`

Expected: `sampleSize=30`, 각 source/route gate의 실제 수치, 최종 `approved=true|false`를 출력하며 `/tmp/public-road-promotion-report.json`에 좌표·기관명·경로선이 없음. `KAKAO_MOBILITY_REST_API_KEY`, `TOPIS_API_KEY`, `OPINET_API_KEY` 중 필요한 키가 없으면 네트워크 호출 전 exit code 2와 누락 변수명만 출력.

- [ ] **Step 5: 저장 fixture로 지표 테스트 통과 확인**

Run: `cd apps/travel-map && pytest tests/providers/public_road/test_comparison.py -q`

Expected: `9 passed`, source/graph 불일치, turn/access 미확인, percentile·분모 0 방지·30건 미만 거부·개인정보 필드 거부 포함.

- [ ] **Step 6: 비교 도구 커밋**

```bash
git add apps/travel-map/app/providers/public_road/comparison.py apps/travel-map/scripts/compare_road_providers.py apps/travel-map/tests/providers/public_road/test_comparison.py apps/travel-map/tests/fixtures/public-road/comparison-results.json
git commit -m "feat(travel-map): gate public road provider promotion"
```

### Task 14: 운영 리소스 검증·Docker·문서와 전체 검증

**Files:**
- Create: `apps/travel-map/resources/road-network/source-contract-report.json`
- Create: `apps/travel-map/resources/road-network/current.json`
- Create: `apps/travel-map/resources/road-network/promotion-report.json`
- Modify: `apps/travel-map/Dockerfile`
- Modify: `apps/travel-map/README.md`
- Create: `apps/travel-map/app/api/provider_health.py`
- Modify: `apps/travel-map/app/dependencies.py`
- Modify: `apps/travel-map/app/main.py`
- Create: `apps/travel-map/tests/integration/test_public_road_release.py`

**Interfaces:**
- Consumes: Tasks 2–13 source contract, graph snapshot, health metadata, provider modes and promotion decision.
- Produces: 재현 가능한 운영 graph 포함 이미지; `app.api.provider_health`가 제공하는 `/health/providers`의 비식별 metadata `graphSnapshotId`, `graphSourceAsOf`, `sourceContractHash`, `promotionApproved`, `trafficAgeSeconds`, `roadProviderMode`, `fallbackReady`.

- [ ] **Step 1: 운영 graph 누락·hash 불일치·비밀 노출 실패 테스트 작성**

```python
def test_health_exposes_only_non_sensitive_road_metadata(client):
    response = client.get("/health/providers")
    assert response.status_code == 200
    road = response.json()["road"]
    assert set(road) == {
        "graphSnapshotId", "graphSourceAsOf", "sourceContractHash",
        "promotionApproved", "trafficAgeSeconds", "roadProviderMode",
        "fallbackReady"
    }
    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "latitude" not in serialized
    assert "longitude" not in serialized


def test_primary_mode_falls_back_when_graph_pointer_is_invalid(public_primary_client):
    response = public_primary_client.post("/api/v1/trips/preview", json=valid_preview_request())
    assert response.status_code == 200
    assert any(route["source"].startswith("KAKAO") for route in response.json()["routes"])
    assert "ROAD_GRAPH_UNAVAILABLE" in {item["code"] for item in response.json()["warnings"]}
```

- [ ] **Step 2: release 테스트 RED 확인**

Run: `cd apps/travel-map && pytest tests/integration/test_public_road_release.py -q`

Expected: health metadata 또는 invalid graph fallback assertion으로 FAIL.

- [ ] **Step 3: health metadata와 Docker artifact 검증 구현**

`app/api/provider_health.py`는 `APIRouter`로 `GET /health/providers`를 정의하고, `AppDependencies`에 추가한 `road_provider_health: Callable[[], RoadProviderHealth]`가 반환한 상태만 camelCase로 직렬화한다. `create_app()`은 이 router를 포함하되 기존 `/healthz -> {"status": "ok"}` 계약은 바꾸지 않는다. health callable은 현재 포인터·manifest·승격 보고서와 in-memory TOPIS snapshot의 나이만 읽으며 키, 좌표, 링크 ID를 반환하지 않는다. Stage A 기본값과 `ROAD_PROVIDER_MODE=kakao`에서는 nullable snapshot 필드, `promotionApproved=false`, `fallbackReady=true`를 반환한다.

```python
class RoadProviderHealth(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    graph_snapshot_id: str | None
    graph_source_as_of: date | None
    source_contract_hash: str | None
    promotion_approved: bool
    traffic_age_seconds: int | None
    road_provider_mode: RoadProviderMode
    fallback_ready: bool
```

Docker build arg `ROAD_GRAPH_ARCHIVE`는 CI가 승인 snapshot archive의 절대 경로가 아닌 build context 상대 경로로 전달한다. build 단계에서 source contract input hash, `manifest.json`의 SQLite SHA-256, `promotion-report.json`의 `graphSnapshotId` 일치를 확인하고 `/app/resources/road-network/`에 복사한다. 저장소 기본 `source-contract-report.json`은 `{"schemaVersion": 1, "buildAllowed": false, "primaryEligible": false, "failures": ["LIVE_PROBE_REQUIRED"], "inputSha256": {}}`, `current.json`은 `{"snapshotId": null}`, 기본 `promotion-report.json`은 `{"approved": false, "graphSnapshotId": null, "failedGates": ["LIVE_COMPARISON_REQUIRED"]}`다. archive가 없으면 기본 이미지는 `ROAD_PROVIDER_MODE=kakao`로만 동작하고, `public_primary` 설정 시 health가 degraded여도 카카오 fallback readiness가 true인 동안 요청을 처리한다.

- [ ] **Step 4: README에 실제 빌드·shadow·승격 절차 기록**

README에 다음 명령과 기준을 그대로 기록한다: source probe와 graph compiler 명령, `ROAD_PROVIDER_MODE=public_shadow` 로컬 실행, 30쌍 비교 명령, Task 13의 source/route 품질 gate, 승인 report 배치, `public_primary` 전환, 이전 snapshot ID로 `current.json`과 promotion report를 함께 원자 rollback. TOPIS/오피넷 원천시각, 사용자가 확인한 유종·연비·주차비, 확인된 통행료만 포함됨을 결과에 노출한다는 점과 예상 이동비가 법정 여비가 아니라는 점을 명시한다.

- [ ] **Step 5: release 테스트와 전체 단위 테스트 실행**

Run: `cd apps/travel-map && pytest tests/providers/public_road tests/integration/test_public_road_bootstrap.py tests/integration/test_public_road_round_trip.py tests/integration/test_public_road_release.py -q`

Expected: all PASS, skipped live tests 0개; 외부 키 없이 fixture만 사용.

- [ ] **Step 6: 전체 앱 lint 실행**

Run: `cd apps/travel-map && ruff check .`

Expected: `All checks passed!`.

- [ ] **Step 7: 전체 앱 테스트 실행**

Run: `cd apps/travel-map && pytest -q`

Expected: full suite PASS without network access.

- [ ] **Step 8: 카카오 모드 Docker image 빌드**

Run: `cd apps/travel-map && docker build -t seoul-travel-map-road:test .`

Expected: image build 성공, source/graph/promotion 기본 리소스 schema 검사 PASS.

- [ ] **Step 9: 카카오 모드 Docker smoke test 실행**

Run: `cd apps/travel-map && docker run --rm -e ROAD_PROVIDER_MODE=kakao seoul-travel-map-road:test python -c 'from app.main import app; assert app is not None; print("kakao fallback image OK")'`

Expected: `kakao fallback image OK`.

- [ ] **Step 10: 운영 통합 커밋**

```bash
git add apps/travel-map/resources/road-network/source-contract-report.json apps/travel-map/resources/road-network/current.json apps/travel-map/resources/road-network/promotion-report.json apps/travel-map/app/api/provider_health.py apps/travel-map/app/dependencies.py apps/travel-map/app/main.py apps/travel-map/Dockerfile apps/travel-map/README.md apps/travel-map/tests/integration/test_public_road_release.py
git commit -m "docs(travel-map): document public road rollout"
```

## 완료 판정

- fixture graph만으로 공공 `CAR` provider가 최단시간·최단거리·최저비용과 복수 경로를 결정론적으로 반환한다.
- 일방통행을 보존하며 판정용 왕복거리는 양방향 경로 합으로 11,999m/12,000m 경계 테스트를 통과한다.
- TOPIS 장애는 기준속도 또는 15분 이내 stale snapshot으로 제한되고 경고가 사용자 응답에 남는다.
- 오피넷 장애는 7일 이내 승인값만 사용하며 그보다 오래되면 이동비를 `UNKNOWN`으로 두고 경로 자체는 유지한다.
- `kakao`, `public_shadow`, `public_primary` 세 모드가 독립 테스트되고 public 실패 시 0값 경로 없이 카카오로 fallback한다.
- 승인된 실제 graph snapshot과 30쌍 비교가 모든 승격 gate를 통과하기 전에는 운영 기본값을 `public_primary`로 바꾸지 않는다.
- health·로그·비교 summary에 API 키, 기관명, 목적지, 정밀 좌표, geometry가 없다.

## 실행 순서

Task 1→14 순서로 실행한다. Task 2의 fixture probe와 Task 3의 fixture graph부터 Task 12까지는 외부 키 없이 완전히 테스트할 수 있다. 실제 원천 live probe, 대용량 graph 생성, 카카오 live 비교는 opt-in이며, 실패해도 카카오 fallback이 있는 앱의 기존 공개 기능을 손상시키지 않는다.
