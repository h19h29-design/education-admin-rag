import asyncio
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

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

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def route(
    route_id: str,
    seconds: int = 600,
    meters: int = 5_000,
    cost: int | None = 3_000,
    *,
    mode: TravelMode = TravelMode.TRANSIT,
    source: str = "FAKE",
    geometry: tuple[Coordinate, ...] | None = None,
) -> RouteOption:
    return RouteOption(
        id=route_id,
        mode=mode,
        duration_seconds=seconds,
        distance_meters=meters,
        mobility_cost_krw=cost,
        cost_status=CostStatus.UNKNOWN if cost is None else CostStatus.KNOWN,
        cost_breakdown=None,
        geometry=geometry or (Coordinate(37.55, 126.97), Coordinate(37.56, 126.98)),
        source=source,
        source_as_of=NOW,
    )


def result_with(item: RouteOption, *, provider: str | None = None) -> ProviderResult:
    return ProviderResult(provider=provider or item.source, routes=(item,))


def failed_result(code: str, *, provider: str) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        routes=(),
        warnings=(
            ProviderWarning(code=code, message="fixture failure", source=provider),
        ),
    )


def base_query(*, mode: TravelMode = TravelMode.TRANSIT) -> RouteQuery:
    return RouteQuery(
        origin=Coordinate(37.55, 126.97),
        destination=Coordinate(37.56, 126.98),
        depart_at=NOW,
        mode=mode,
        car_assumptions=None,
    )


class FakeProvider:
    def __init__(
        self,
        name: str,
        result: ProviderResult,
        *,
        supported_modes: frozenset[TravelMode] = frozenset({TravelMode.TRANSIT}),
        delay_seconds: float = 0,
    ) -> None:
        self.name = name
        self.result = result
        self.supported_modes = supported_modes
        self.delay_seconds = delay_seconds
        self.call_count = 0

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        self.call_count += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.result


class RaisingProvider:
    supported_modes = frozenset({TravelMode.TRANSIT})

    def __init__(self, name: str) -> None:
        self.name = name

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        raise RuntimeError("secret detail must not escape")


@dataclass
class ConcurrencyTracker:
    active: int = 0
    peak: int = 0


class TrackingProvider:
    def __init__(
        self,
        name: str,
        mode: TravelMode,
        tracker: ConcurrencyTracker,
    ) -> None:
        self.name = name
        self.supported_modes = frozenset({mode})
        self._mode = mode
        self._tracker = tracker

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        self._tracker.active += 1
        self._tracker.peak = max(self._tracker.peak, self._tracker.active)
        try:
            await asyncio.sleep(0.01)
        finally:
            self._tracker.active -= 1
        return result_with(
            route(self.name, mode=self._mode, source=self.name),
            provider=self.name,
        )


def known_breakdown(total: int) -> RouteCostBreakdown:
    return RouteCostBreakdown(fare_krw=total)
