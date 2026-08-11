import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import replace
from inspect import iscoroutinefunction
from math import cos, isfinite, radians, sqrt

from app.routing.models import (
    Coordinate,
    ProviderResult,
    ProviderWarning,
    RouteCollection,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.provider import RouteProvider
from app.routing.ranking import deduplicate_routes, rank_routes


class RouteOrchestrator:
    def __init__(
        self,
        providers: Mapping[TravelMode, tuple[RouteProvider, ...]],
        *,
        max_concurrency: int,
        provider_timeout_seconds: float = 5.0,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if (
            type(provider_timeout_seconds) is not float
            or not isfinite(provider_timeout_seconds)
            or provider_timeout_seconds <= 0
        ):
            raise ValueError("provider_timeout_seconds must be a positive finite float")
        normalized: dict[TravelMode, tuple[RouteProvider, ...]] = {}
        provider_names: set[str] = set()
        for mode, chain in providers.items():
            if type(mode) is not TravelMode:
                raise TypeError("provider registry keys must be TravelMode")
            if type(chain) is not tuple:
                raise TypeError("provider chains must be tuples")
            for provider in chain:
                _validate_provider(provider)
                if provider.name in provider_names:
                    raise ValueError("provider names must be globally unique")
                provider_names.add(provider.name)
            normalized[mode] = chain
        self._providers = normalized
        self._provider_timeout_seconds = provider_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def collect(
        self,
        query_base: RouteQuery,
        requested_modes: Iterable[TravelMode],
    ) -> RouteCollection:
        if type(query_base) is not RouteQuery:
            raise TypeError("query_base must be RouteQuery")
        requested = set(requested_modes)
        if any(type(mode) is not TravelMode for mode in requested):
            raise TypeError("requested_modes must contain TravelMode values")
        ordered_modes = tuple(mode for mode in TravelMode if mode in requested)
        outcomes = await asyncio.gather(
            *(self._collect_mode(query_base, mode) for mode in ordered_modes)
        )
        return self._build_collection(ordered_modes, outcomes)

    def combine_collections(
        self,
        collections: Mapping[TravelMode, RouteCollection],
    ) -> RouteCollection:
        """Apply the normal cross-mode ranking to independently collected modes."""

        ordered_modes = tuple(mode for mode in TravelMode if mode in collections)
        if any(type(mode) is not TravelMode for mode in collections):
            raise TypeError("collection keys must be TravelMode")
        outcomes: list[tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]] = []
        for mode in ordered_modes:
            collection = collections[mode]
            if type(collection) is not RouteCollection:
                raise TypeError("collections must contain RouteCollection values")
            outcomes.append((collection.routes, collection.warnings))
        return self._build_collection(ordered_modes, outcomes)

    def _build_collection(
        self,
        ordered_modes: tuple[TravelMode, ...],
        outcomes: Iterable[tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]],
    ) -> RouteCollection:

        routes: list[RouteOption] = []
        warnings: list[ProviderWarning] = []
        priorities: dict[str, int] = {}
        priority = 0
        for mode in ordered_modes:
            for provider in self._providers.get(mode, ()):
                priorities.setdefault(provider.name, priority)
                priority += 1
        for mode_routes, mode_warnings in outcomes:
            routes.extend(mode_routes)
            warnings.extend(mode_warnings)

        unique_routes, duplicate_warnings = _drop_duplicate_route_ids(routes)
        warnings.extend(duplicate_warnings)
        for route in unique_routes:
            if route.source not in priorities and "+KAKAO_GEOMETRY" in route.source:
                original_source = route.source.split("+", 1)[0]
                priorities[route.source] = priorities.get(
                    original_source,
                    max(priorities.values(), default=-1) + 1,
                )
        normalized_routes = deduplicate_routes(
            unique_routes,
            provider_priorities=priorities,
        )
        return RouteCollection(
            routes=normalized_routes,
            best=rank_routes(
                normalized_routes,
                provider_priorities=priorities,
            ),
            warnings=tuple(warnings),
        )

    async def _collect_mode(
        self,
        query_base: RouteQuery,
        mode: TravelMode,
    ) -> tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]:
        query = replace(
            query_base,
            mode=mode,
            car_assumptions=(
                query_base.car_assumptions if mode is TravelMode.CAR else None
            ),
        )
        chain = self._providers.get(mode, ())
        if not chain:
            warning = ProviderWarning(
                code="NO_PROVIDER",
                message=f"No provider is configured for {mode.value}",
                source="ROUTE_ORCHESTRATOR",
            )
            return (), (warning,)

        warnings: list[ProviderWarning] = []
        geometry_placeholders: ProviderResult | None = None
        for provider in chain:
            if mode not in provider.supported_modes:
                warnings.append(
                    ProviderWarning(
                        code="CAPABILITY_MISSING",
                        message=f"Provider does not support {mode.value}",
                        source=provider.name,
                    )
                )
                continue
            try:
                async with self._semaphore:
                    result = await asyncio.wait_for(
                        provider.get_routes(query),
                        timeout=self._provider_timeout_seconds,
                    )
            except TimeoutError:
                warnings.append(
                    ProviderWarning(
                        code="UPSTREAM_TIMEOUT",
                        message="Route provider timed out",
                        source=provider.name,
                    )
                )
                continue
            except Exception:  # noqa: BLE001
                warnings.append(
                    ProviderWarning(
                        code="UPSTREAM_ERROR",
                        message="Route provider request failed",
                        source=provider.name,
                    )
                )
                continue

            if type(result) is not ProviderResult:
                warnings.append(
                    ProviderWarning(
                        code="INVALID_PROVIDER_RESULT",
                        message="Route provider returned an invalid result",
                        source=provider.name,
                    )
                )
                continue
            if result.provider != provider.name:
                warnings.append(
                    ProviderWarning(
                        code="PROVIDER_IDENTITY_MISMATCH",
                        message="Route provider identity did not match its registry entry",
                        source=provider.name,
                    )
                )
                continue
            warnings.extend(result.warnings)
            if any(route.mode is not mode for route in result.routes):
                warnings.append(
                    ProviderWarning(
                        code="MODE_MISMATCH",
                        message="Route provider returned a different travel mode",
                        source=provider.name,
                    )
                )
                continue
            if result.routes:
                if provider.name == "SEOUL_TRANSIT" and any(
                    "GEOMETRY_MISSING" in route.warnings for route in result.routes
                ):
                    geometry_placeholders = result
                    continue
                if (
                    geometry_placeholders is not None
                    and provider.name == "KAKAO_TRANSIT"
                ):
                    supplemented, match_warning = _supplement_public_geometry(
                        geometry_placeholders.routes,
                        result.routes,
                    )
                    warnings.append(match_warning)
                    return supplemented, tuple(warnings)
                return result.routes, tuple(warnings)
            if not result.warnings:
                warnings.append(
                    ProviderWarning(
                        code="NO_ROUTES",
                        message="Route provider returned no routes",
                        source=provider.name,
                    )
                )
        if geometry_placeholders is not None:
            warnings.append(
                ProviderWarning(
                    code="GEOMETRY_UNAVAILABLE",
                    message="No defensible geometry supplement was available",
                    source="ROUTE_ORCHESTRATOR",
                )
            )
        return (), tuple(warnings)


