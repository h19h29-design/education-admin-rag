import copy

import httpx
import pytest
from app.providers.kakao_mobility import KakaoCarProvider
from app.providers.opinet import OpinetClient
from app.routing.models import CostStatus, TravelMode
from pydantic import SecretStr
from tests.providers.helpers import (
    NOW,
    gasoline_assumptions,
    load_json,
    route_query,
)


@pytest.mark.asyncio
async def test_kakao_car_returns_all_alternatives_and_self_driving_cost() -> None:
    kakao_secret = "mobility-header-secret"
    opinet_secret = "opinet-query-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "apis-navi.kakaomobility.com":
            assert request.headers["Authorization"] == f"KakaoAK {kakao_secret}"
            assert request.url.params["origin"] == "126.97,37.55"
            assert request.url.params["destination"] == "126.98,37.56"
            assert request.url.params["priority"] == "RECOMMEND"
            assert request.url.params["alternatives"] == "true"
            assert request.url.params["summary"] == "false"
            assert request.url.params["car_fuel"] == "GASOLINE"
            assert kakao_secret not in str(request.url)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=load_json("kakao-car.json"),
            )
        assert request.url.host == "www.opinet.co.kr"
        assert request.url.params["certkey"] == opinet_secret
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("opinet-average.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        opinet = OpinetClient(
            http=http,
            cert_key=SecretStr(opinet_secret),
            now=lambda: NOW,
        )
        provider = KakaoCarProvider(
            http=http,
            rest_key=SecretStr(kakao_secret),
            opinet=opinet,
            now=lambda: NOW,
            priority="RECOMMEND",
            alternatives=True,
        )
        first = await provider.get_routes(
            route_query(TravelMode.CAR, car_assumptions=gasoline_assumptions())
        )
        second = await provider.get_routes(
            route_query(TravelMode.CAR, car_assumptions=gasoline_assumptions())
        )

    assert len(first.routes) == 2
    route = first.routes[0]
    assert route.duration_seconds == 2400
    assert route.distance_meters == 20000
    assert route.mobility_cost_krw == 6400
    assert route.cost_breakdown is not None
    assert route.cost_breakdown.fuel_krw == 3400
    assert route.cost_breakdown.toll_krw == 1000
    assert route.cost_breakdown.parking_krw == 2000
    assert route.mobility_cost_krw != 24000
    assert len(route.geometry) == 3
    assert [item.id for item in first.routes] == [item.id for item in second.routes]


@pytest.mark.asyncio
async def test_kakao_car_returns_unknown_cost_when_fuel_price_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "apis-navi.kakaomobility.com"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-car.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoCarProvider(
            http=http,
            rest_key=SecretStr("test-key"),
            opinet=OpinetClient(http=http, cert_key=None),
            now=lambda: NOW,
        )
        result = await provider.get_routes(
            route_query(TravelMode.CAR, car_assumptions=gasoline_assumptions())
        )

    assert result.routes
    assert all(route.cost_status is CostStatus.UNKNOWN for route in result.routes)
    assert all(route.mobility_cost_krw is None for route in result.routes)
    assert [warning.code for warning in result.warnings] == ["MISSING_CREDENTIAL"]


@pytest.mark.asyncio
async def test_kakao_car_without_assumptions_does_not_fetch_opinet_or_invent_cost() -> (
    None
):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-car.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoCarProvider(
            http=http,
            rest_key=SecretStr("test-key"),
            opinet=OpinetClient(http=http, cert_key=SecretStr("unused")),
        )
        result = await provider.get_routes(route_query(TravelMode.CAR))

    assert hosts == ["apis-navi.kakaomobility.com"]
    assert result.routes[0].cost_status is CostStatus.UNKNOWN
    assert [warning.code for warning in result.warnings] == ["CAR_ASSUMPTIONS_MISSING"]


@pytest.mark.asyncio
async def test_kakao_car_rejects_route_road_and_vertex_limits() -> None:
    payloads: list[dict[str, object]] = []
    routes = load_json("kakao-car.json")
    routes["routes"] = [copy.deepcopy(routes["routes"][0]) for _ in range(6)]  # type: ignore[index]
    payloads.append(routes)
    roads = load_json("kakao-car.json")
    roads["routes"][0]["sections"][0]["roads"] = [  # type: ignore[index]
        copy.deepcopy(roads["routes"][0]["sections"][0]["roads"][0])  # type: ignore[index]
        for _ in range(3)
    ]
    payloads.append(roads)
    vertexes = load_json("kakao-car.json")
    vertexes["routes"][0]["sections"][0]["roads"][0]["vertexes"] = [  # type: ignore[index]
        126.97,
        37.55,
    ] * 4
    payloads.append(vertexes)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payloads.pop(0),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoCarProvider(
            http=http,
            rest_key=SecretStr("test-key"),
            opinet=OpinetClient(http=http, cert_key=None),
            max_routes=5,
            max_roads=2,
            max_geometry_points=3,
        )
        results = [
            await provider.get_routes(route_query(TravelMode.CAR)) for _ in range(3)
        ]

    assert [result.warnings[0].code for result in results] == [
        "RESPONSE_LIMIT_EXCEEDED",
        "RESPONSE_LIMIT_EXCEEDED",
        "RESPONSE_LIMIT_EXCEEDED",
    ]


@pytest.mark.asyncio
async def test_kakao_car_geometry_limit_counts_raw_vertices_across_roads() -> None:
    payload = load_json("kakao-car.json")
    payload["routes"] = [payload["routes"][0]]  # type: ignore[index]
    road = payload["routes"][0]["sections"][0]["roads"][0]  # type: ignore[index]
    road["vertexes"] = [126.97, 37.55, 126.98, 37.56]
    payload["routes"][0]["sections"][0]["roads"] = [  # type: ignore[index]
        copy.deepcopy(road),
        copy.deepcopy(road),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await KakaoCarProvider(
            http=http,
            rest_key=SecretStr("key"),
            max_roads=2,
            max_geometry_points=3,
        ).get_routes(route_query(TravelMode.CAR))

    assert [warning.code for warning in result.warnings] == ["RESPONSE_LIMIT_EXCEEDED"]


def test_kakao_car_rejects_invalid_priority_and_alternatives_types() -> None:
    with pytest.raises(ValueError):
        KakaoCarProvider(rest_key=SecretStr("key"), priority="FASTEST")
    with pytest.raises(TypeError):
        KakaoCarProvider(rest_key=SecretStr("key"), alternatives=1)  # type: ignore[arg-type]
