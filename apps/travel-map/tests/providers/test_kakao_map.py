import asyncio
import copy

import httpx
import pytest
from app.providers.kakao_map import KakaoTransitProvider, KakaoWalkProvider
from app.routing.models import CostStatus, TravelMode
from pydantic import SecretStr
from tests.providers.helpers import NOW, load_json, route_query


@pytest.mark.asyncio
async def test_kakao_public_transit_returns_all_routes_with_fares_and_geometry() -> (
    None
):
    secret = "map-header-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://dapi.kakao.com/v2/routing/publictraffic",
            params={
                "start_x": "126.97",
                "start_y": "37.55",
                "end_x": "126.98",
                "end_y": "37.56",
                "input_coord": "WGS84",
                "output_coord": "WGS84",
            },
        )
        assert request.headers["Authorization"] == f"KakaoAK {secret}"
        assert secret not in str(request.url)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json=load_json("kakao-publictraffic.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoTransitProvider(
            http=http,
            rest_key=SecretStr(secret),
            now=lambda: NOW,
        )
        result = await provider.get_routes(route_query(TravelMode.TRANSIT))

    assert len(result.routes) == 3
    first = result.routes[0]
    assert first.duration_seconds == 2820
    assert first.distance_meters == 14600
    assert first.mobility_cost_krw == 1550
    assert first.cost_status is CostStatus.KNOWN
    assert first.cost_breakdown is not None
    assert first.cost_breakdown.fare_krw == 1550
    assert len(first.geometry) == 3
    assert len({route.id for route in result.routes}) == 3
    assert all(route.source == "KAKAO_TRANSIT" for route in result.routes)
    assert all(route.source_as_of == NOW for route in result.routes)


@pytest.mark.asyncio
async def test_kakao_walk_calls_three_modes_and_returns_distinct_deterministic_ids() -> (
    None
):
    modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/routing/walk"
        modes.append(request.url.params["route_mode"])
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-walk.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoWalkProvider(
            http=http,
            rest_key=SecretStr("test-key"),
            now=lambda: NOW,
        )
        first = await provider.get_routes(route_query(TravelMode.WALK))
        second = await provider.get_routes(route_query(TravelMode.WALK))

    assert modes == [
        "BROAD_FIRST",
        "SHORTEST",
        "ACCESSIBLE",
        "BROAD_FIRST",
        "SHORTEST",
        "ACCESSIBLE",
    ]
    assert len(first.routes) == 3
    assert len({route.id for route in first.routes}) == 3
    assert [route.id for route in first.routes] == [route.id for route in second.routes]
    assert all(route.mobility_cost_krw == 0 for route in first.routes)