def _drop_duplicate_route_ids(
    routes: list[RouteOption],
) -> tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]:
    seen: set[str] = set()
    kept: list[RouteOption] = []
    warnings: list[ProviderWarning] = []
    for route in routes:
        if route.id in seen:
            warnings.append(
                ProviderWarning(
                    code="DUPLICATE_ROUTE_ID",
                    message="A duplicate route id was excluded",
                    source=route.source,
                )
            )
            continue
        seen.add(route.id)
        kept.append(route)
    return tuple(kept), tuple(warnings)


def _validate_provider(provider: object) -> None:
    name = getattr(provider, "name", None)
    if type(name) is not str:
        raise TypeError("provider name must be str")
    if not name.strip():
        raise ValueError("provider name must be nonblank")
    supported_modes = getattr(provider, "supported_modes", None)
    if type(supported_modes) is not frozenset:
        raise TypeError("provider supported_modes must be frozenset")
    if any(type(mode) is not TravelMode for mode in supported_modes):
        raise TypeError("provider supported_modes must contain TravelMode values")
    get_routes = getattr(provider, "get_routes", None)
    if not callable(get_routes):
        raise TypeError("provider get_routes must be callable")
    if not iscoroutinefunction(get_routes) and not iscoroutinefunction(
        type(get_routes).__call__
    ):
        raise TypeError("provider get_routes must be async")


