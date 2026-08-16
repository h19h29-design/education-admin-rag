import pytest
from app.routing.models import (
    BestRouteIds,
    CarAssumptions,
    Coordinate,
    CostStatus,
    FuelType,
    ProviderResult,
    ProviderWarning,
    RouteCollection,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.ranking import deduplicate_routes, rank_routes
from tests.routing.fakes import NOW, route


class StringSubclass(str):
    pass


class RouteOptionSubclass(RouteOption):
    pass


class ProviderWarningSubclass(ProviderWarning):
    pass


# Break caught: allowing unknown costs to masquerade as zero in cheapest ranking.
def test_rank_routes_selects_fastest_shortest_and_known_cheapest() -> None:
    routes = (
        route("fast", seconds=600, meters=5_000, cost=3_000),
        route("short", seconds=900, meters=3_000, cost=2_000),
        route("cheap", seconds=1_200, meters=4_000, cost=0),
        route("unknown", seconds=300, meters=1_000, cost=None),
    )

    best = rank_routes(routes)

    assert best.fastest_route_id == "unknown"
    assert best.shortest_route_id == "unknown"
    assert best.cheapest_route_id == "cheap"


# Break caught: selecting an arbitrary route when every cost is unavailable.
def test_rank_routes_has_no_cheapest_when_all_costs_are_unknown() -> None:
    best = rank_routes((route("one", cost=None), route("two", cost=None)))

    assert best.cheapest_route_id is None


# Break caught: provider arrival order deciding otherwise tied representatives.
def test_rank_routes_uses_provider_priority_then_route_id_for_ties() -> None:
    routes = (
        route("z", source="LOW"),
        route("b", source="HIGH"),
        route("a", source="HIGH"),
    )

    best = rank_routes(routes, provider_priorities={"HIGH": 0, "LOW": 1})

    assert best.fastest_route_id == "a"
    assert best.shortest_route_id == "a"
    assert best.cheapest_route_id == "a"


# Break caught: returning duplicate alternatives from lower-priority providers.
def test_deduplicate_routes_keeps_higher_priority_for_same_geometry() -> None:
    routes = (
        route("fallback", seconds=604, meters=5_020, source="LOW"),
        route("primary", seconds=600, meters=5_000, source="HIGH"),
    )

    normalized = deduplicate_routes(
        routes,
        provider_priorities={"HIGH": 0, "LOW": 1},
    )

    assert [item.id for item in normalized] == ["primary"]


# Break caught: metric-only dedupe deleting a genuinely different road/path.
def test_deduplicate_routes_preserves_similar_metrics_on_different_geometry() -> None:
    north = (
        Coordinate(37.55, 126.97),
        Coordinate(37.57, 126.98),
        Coordinate(37.56, 126.99),
    )
    south = (
        Coordinate(37.55, 126.97),
        Coordinate(37.54, 126.98),
        Coordinate(37.56, 126.99),
    )

    normalized = deduplicate_routes(
        (
            route("north", geometry=north, source="HIGH"),
            route("south", geometry=south, source="LOW"),
        ),
        provider_priorities={"HIGH": 0, "LOW": 1},
    )

    assert [item.id for item in normalized] == ["north", "south"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", -1),
        ("duration_seconds", True),
        ("distance_meters", -1),
        ("distance_meters", 1.5),
        ("mobility_cost_krw", -1),
        ("mobility_cost_krw", True),
    ],
)
def test_route_option_rejects_invalid_exact_numeric_fields(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "id": "route",
        "mode": TravelMode.TRANSIT,
        "duration_seconds": 60,
        "distance_meters": 100,
        "mobility_cost_krw": 100,
        "cost_status": CostStatus.KNOWN,
        "cost_breakdown": None,
        "geometry": (Coordinate(37.55, 126.97), Coordinate(37.56, 126.98)),
        "source": "FAKE",
        "source_as_of": NOW,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        RouteOption(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "geometry",
    [
        (Coordinate(37.55, 126.97),),
        (Coordinate(float("nan"), 126.97), Coordinate(37.56, 126.98)),
        (Coordinate(91.0, 126.97), Coordinate(37.56, 126.98)),
        (Coordinate(37.55, 181.0), Coordinate(37.56, 126.98)),
    ],
)
def test_route_option_rejects_invalid_geometry(
    geometry: tuple[Coordinate, ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        RouteOption(
            id="route",
            mode=TravelMode.TRANSIT,
            duration_seconds=60,
            distance_meters=100,
            mobility_cost_krw=100,
            cost_status=CostStatus.KNOWN,
            cost_breakdown=None,
            geometry=geometry,
            source="FAKE",
            source_as_of=NOW,
        )


@pytest.mark.parametrize(
    ("status", "cost", "breakdown"),
    [
        (CostStatus.UNKNOWN, 0, None),
        (CostStatus.UNKNOWN, None, RouteCostBreakdown()),
        (CostStatus.KNOWN, None, None),
        (CostStatus.ESTIMATED, 100, RouteCostBreakdown(fare_krw=99)),
    ],
)
def test_route_option_rejects_inconsistent_cost_contract(
    status: CostStatus,
    cost: int | None,
    breakdown: RouteCostBreakdown | None,
) -> None:
    with pytest.raises(ValueError):
        RouteOption(
            id="route",
            mode=TravelMode.TRANSIT,
            duration_seconds=60,
            distance_meters=100,
            mobility_cost_krw=cost,
            cost_status=status,
            cost_breakdown=breakdown,
            geometry=(Coordinate(37.55, 126.97), Coordinate(37.56, 126.98)),
            source="FAKE",
            source_as_of=NOW,
        )


def test_models_reject_blank_ids_naive_timestamps_and_duplicate_routes() -> None:
    with pytest.raises(ValueError):
        route("   ")
    with pytest.raises(ValueError):
        RouteQuery(
            origin=Coordinate(37.55, 126.97),
            destination=Coordinate(37.56, 126.98),
            depart_at=NOW.replace(tzinfo=None),
            mode=TravelMode.TRANSIT,
        )
    item = route("same")
    with pytest.raises(ValueError):
        ProviderResult(provider="FAKE", routes=(item, item))


def test_provider_result_requires_route_source_identity() -> None:
    with pytest.raises(ValueError):
        ProviderResult(provider="OTHER", routes=(route("route"),))


def test_value_objects_reject_non_finite_and_wrong_exact_types() -> None:
    with pytest.raises((TypeError, ValueError)):
        CarAssumptions(FuelType.GASOLINE, float("inf"), 0)
    with pytest.raises((TypeError, ValueError)):
        CarAssumptions(FuelType.GASOLINE, 10.0, True)
    with pytest.raises((TypeError, ValueError)):
        RouteCostBreakdown(fare_krw=True)
    with pytest.raises(ValueError):
        ProviderWarning(code=" ", message="message", source="FAKE")
    with pytest.raises((TypeError, ValueError)):
        RouteQuery(
            origin=Coordinate(37.55, 126.97),
            destination=Coordinate(37.56, float("inf")),
            depart_at=NOW,
            mode=TravelMode.TRANSIT,
        )


def test_coordinate_constructor_keeps_task2_projection_semantics() -> None:
    coordinate = Coordinate(latitude=8_585.0, longitude=8_585.0)

    assert coordinate.latitude == 8_585.0
    assert coordinate.longitude == 8_585.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fastest_route_id", True),
        ("fastest_route_id", 1),
        ("shortest_route_id", StringSubclass("route")),
        ("cheapest_route_id", "   "),
    ],
)
def test_best_route_ids_reject_noncanonical_public_values(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "fastest_route_id": None,
        "shortest_route_id": None,
        "cheapest_route_id": None,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        BestRouteIds(**values)  # type: ignore[arg-type]


def test_route_collection_rejects_noncanonical_route_and_warning_elements() -> None:
    valid = route("valid")
    route_subclass = RouteOptionSubclass(
        id="subclass",
        mode=valid.mode,
        duration_seconds=valid.duration_seconds,
        distance_meters=valid.distance_meters,
        mobility_cost_krw=valid.mobility_cost_krw,
        cost_status=valid.cost_status,
        cost_breakdown=valid.cost_breakdown,
        geometry=valid.geometry,
        source=valid.source,
        source_as_of=valid.source_as_of,
    )
    warning_subclass = ProviderWarningSubclass(
        code="CODE",
        message="message",
        source="FAKE",
    )
    valid_best = BestRouteIds("valid", "valid", "valid")

    with pytest.raises(TypeError):
        RouteCollection(
            routes=(route_subclass,),
            best=BestRouteIds("subclass", "subclass", "subclass"),
            warnings=(),
        )
    with pytest.raises(TypeError):
        RouteCollection(routes=(valid,), best=valid_best, warnings=(warning_subclass,))


def test_route_collection_rejects_inconsistent_best_references() -> None:
    known = route("known", seconds=600, meters=5_000, cost=100)
    slower = route("slower", seconds=700, meters=6_000, cost=200)
    unknown = route("unknown", seconds=500, meters=4_000, cost=None)

    with pytest.raises(ValueError, match="fastest"):
        RouteCollection(
            routes=(known, slower),
            best=BestRouteIds("slower", "known", "known"),
            warnings=(),
        )
    with pytest.raises(ValueError, match="shortest"):
        RouteCollection(
            routes=(known, slower),
            best=BestRouteIds("known", "slower", "known"),
            warnings=(),
        )
    with pytest.raises(ValueError, match="cheapest"):
        RouteCollection(
            routes=(known, unknown),
            best=BestRouteIds("unknown", "unknown", "unknown"),
            warnings=(),
        )
    with pytest.raises(ValueError, match="cheapest"):
        RouteCollection(
            routes=(known, slower),
            best=BestRouteIds("known", "known", None),
            warnings=(),
        )


def test_route_collection_accepts_legitimate_empty_and_all_unknown_results() -> None:
    empty = RouteCollection(
        routes=(),
        best=BestRouteIds(None, None, None),
        warnings=(),
    )
    unknown = route("unknown", cost=None)
    all_unknown = RouteCollection(
        routes=(unknown,),
        best=BestRouteIds("unknown", "unknown", None),
        warnings=(),
    )

    assert empty.routes == ()
    assert all_unknown.best.cheapest_route_id is None
