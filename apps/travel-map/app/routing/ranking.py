from collections.abc import Mapping, Sequence
from math import isfinite

from pyproj import Transformer
from shapely.geometry import LineString  # type: ignore[import-untyped]

from app.routing.models import BestRouteIds, CostStatus, RouteOption

_WGS84_TO_5179 = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
_METRIC_TOLERANCE = 0.02
_ENDPOINT_TOLERANCE_METERS = 20.0
_BUFFER_METERS = 10.0
_MIN_MUTUAL_OVERLAP = 0.95


def rank_routes(
    routes: Sequence[RouteOption],
    *,
    provider_priorities: Mapping[str, int] | None = None,
) -> BestRouteIds:
    priorities = _complete_priorities(routes, provider_priorities)
    if not routes:
        return BestRouteIds(None, None, None)

    fastest = min(
        routes,
        key=lambda route: (
            route.duration_seconds,
            route.duration_seconds,
            route.distance_meters,
            priorities[route.source],
            route.id,
        ),
    )
    shortest = min(
        routes,
        key=lambda route: (
            route.distance_meters,
            route.duration_seconds,
            route.distance_meters,
            priorities[route.source],
            route.id,
        ),
    )
    known_costs = tuple(
        route
        for route in routes
        if route.cost_status is not CostStatus.UNKNOWN
        and route.mobility_cost_krw is not None
    )
    cheapest = (
        min(
            known_costs,
            key=lambda route: (
                route.mobility_cost_krw,
                route.duration_seconds,
                route.distance_meters,
                priorities[route.source],
                route.id,
            ),
        )
        if known_costs
        else None
    )
    return BestRouteIds(
        fastest_route_id=fastest.id,
        shortest_route_id=shortest.id,
        cheapest_route_id=cheapest.id if cheapest is not None else None,
    )


def deduplicate_routes(
    routes: Sequence[RouteOption],
    *,
    provider_priorities: Mapping[str, int] | None = None,
) -> tuple[RouteOption, ...]:
    priorities = _complete_priorities(routes, provider_priorities)
    ordered = sorted(
        routes,
        key=lambda route: (priorities[route.source], route.id),
    )
    kept: list[RouteOption] = []
    for candidate in ordered:
        if any(_routes_are_duplicates(candidate, existing) for existing in kept):
            continue
        kept.append(candidate)
    return tuple(kept)


def _complete_priorities(
    routes: Sequence[RouteOption],
    supplied: Mapping[str, int] | None,
) -> dict[str, int]:
    priorities: dict[str, int] = {}
    if supplied is not None:
        for source, priority in supplied.items():
            if type(source) is not str or not source.strip():
                raise ValueError("provider priority source must be nonblank")
            if type(priority) is not int or priority < 0:
                raise ValueError("provider priorities must be nonnegative integers")
            priorities[source] = priority
    next_priority = max(priorities.values(), default=-1) + 1
    for route in routes:
        if route.source not in priorities:
            priorities[route.source] = next_priority
            next_priority += 1
    return priorities


def _routes_are_duplicates(left: RouteOption, right: RouteOption) -> bool:
    if left.mode is not right.mode:
        return False
    if not _within_relative_tolerance(left.duration_seconds, right.duration_seconds):
        return False
    if not _within_relative_tolerance(left.distance_meters, right.distance_meters):
        return False

    left_points = _project_geometry(left)
    right_points = _project_geometry(right)
    if _point_distance(left_points[0], right_points[0]) > _ENDPOINT_TOLERANCE_METERS:
        return False
    if _point_distance(left_points[-1], right_points[-1]) > _ENDPOINT_TOLERANCE_METERS:
        return False

    left_buffer = LineString(left_points).buffer(_BUFFER_METERS)
    right_buffer = LineString(right_points).buffer(_BUFFER_METERS)
    if left_buffer.area <= 0 or right_buffer.area <= 0:
        return False
    intersection_area = left_buffer.intersection(right_buffer).area
    left_overlap = intersection_area / left_buffer.area
    right_overlap = intersection_area / right_buffer.area
    return (
        isfinite(left_overlap)
        and isfinite(right_overlap)
        and left_overlap >= _MIN_MUTUAL_OVERLAP
        and right_overlap >= _MIN_MUTUAL_OVERLAP
    )


def _within_relative_tolerance(left: int, right: int) -> bool:
    denominator = max(left, right)
    if denominator == 0:
        return True
    return abs(left - right) / denominator <= _METRIC_TOLERANCE


def _project_geometry(route: RouteOption) -> tuple[tuple[float, float], ...]:
    return tuple(
        _WGS84_TO_5179.transform(point.longitude, point.latitude)
        for point in route.geometry
    )


def _point_distance(
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    return ((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2) ** 0.5
