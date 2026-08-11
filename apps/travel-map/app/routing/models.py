from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite


@dataclass(frozen=True)
class Coordinate:
    """A coordinate pair whose interpretation is defined by its consumer.

    Task 2 also uses this value object with projected test coordinates, so
    geographic bounds are deliberately enforced at routing query/geometry
    boundaries rather than here.
    """

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

    def __post_init__(self) -> None:
        if type(self.fuel_type) is not FuelType:
            raise TypeError("fuel_type must be FuelType")
        _require_finite_float(
            self.efficiency_km_per_liter,
            "efficiency_km_per_liter",
        )
        if self.efficiency_km_per_liter <= 0:
            raise ValueError("efficiency_km_per_liter must be positive")
        _require_nonnegative_int(self.parking_cost_krw, "parking_cost_krw")


@dataclass(frozen=True)
class RouteCostBreakdown:
    fare_krw: int = 0
    fuel_krw: int = 0
    toll_krw: int = 0
    parking_krw: int = 0

    def __post_init__(self) -> None:
        for name in ("fare_krw", "fuel_krw", "toll_krw", "parking_krw"):
            _require_nonnegative_int(getattr(self, name), name)

    @property
    def total_krw(self) -> int:
        return self.fare_krw + self.fuel_krw + self.toll_krw + self.parking_krw


@dataclass(frozen=True)
class RouteQuery:
    origin: Coordinate
    destination: Coordinate
    depart_at: datetime
    mode: TravelMode
    car_assumptions: CarAssumptions | None = None

    def __post_init__(self) -> None:
        _require_geographic_coordinate(self.origin, "origin")
        _require_geographic_coordinate(self.destination, "destination")
        _require_aware_datetime(self.depart_at, "depart_at")
        if type(self.mode) is not TravelMode:
            raise TypeError("mode must be TravelMode")
        if self.car_assumptions is not None:
            if type(self.car_assumptions) is not CarAssumptions:
                raise TypeError("car_assumptions must be CarAssumptions or None")
            if self.mode is not TravelMode.CAR:
                raise ValueError("car_assumptions are only valid for CAR queries")


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

    def __post_init__(self) -> None:
        _require_nonblank_string(self.id, "id")
        if type(self.mode) is not TravelMode:
            raise TypeError("mode must be TravelMode")
        _require_nonnegative_int(self.duration_seconds, "duration_seconds")
        _require_nonnegative_int(self.distance_meters, "distance_meters")
        if self.mobility_cost_krw is not None:
            _require_nonnegative_int(self.mobility_cost_krw, "mobility_cost_krw")
        if type(self.cost_status) is not CostStatus:
            raise TypeError("cost_status must be CostStatus")
        if (
            self.cost_breakdown is not None
            and type(self.cost_breakdown) is not RouteCostBreakdown
        ):
            raise TypeError("cost_breakdown must be RouteCostBreakdown or None")
        _validate_cost_contract(self)
        if type(self.geometry) is not tuple:
            raise TypeError("geometry must be a tuple")
        if len(self.geometry) < 2:
            raise ValueError("geometry must contain at least two coordinates")
        for index, coordinate in enumerate(self.geometry):
            _require_geographic_coordinate(coordinate, f"geometry[{index}]")
        _require_nonblank_string(self.source, "source")
        _require_aware_datetime(self.source_as_of, "source_as_of")
        if type(self.warnings) is not tuple:
            raise TypeError("warnings must be a tuple")
        for index, warning in enumerate(self.warnings):
            _require_nonblank_string(warning, f"warnings[{index}]")


@dataclass(frozen=True)
class ProviderWarning:
    code: str
    message: str
    source: str

    def __post_init__(self) -> None:
        _require_nonblank_string(self.code, "code")
        _require_nonblank_string(self.message, "message")
        _require_nonblank_string(self.source, "source")


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    routes: tuple[RouteOption, ...]
    warnings: tuple[ProviderWarning, ...] = ()

    def __post_init__(self) -> None:
        _require_nonblank_string(self.provider, "provider")
        if type(self.routes) is not tuple:
            raise TypeError("routes must be a tuple")
        route_ids: set[str] = set()
        for route in self.routes:
            if type(route) is not RouteOption:
                raise TypeError("routes must contain RouteOption values")
            if route.source != self.provider:
                raise ValueError("route source must match result provider")
            if route.id in route_ids:
                raise ValueError("route ids must be unique within a provider result")
            route_ids.add(route.id)
        if type(self.warnings) is not tuple:
            raise TypeError("warnings must be a tuple")
        for warning in self.warnings:
            if type(warning) is not ProviderWarning:
                raise TypeError("warnings must contain ProviderWarning values")
            if warning.source != self.provider:
                raise ValueError("warning source must match result provider")


@dataclass(frozen=True)
class BestRouteIds:
    fastest_route_id: str | None
    shortest_route_id: str | None
    cheapest_route_id: str | None


@dataclass(frozen=True)
class RouteCollection:
    routes: tuple[RouteOption, ...]
    best: BestRouteIds
    warnings: tuple[ProviderWarning, ...]

    def __post_init__(self) -> None:
        if type(self.routes) is not tuple:
            raise TypeError("routes must be a tuple")
        if type(self.best) is not BestRouteIds:
            raise TypeError("best must be BestRouteIds")
        if type(self.warnings) is not tuple:
            raise TypeError("warnings must be a tuple")
        route_ids = [route.id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route collection ids must be unique")
        known_ids = set(route_ids)
        for route_id in (
            self.best.fastest_route_id,
            self.best.shortest_route_id,
            self.best.cheapest_route_id,
        ):
            if route_id is not None and route_id not in known_ids:
                raise ValueError("best route ids must refer to collection routes")


def _validate_cost_contract(route: RouteOption) -> None:
    if route.cost_status is CostStatus.UNKNOWN:
        if route.mobility_cost_krw is not None or route.cost_breakdown is not None:
            raise ValueError("UNKNOWN costs must not contain a value or breakdown")
        return
    if route.mobility_cost_krw is None:
        raise ValueError("known or estimated costs require a value")
    if (
        route.cost_breakdown is not None
        and route.cost_breakdown.total_krw != route.mobility_cost_krw
    ):
        raise ValueError("cost breakdown total must match mobility cost")


def _require_geographic_coordinate(value: object, field: str) -> None:
    if type(value) is not Coordinate:
        raise TypeError(f"{field} must be Coordinate")
    _require_finite_float(value.latitude, f"{field}.latitude")
    _require_finite_float(value.longitude, f"{field}.longitude")
    if not -90 <= value.latitude <= 90:
        raise ValueError(f"{field}.latitude is outside WGS84 bounds")
    if not -180 <= value.longitude <= 180:
        raise ValueError(f"{field}.longitude is outside WGS84 bounds")


def _require_aware_datetime(value: object, field: str) -> None:
    if type(value) is not datetime:
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_nonblank_string(value: object, field: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field} must be str")
    if not value.strip():
        raise ValueError(f"{field} must be nonblank")


def _require_finite_float(value: object, field: str) -> None:
    if type(value) is not float:
        raise TypeError(f"{field} must be float")
    if not isfinite(value):
        raise ValueError(f"{field} must be finite")


def _require_nonnegative_int(value: object, field: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{field} must be int")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
