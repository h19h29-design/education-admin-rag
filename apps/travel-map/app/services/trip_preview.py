"""Trip-preview orchestration that keeps legal classification independent of display routes."""

import asyncio
from dataclasses import replace
from typing import Literal

from app.cache import CAR_AND_TRANSIT_TTL_SECONDS, WALK_TTL_SECONDS
from app.contracts import (
    AllowanceResponse,
    AmountResponse,
    BestResponse,
    ClassificationPathResponse,
    CoordinateResponse,
    CoverageResponse,
    OriginResponse,
    RouteCostResponse,
    RouteResponse,
    TripPreviewRequest,
    TripPreviewResponse,
)
from app.dependencies import AppDependencies
from app.institutions.models import InstitutionSite
from app.policy.models import (
    AllowanceResult,
    AllowanceStatus,
    Classification,
    CoverageState,
    PolicyInput,
    PolicyResult,
)
from app.routing.models import (
    CarAssumptions,
    Coordinate,
    CostStatus,
    ProviderResult,
    RouteCollection,
    RouteOption,
    RouteQuery,
    TravelMode,
)


class TripPreviewService:
    def __init__(self, dependencies: AppDependencies) -> None:
        self._dependencies = dependencies

    async def preview(self, request: TripPreviewRequest) -> TripPreviewResponse:
        site = self._dependencies.institutions.require_site(request.origin_site_id)
        origin = _site_coordinate(site)
        destination = Coordinate(
            latitude=request.destination.latitude,
            longitude=request.destination.longitude,
        )
        coverage_state = self._dependencies.coverage.classify(destination)
        origin_response = _origin_response(site, origin)
        snapshot_id = _snapshot_id(self._dependencies, site.site_id)

        if coverage_state is CoverageState.OUTSIDE:
            return _outside_response(
                request=request,
                origin=origin_response,
                snapshot_id=snapshot_id,
            )

        car_assumptions = CarAssumptions(
            fuel_type=request.car_assumptions.fuel_type,
            efficiency_km_per_liter=request.car_assumptions.efficiency_km_per_liter,
            parking_cost_krw=request.car_assumptions.parking_cost_krw,
        )
        display_query = RouteQuery(
            origin=origin,
            destination=destination,
            depart_at=request.starts_at,
            mode=TravelMode.CAR,
            car_assumptions=car_assumptions,
        )
        display_routes = await self._display_routes(display_query)
        outbound = await self._classification_route(display_query)
        returning = await self._classification_route(
            RouteQuery(
                origin=destination,
                destination=origin,
                depart_at=request.returns_at,
                mode=TravelMode.CAR,
                car_assumptions=car_assumptions,
            )
        )
        classification_path, classification_distance = _classification_path(
            outbound,
            returning,
        )
        policy, data_warnings = self._policy_result(
            request=request,
            coverage_state=coverage_state,
            classification_distance=classification_distance,
        )
        warnings = (
            tuple(warning.code for warning in display_routes.warnings) + data_warnings
        )
        routes = tuple(_route_response(route) for route in display_routes.routes)
        best = BestResponse(
            fastest_route_id=display_routes.best.fastest_route_id,
            shortest_route_id=display_routes.best.shortest_route_id,
            cheapest_route_id=display_routes.best.cheapest_route_id,
        )
        return TripPreviewResponse(
            coverage=CoverageResponse(status=_coverage_status(coverage_state)),
            origin=origin_response,
            institution_snapshot_id=snapshot_id,
            policy_scope=request.policy_profile,
            classification=policy.classification.value,
            classification_distance_meters=classification_distance,
            classification_path=classification_path,
            routes=routes,
            best=best,
            mobility_cost=_mobility_cost(display_routes.routes, best.fastest_route_id),
            allowance=AllowanceResponse(
                status=policy.allowance.status.value,
                amount_krw=policy.allowance.amount_krw,
                warnings=policy.allowance.warnings,
            ),
            rule_set_id=policy.rule_set_id,
            effective_from=policy.effective_from,
            source_refs=policy.source_refs,
            warnings=warnings,
        )

    async def _display_routes(self, query: RouteQuery) -> RouteCollection:
        collections = await asyncio.gather(
            *(self._display_mode_routes(query, mode) for mode in TravelMode)
        )
        return self._dependencies.route_orchestrator.combine_collections(
            dict(zip(TravelMode, collections, strict=True))
        )

    async def _display_mode_routes(
        self,
        query: RouteQuery,
        mode: TravelMode,
    ) -> RouteCollection:
        key = self._dependencies.cache.route_key(
            provider="ROUTE_ORCHESTRATOR",
            mode=mode,
            origin=query.origin,
            destination=query.destination,
            depart_at=query.depart_at,
            options=(
                {
                    "fuelType": (
                        query.car_assumptions.fuel_type.value
                        if query.car_assumptions is not None
                        else None
                    ),
                    "efficiencyKmPerLiter": (
                        query.car_assumptions.efficiency_km_per_liter
                        if query.car_assumptions is not None
                        else None
                    ),
                    "parkingCostKrw": (
                        query.car_assumptions.parking_cost_krw
                        if query.car_assumptions is not None
                        else None
                    ),
                }
                if mode is TravelMode.CAR
                else {}
            ),
        )
        cached = self._dependencies.cache.get(key)
        if type(cached) is RouteCollection:
            return cached
        collected = await self._dependencies.route_orchestrator.collect(
            query,
            {mode},
        )
        return self._dependencies.cache.set(
            key,
            collected,
            ttl_seconds=(
                WALK_TTL_SECONDS
                if mode is TravelMode.WALK
                else CAR_AND_TRANSIT_TTL_SECONDS
            ),
        )

    async def _classification_route(self, query: RouteQuery) -> RouteOption | None:
        provider = self._dependencies.classification_provider
        key = self._dependencies.cache.route_key(
            provider=provider.name,
            mode=TravelMode.CAR,
            origin=query.origin,
            destination=query.destination,
            depart_at=query.depart_at,
            options={"priority": "DISTANCE", "alternatives": False},
        )
        cached = self._dependencies.cache.get(key)
        if type(cached) is RouteOption:
            return cached
        try:
            result = await provider.get_routes(query)
        except Exception:  # noqa: BLE001 - provider details must not become public
            return None
        if (
            type(result) is not ProviderResult
            or result.provider != provider.name
            or not result.routes
        ):
            return None
        route = result.routes[0]
        if route.mode is not TravelMode.CAR:
            return None
        return self._dependencies.cache.set(
            key,
            route,
            ttl_seconds=CAR_AND_TRANSIT_TTL_SECONDS,
        )

    def _policy_result(
        self,
        *,
        request: TripPreviewRequest,
        coverage_state: CoverageState,
        classification_distance: int | None,
    ) -> tuple[PolicyResult, tuple[str, ...]]:
        is_seoul = coverage_state is CoverageState.SEOUL
        data_unavailable = classification_distance is None
        policy = self._dependencies.policy.calculate(
            PolicyInput(
                destination_in_seoul=is_seoul,
                round_trip_distance_m=classification_distance or 0,
                starts_at=request.starts_at,
                returns_at=request.returns_at,
                policy_profile=request.policy_profile,
                vehicle_use=request.vehicle_use,
                has_other_local_trips_today=request.has_other_local_trips_today,
                previous_allowance_krw=request.previous_allowance_krw,
            )
        )
        if not data_unavailable:
            return policy, ()
        classification = (
            Classification.LOCAL if is_seoul else Classification.REVIEW_REQUIRED
        )
        return (
            replace(
                policy,
                classification=classification,
                allowance=AllowanceResult(
                    status=AllowanceStatus.REVIEW_REQUIRED,
                    amount_krw=None,
                    warnings=("DATA_UNAVAILABLE",),
                ),
            ),
            ("DATA_UNAVAILABLE",),
        )


