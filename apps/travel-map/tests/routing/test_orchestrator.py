import asyncio
from dataclasses import replace

import pytest
from app.routing.models import Coordinate, ProviderResult, ProviderWarning, TravelMode
from app.routing.orchestrator import RouteOrchestrator
from tests.routing.fakes import (
    ConcurrencyTracker,
    FakeProvider,
    RaisingProvider,
    TrackingProvider,
    base_query,
    failed_result,
    result_with,
    route,
)


class InvalidProvider:
    def __init__(self, name: str, supported_modes: object) -> None:
        self.name = name
        self.supported_modes = supported_modes

    async def get_routes(self, query: object) -> ProviderResult:
        return ProviderResult(provider="INVALID", routes=())


class SyncProvider:
    name = "SYNC"
    supported_modes = frozenset({TravelMode.TRANSIT})

    def get_routes(self, query: object) -> ProviderResult:
        return ProviderResult(provider=self.name, routes=())


class AsyncRouteCallable:
    async def __call__(self, query: object) -> ProviderResult:
        return result_with(route("callable", source="CALLABLE"))


class AsyncCallableProvider:
    name = "CALLABLE"
    supported_modes = frozenset({TravelMode.TRANSIT})

    def __init__(self) -> None:
        self.get_routes = AsyncRouteCallable()


class CancellingProvider:
    name = "CANCELLING"
    supported_modes = frozenset({TravelMode.TRANSIT})

    async def get_routes(self, query: object) -> ProviderResult:
        raise asyncio.CancelledError


# Break caught: a malformed provider registry failing only after request fan-out.
@pytest.mark.parametrize(
    "provider",
    [
        InvalidProvider(" ", frozenset({TravelMode.TRANSIT})),
        InvalidProvider("INVALID", {TravelMode.TRANSIT}),
        InvalidProvider("INVALID", frozenset({"TRANSIT"})),
    ],
)
def test_orchestrator_rejects_invalid_provider_contract(provider: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RouteOrchestrator(
            {TravelMode.TRANSIT: (provider,)},  # type: ignore[arg-type]
            max_concurrency=1,
        )


# Break caught: a synchronous boundary becoming a sanitized runtime warning.
def test_orchestrator_rejects_sync_get_routes_during_registration() -> None:
    with pytest.raises(TypeError, match="get_routes must be async"):
        RouteOrchestrator(
            {TravelMode.TRANSIT: (SyncProvider(),)},  # type: ignore[dict-item]
            max_concurrency=1,
        )


# Break caught: rejecting a valid async callable object used as the protocol method.
@pytest.mark.asyncio
async def test_orchestrator_accepts_async_callable_get_routes_object() -> None:
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (AsyncCallableProvider(),)},  # type: ignore[dict-item]
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["callable"]


# Break caught: treating cooperative task cancellation as an upstream fallback.
@pytest.mark.asyncio
async def test_orchestrator_does_not_swallow_cancellation() -> None:
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (CancellingProvider(),)},
        max_concurrency=1,
    )

    with pytest.raises(asyncio.CancelledError):
        await orchestrator.collect(base_query(), {TravelMode.TRANSIT})


# Break caught: one name collapsing two same-mode chain positions to one priority.
def test_orchestrator_rejects_duplicate_provider_names_in_same_chain() -> None:
    first = FakeProvider(
        "duplicate",
        result_with(route("first", source="duplicate")),
    )
    second = FakeProvider(
        "duplicate",
        result_with(route("second", source="duplicate")),
    )

    with pytest.raises(ValueError, match="provider names must be globally unique"):
        RouteOrchestrator(
            {TravelMode.TRANSIT: (first, second)},
            max_concurrency=1,
        )


# Break caught: a name collision across modes changing representative tie order.
def test_orchestrator_rejects_duplicate_provider_names_across_modes() -> None:
    transit = FakeProvider(
        "duplicate",
        result_with(route("transit", source="duplicate")),
    )
    car = FakeProvider(
        "duplicate",
        result_with(
            route("car", mode=TravelMode.CAR, source="duplicate"),
        ),
        supported_modes=frozenset({TravelMode.CAR}),
    )

    with pytest.raises(ValueError, match="provider names must be globally unique"):
        RouteOrchestrator(
            {
                TravelMode.TRANSIT: (transit,),
                TravelMode.CAR: (car,),
            },
            max_concurrency=2,
        )


# Break caught: route ID lexical order replacing registry order in cross-mode ties.
@pytest.mark.asyncio
async def test_orchestrator_uses_unique_registry_order_for_cross_mode_ties() -> None:
    transit = FakeProvider(
        "z-provider",
        result_with(route("z-route", source="z-provider")),
    )
    car = FakeProvider(
        "a-provider",
        result_with(
            route("a-route", mode=TravelMode.CAR, source="a-provider"),
        ),
        supported_modes=frozenset({TravelMode.CAR}),
    )
    orchestrator = RouteOrchestrator(
        {
            TravelMode.TRANSIT: (transit,),
            TravelMode.CAR: (car,),
        },
        max_concurrency=2,
    )

    collection = await orchestrator.collect(
        base_query(),
        {TravelMode.CAR, TravelMode.TRANSIT},
    )

    assert collection.best.fastest_route_id == "z-route"
    assert collection.best.shortest_route_id == "z-route"
    assert collection.best.cheapest_route_id == "z-route"