def _supplement_public_geometry(
    public_routes: tuple[RouteOption, ...],
    kakao_routes: tuple[RouteOption, ...],
) -> tuple[tuple[RouteOption, ...], ProviderWarning]:
    candidate_edges = tuple(
        tuple(
            index
            for index, kakao_route in enumerate(kakao_routes)
            if _geometry_candidate_matches(public_route, kakao_route)
        )
        for public_route in public_routes
    )
    if any(not edges for edges in candidate_edges):
        return kakao_routes, ProviderWarning(
            code="GEOMETRY_MATCH_NOT_FOUND",
            message="Public route geometry could not be matched uniquely",
            source="ROUTE_ORCHESTRATOR",
        )
    assignment = _find_perfect_geometry_matching(
        candidate_edges,
        candidate_count=len(kakao_routes),
    )
    if assignment is None or any(
        _find_perfect_geometry_matching(
            candidate_edges,
            candidate_count=len(kakao_routes),
            forbidden=(public_index, candidate_index),
        )
        is not None
        for public_index, candidate_index in enumerate(assignment)
    ):
        return kakao_routes, ProviderWarning(
            code="GEOMETRY_MATCH_AMBIGUOUS",
            message="Public route geometry could not be matched uniquely",
            source="ROUTE_ORCHESTRATOR",
        )
    matches = tuple(
        (public_route, kakao_routes[candidate_index])
        for public_route, candidate_index in zip(
            public_routes,
            assignment,
            strict=True,
        )
    )

    supplemented = tuple(
        replace(
            public_route,
            id=f"{public_route.id}+geometry:{kakao_route.id}",
            geometry=kakao_route.geometry,
            source=f"{public_route.source}+KAKAO_GEOMETRY",
            warnings=tuple(
                warning
                for warning in public_route.warnings
                if warning != "GEOMETRY_MISSING"
            )
            + (f"GEOMETRY_SOURCE={kakao_route.source}:{kakao_route.id}",),
        )
        for public_route, kakao_route in matches
    )
    return supplemented, ProviderWarning(
        code="GEOMETRY_SUPPLEMENTED",
        message="Public route geometry was matched to a unique Kakao route",
        source="ROUTE_ORCHESTRATOR",
    )


def _find_perfect_geometry_matching(
    candidate_edges: tuple[tuple[int, ...], ...],
    *,
    candidate_count: int,
    forbidden: tuple[int, int] | None = None,
) -> tuple[int, ...] | None:
    candidate_to_public = [-1] * candidate_count

    def augment(public_index: int, seen: set[int]) -> bool:
        for candidate_index in candidate_edges[public_index]:
            if forbidden == (public_index, candidate_index) or candidate_index in seen:
                continue
            seen.add(candidate_index)
            previous_public = candidate_to_public[candidate_index]
            if previous_public == -1 or augment(previous_public, seen):
                candidate_to_public[candidate_index] = public_index
                return True
        return False

    for public_index in range(len(candidate_edges)):
        if not augment(public_index, set()):
            return None
    public_to_candidate = [-1] * len(candidate_edges)
    for candidate_index, public_index in enumerate(candidate_to_public):
        if public_index != -1:
            public_to_candidate[public_index] = candidate_index
    if any(candidate_index == -1 for candidate_index in public_to_candidate):
        return None
    return tuple(public_to_candidate)


def _geometry_candidate_matches(public: RouteOption, candidate: RouteOption) -> bool:
    if public.mode is not candidate.mode:
        return False
    if not _within_two_percent(public.duration_seconds, candidate.duration_seconds):
        return False
    if not _within_two_percent(public.distance_meters, candidate.distance_meters):
        return False
    return (
        _coordinate_distance_meters(public.geometry[0], candidate.geometry[0]) <= 20.0
        and _coordinate_distance_meters(public.geometry[-1], candidate.geometry[-1])
        <= 20.0
    )


def _within_two_percent(left: int, right: int) -> bool:
    denominator = max(left, right)
    return denominator == 0 or abs(left - right) / denominator <= 0.02


def _coordinate_distance_meters(left: Coordinate, right: Coordinate) -> float:
    left_latitude = radians(left.latitude)
    right_latitude = radians(right.latitude)
    mean_latitude = (left_latitude + right_latitude) / 2.0
    latitude_delta = right_latitude - left_latitude
    longitude_delta = radians(right.longitude - left.longitude)
    return 6_371_008.8 * sqrt(
        latitude_delta**2 + (cos(mean_latitude) * longitude_delta) ** 2
    )