def _site_coordinate(site: InstitutionSite) -> Coordinate:
    if site.routing_anchor_latitude is None or site.routing_anchor_longitude is None:
        raise ValueError("active institution site has no verified routing anchor")
    return Coordinate(
        latitude=site.routing_anchor_latitude,
        longitude=site.routing_anchor_longitude,
    )


def _origin_response(site: InstitutionSite, coordinate: Coordinate) -> OriginResponse:
    return OriginResponse(
        site_id=site.site_id,
        name=site.site_name,
        address=site.road_address,
        coordinate=CoordinateResponse(
            latitude=coordinate.latitude,
            longitude=coordinate.longitude,
        ),
    )


def _snapshot_id(dependencies: AppDependencies, site_id: str) -> str | None:
    for item in dependencies.institutions.search(query=site_id, limit=50):
        if item.site_id == site_id:
            return item.snapshot_id
    return None


def _coverage_status(
    state: CoverageState,
) -> Literal["SEOUL", "BUFFER", "OUT_OF_COVERAGE"]:
    if state is CoverageState.SEOUL:
        return "SEOUL"
    if state is CoverageState.BUFFER:
        return "BUFFER"
    if state is CoverageState.OUTSIDE:
        return "OUT_OF_COVERAGE"
    raise ValueError("unsupported coverage state")