# Break caught: abandoning a mode when its primary provider partially fails.
@pytest.mark.asyncio
async def test_orchestrator_uses_second_provider_after_primary_failure() -> None:
    primary = FakeProvider(
        "public",
        failed_result("UPSTREAM_TIMEOUT", provider="public"),
    )
    fallback = FakeProvider(
        "kakao",
        result_with(route("fallback", 700, 4_000, 1_500, source="kakao")),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (primary, fallback)},
        max_concurrency=3,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["fallback"]
    assert [warning.code for warning in collection.warnings] == ["UPSTREAM_TIMEOUT"]


# Break caught: spending quota on fallback after a valid primary response.
@pytest.mark.asyncio
async def test_orchestrator_stops_chain_after_valid_primary_routes() -> None:
    primary = FakeProvider(
        "public",
        result_with(route("primary", source="public")),
    )
    fallback = FakeProvider(
        "kakao",
        result_with(route("fallback", source="kakao")),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (primary, fallback)},
        max_concurrency=3,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["primary"]
    assert fallback.call_count == 0


# Break caught: invoking a provider for a mode it does not advertise.
@pytest.mark.asyncio
async def test_orchestrator_falls_back_on_missing_capability() -> None:
    wrong_mode = FakeProvider(
        "transit-only",
        result_with(route("wrong", source="transit-only")),
    )
    car = FakeProvider(
        "car",
        result_with(
            route("car", mode=TravelMode.CAR, source="car"),
        ),
        supported_modes=frozenset({TravelMode.CAR}),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.CAR: (wrong_mode, car)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.CAR})

    assert [item.id for item in collection.routes] == ["car"]
    assert [warning.code for warning in collection.warnings] == ["CAPABILITY_MISSING"]
    assert wrong_mode.call_count == 0


# Break caught: one hung upstream preventing fallback indefinitely.
@pytest.mark.asyncio
async def test_orchestrator_times_out_then_uses_fallback() -> None:
    slow = FakeProvider(
        "slow",
        result_with(route("slow", source="slow")),
        delay_seconds=0.05,
    )
    fallback = FakeProvider(
        "fallback",
        result_with(route("fallback", source="fallback")),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (slow, fallback)},
        max_concurrency=1,
        provider_timeout_seconds=0.005,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["fallback"]
    assert [warning.code for warning in collection.warnings] == ["UPSTREAM_TIMEOUT"]


# Break caught: synthesizing a misleading zero route when every provider fails.
@pytest.mark.asyncio
async def test_orchestrator_all_provider_failure_returns_no_route() -> None:
    empty = FakeProvider("empty", ProviderResult(provider="empty", routes=()))
    broken = RaisingProvider("broken")
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (empty, broken)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert collection.routes == ()
    assert collection.best.fastest_route_id is None
    assert collection.best.shortest_route_id is None
    assert collection.best.cheapest_route_id is None
    assert [warning.code for warning in collection.warnings] == [
        "NO_ROUTES",
        "UPSTREAM_ERROR",
    ]
    assert "secret detail" not in collection.warnings[-1].message


# Break caught: unbounded mode fan-out overrunning upstream limits.
@pytest.mark.asyncio
async def test_orchestrator_enforces_global_concurrency_limit() -> None:
    tracker = ConcurrencyTracker()
    providers = {
        mode: (TrackingProvider(mode.value, mode, tracker),) for mode in TravelMode
    }
    orchestrator = RouteOrchestrator(providers, max_concurrency=2)

    collection = await orchestrator.collect(base_query(), set(TravelMode))

    assert len(collection.routes) == 3
    assert tracker.peak == 2


# Break caught: asynchronous completion making warning order nondeterministic.
@pytest.mark.asyncio
async def test_orchestrator_warning_order_is_stable_by_mode_and_chain() -> None:
    transit = FakeProvider(
        "transit",
        failed_result("TRANSIT_FAIL", provider="transit"),
        delay_seconds=0.02,
    )
    car = FakeProvider(
        "car",
        failed_result("CAR_FAIL", provider="car"),
        supported_modes=frozenset({TravelMode.CAR}),
    )
    orchestrator = RouteOrchestrator(
        {
            TravelMode.TRANSIT: (transit,),
            TravelMode.CAR: (car,),
        },
        max_concurrency=2,
    )

    collection = await orchestrator.collect(
        base_query(),
        {TravelMode.CAR, TravelMode.TRANSIT},
    )

    assert [warning.code for warning in collection.warnings] == [
        "TRANSIT_FAIL",
        "CAR_FAIL",
    ]