@pytest.mark.asyncio
async def test_kakao_map_rejects_wrong_mode_without_network() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoTransitProvider(http=http, rest_key=SecretStr("test-key"))
        result = await provider.get_routes(route_query(TravelMode.WALK))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["UNSUPPORTED_MODE"]
    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "mode", "expected_requests"),
    [
        (KakaoTransitProvider, TravelMode.TRANSIT, 1),
        (KakaoWalkProvider, TravelMode.WALK, 3),
    ],
)
async def test_kakao_map_classifies_official_no_results_as_empty_not_schema_error(
    provider_type: type[KakaoTransitProvider] | type[KakaoWalkProvider],
    mode: TravelMode,
    expected_requests: int,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"status": "NO_RESULTS"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await provider_type(
            http=http,
            rest_key=SecretStr("key"),
        ).get_routes(route_query(mode))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["NO_RESULTS"]
    assert requests == expected_requests


# Break caught: the official walk empty-result status is treated as malformed schema.
@pytest.mark.asyncio
async def test_kakao_walk_classifies_route_result_not_found_as_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"status": "ROUTE_RESULT_NOT_FOUND"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await KakaoWalkProvider(
            http=http,
            rest_key=SecretStr("key"),
        ).get_routes(route_query(TravelMode.WALK))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["NO_RESULTS"]


# Break caught: schema drift in the first of three walk calls is lost when only the
# final call's observed fingerprint is retained.
@pytest.mark.asyncio
async def test_kakao_walk_fingerprint_covers_every_mode_response() -> None:
    async def fingerprint(*, drift_first: bool) -> str | None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = load_json("kakao-walk.json")
            if drift_first and request.url.params["route_mode"] == "BROAD_FIRST":
                payload["nested_contract_drift"] = {"new_field": True}
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=payload,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = KakaoWalkProvider(http=http, rest_key=SecretStr("key"))
            result = await provider.get_routes(route_query(TravelMode.WALK))
            assert len(result.routes) == 3
            return provider.last_schema_fingerprint

    stable = await fingerprint(drift_first=False)
    drifted = await fingerprint(drift_first=True)

    assert type(stable) is str and len(stable) == 64
    assert type(drifted) is str and len(drifted) == 64
    assert stable != drifted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, "UPSTREAM_RATE_LIMIT"), (503, "UPSTREAM_UNAVAILABLE")],
)
async def test_kakao_map_retries_bounded_statuses_with_secret_free_warning(
    status: int,
    expected: str,
) -> None:
    secret = "map-status-secret"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json"},
            json={"message": secret},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoTransitProvider(http=http, rest_key=SecretStr(secret))
        result = await provider.get_routes(route_query(TravelMode.TRANSIT))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == [expected]
    assert requests == 2
    assert secret not in repr(result)
    assert secret not in repr(provider)


@pytest.mark.asyncio
async def test_kakao_map_rejects_schema_route_and_geometry_limits() -> None:
    payloads: list[dict[str, object]] = []
    too_many_routes = load_json("kakao-publictraffic.json")
    too_many_routes["routes"] = [
        copy.deepcopy(too_many_routes["routes"][0])  # type: ignore[index]
        for _ in range(11)
    ]
    payloads.append(too_many_routes)
    too_many_points = load_json("kakao-publictraffic.json")
    path = too_many_points["routes"][0]["steps"][0]["path"]  # type: ignore[index]
    path["points"] = [[126.97, 37.55] for _ in range(101)]
    payloads.append(too_many_points)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payloads.pop(0),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = KakaoTransitProvider(
            http=http,
            rest_key=SecretStr("test-key"),
            max_routes=10,
            max_geometry_points=100,
        )
        route_limit = await provider.get_routes(route_query(TravelMode.TRANSIT))
        geometry_limit = await provider.get_routes(route_query(TravelMode.TRANSIT))

    assert [warning.code for warning in route_limit.warnings] == [
        "RESPONSE_LIMIT_EXCEEDED"
    ]
    assert [warning.code for warning in geometry_limit.warnings] == [
        "RESPONSE_LIMIT_EXCEEDED"
    ]


@pytest.mark.asyncio
async def test_kakao_map_geometry_limit_counts_raw_points_before_deduplication() -> (
    None
):
    payload = load_json("kakao-publictraffic.json")
    payload["routes"] = [payload["routes"][0]]  # type: ignore[index]
    payload["routes"][0]["steps"] = [  # type: ignore[index]
        {"path": {"points": [[126.97, 37.55], [126.97, 37.55]]}},
        {"path": {"points": [[126.97, 37.55], [126.98, 37.56]]}},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await KakaoTransitProvider(
            http=http,
            rest_key=SecretStr("key"),
            max_geometry_points=3,
        ).get_routes(route_query(TravelMode.TRANSIT))

    assert [warning.code for warning in result.warnings] == ["RESPONSE_LIMIT_EXCEEDED"]


@pytest.mark.asyncio
async def test_kakao_map_cancellation_propagates_and_injected_client_stays_open() -> (
    None
):
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = KakaoTransitProvider(http=http, rest_key=SecretStr("test-key"))
    task = asyncio.create_task(provider.get_routes(route_query(TravelMode.TRANSIT)))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()
    await provider.aclose()
    assert not http.is_closed
    await http.aclose()