def _outside_response(
    *,
    request: TripPreviewRequest,
    origin: OriginResponse,
    snapshot_id: str | None,
) -> TripPreviewResponse:
    unavailable = AmountResponse(
        status="UNAVAILABLE",
        amount_krw=None,
        warnings=("OUT_OF_COVERAGE",),
    )
    return TripPreviewResponse(
        coverage=CoverageResponse(status="OUT_OF_COVERAGE"),
        origin=origin,
        institution_snapshot_id=snapshot_id,
        policy_scope=request.policy_profile,
        classification=Classification.REVIEW_REQUIRED.value,
        classification_distance_meters=None,
        classification_path=None,
        routes=(),
        best=BestResponse(
            fastest_route_id=None,
            shortest_route_id=None,
            cheapest_route_id=None,
        ),
        mobility_cost=unavailable,
        allowance=AllowanceResponse(
            status=AllowanceStatus.REVIEW_REQUIRED.value,
            amount_krw=None,
            warnings=("OUT_OF_COVERAGE",),
        ),
        rule_set_id=None,
        effective_from=None,
        source_refs=(),
        warnings=("OUT_OF_COVERAGE",),
    )


def _classification_path(
    outbound: RouteOption | None,
    returning: RouteOption | None,
) -> tuple[ClassificationPathResponse | None, int | None]:
    if outbound is None or returning is None:
        return None, None
    geometry = outbound.geometry + returning.geometry
    return (
        ClassificationPathResponse(
            id=f"{outbound.id}+{returning.id}",
            distance_meters=outbound.distance_meters + returning.distance_meters,
            geometry=tuple(_coordinate_response(item) for item in geometry),
            source=f"{outbound.source}+{returning.source}",
            queried_at=max(outbound.source_as_of, returning.source_as_of),
        ),
        outbound.distance_meters + returning.distance_meters,
    )


def _route_response(route: RouteOption) -> RouteResponse:
    breakdown = route.cost_breakdown
    return RouteResponse(
        id=route.id,
        mode=route.mode,
        duration_seconds=route.duration_seconds,
        distance_meters=route.distance_meters,
        mobility_cost_krw=route.mobility_cost_krw,
        cost_status=route.cost_status.value,
        cost_breakdown=(
            RouteCostResponse(
                fare_krw=breakdown.fare_krw,
                fuel_krw=breakdown.fuel_krw,
                toll_krw=breakdown.toll_krw,
                parking_krw=breakdown.parking_krw,
            )
            if breakdown is not None
            else None
        ),
        geometry=tuple(_coordinate_response(item) for item in route.geometry),
        source=route.source,
        source_as_of=route.source_as_of,
        warnings=route.warnings,
    )


def _coordinate_response(value: Coordinate) -> CoordinateResponse:
    return CoordinateResponse(latitude=value.latitude, longitude=value.longitude)


def _mobility_cost(
    routes: tuple[RouteOption, ...],
    fastest_route_id: str | None,
) -> AmountResponse:
    route = next((item for item in routes if item.id == fastest_route_id), None)
    if route is None:
        return AmountResponse(status="UNAVAILABLE", amount_krw=None)
    status = (
        route.cost_status.value
        if route.cost_status is not CostStatus.UNKNOWN
        else "UNAVAILABLE"
    )
    return AmountResponse(status=status, amount_krw=route.mobility_cost_krw)