# Break caught: accepting a provider response under another provider's identity.
@pytest.mark.asyncio
async def test_orchestrator_rejects_provider_name_mismatch_and_falls_back() -> None:
    mismatched = FakeProvider(
        "expected",
        result_with(route("wrong", source="actual")),
    )
    fallback = FakeProvider(
        "fallback",
        result_with(route("right", source="fallback")),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (mismatched, fallback)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["right"]
    assert [warning.code for warning in collection.warnings] == [
        "PROVIDER_IDENTITY_MISMATCH"
    ]


# Break caught: returning a route for a different requested transport mode.
@pytest.mark.asyncio
async def test_orchestrator_rejects_route_mode_mismatch_and_falls_back() -> None:
    wrong = FakeProvider(
        "wrong",
        result_with(
            route("wrong-mode", mode=TravelMode.CAR, source="wrong"),
        ),
        supported_modes=frozenset({TravelMode.TRANSIT}),
    )
    fallback = FakeProvider(
        "fallback",
        result_with(route("right", source="fallback")),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (wrong, fallback)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert [item.id for item in collection.routes] == ["right"]
    assert [warning.code for warning in collection.warnings] == ["MODE_MISMATCH"]


# Break caught: presenting the public provider's endpoint chord as actual geometry.
@pytest.mark.asyncio
async def test_orchestrator_supplements_unique_matching_public_geometry_with_lineage() -> (
    None
):
    placeholder = route(
        "public",
        seconds=2_820,
        meters=14_600,
        cost=None,
        source="SEOUL_TRANSIT",
        geometry=(Coordinate(37.55, 126.97), Coordinate(37.56, 126.98)),
    )
    placeholder = replace(
        placeholder,
        warnings=("GEOMETRY_MISSING", "FARE_MISSING"),
    )
    geometry = (
        Coordinate(37.55, 126.97),
        Coordinate(37.555, 126.975),
        Coordinate(37.56, 126.98),
    )
    public = FakeProvider(
        "SEOUL_TRANSIT",
        ProviderResult(
            provider="SEOUL_TRANSIT",
            routes=(placeholder,),
            warnings=(
                ProviderWarning(
                    code="GEOMETRY_MISSING",
                    message="Public route geometry is unavailable",
                    source="SEOUL_TRANSIT",
                ),
                ProviderWarning(
                    code="FARE_MISSING",
                    message="Public route fare is unavailable",
                    source="SEOUL_TRANSIT",
                ),
            ),
        ),
    )
    kakao = FakeProvider(
        "KAKAO_TRANSIT",
        result_with(
            route(
                "kakao",
                seconds=2_820,
                meters=14_600,
                cost=1_550,
                source="KAKAO_TRANSIT",
                geometry=geometry,
            )
        ),
    )
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (public, kakao)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert len(collection.routes) == 1
    enriched = collection.routes[0]
    assert enriched.source == "SEOUL_TRANSIT+KAKAO_GEOMETRY"
    assert enriched.geometry == geometry
    assert enriched.duration_seconds == 2_820
    assert enriched.distance_meters == 14_600
    assert enriched.mobility_cost_krw is None
    assert "GEOMETRY_MISSING" not in enriched.warnings
    assert "GEOMETRY_SOURCE=KAKAO_TRANSIT:kakao" in enriched.warnings
    assert [warning.code for warning in collection.warnings] == [
        "GEOMETRY_MISSING",
        "FARE_MISSING",
        "GEOMETRY_SUPPLEMENTED",
    ]


# Break caught: attaching an arbitrary Kakao polyline when two candidates match.
@pytest.mark.asyncio
async def test_orchestrator_does_not_supplement_ambiguous_public_geometry() -> None:
    placeholder = replace(
        route(
            "public",
            seconds=1_000,
            meters=10_000,
            cost=None,
            source="SEOUL_TRANSIT",
        ),
        warnings=("GEOMETRY_MISSING",),
    )
    public = FakeProvider(
        "SEOUL_TRANSIT",
        ProviderResult(provider="SEOUL_TRANSIT", routes=(placeholder,)),
    )
    alternatives = ProviderResult(
        provider="KAKAO_TRANSIT",
        routes=(
            route("kakao-a", seconds=1_000, meters=10_000, source="KAKAO_TRANSIT"),
            route("kakao-b", seconds=1_010, meters=10_100, source="KAKAO_TRANSIT"),
        ),
    )
    kakao = FakeProvider("KAKAO_TRANSIT", alternatives)
    orchestrator = RouteOrchestrator(
        {TravelMode.TRANSIT: (public, kakao)},
        max_concurrency=1,
    )

    collection = await orchestrator.collect(base_query(), {TravelMode.TRANSIT})

    assert {route.source for route in collection.routes} == {"KAKAO_TRANSIT"}
    assert not any(
        "GEOMETRY_SOURCE=" in warning
        for route in collection.routes
        for warning in route.warnings
    )
    assert [warning.code for warning in collection.warnings] == [
        "GEOMETRY_MATCH_AMBIGUOUS"
    ]
