"""Pydantic contracts for the public no-login API."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from app.policy.models import PolicyProfile, VehicleUse
from app.routing.models import FuelType, TravelMode


class ApiModel(BaseModel):
    """Strict external model using camelCase exactly at the HTTP boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ApiRequestModel(BaseModel):
    """Strict HTTP input model that accepts only its documented camelCase aliases."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class DestinationInput(ApiRequestModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    address: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    latitude: Annotated[float, Field(ge=33.0, le=39.5)]
    longitude: Annotated[float, Field(ge=124.0, le=132.0)]


class CarAssumptionsInput(ApiRequestModel):
    fuel_type: FuelType
    efficiency_km_per_liter: Annotated[float, Field(ge=3.0, le=30.0)]
    parking_cost_krw: Annotated[int, Field(ge=0, le=100_000)]


class TripPreviewRequest(ApiRequestModel):
    origin_site_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9-]*:[A-Za-z0-9:_-]+$"),
    ]
    destination: DestinationInput
    starts_at: datetime
    returns_at: datetime
    policy_profile: PolicyProfile
    vehicle_use: VehicleUse
    car_assumptions: CarAssumptionsInput
    has_other_local_trips_today: bool
    previous_allowance_krw: Annotated[int, Field(ge=0, le=20_000)]

    @model_validator(mode="after")
    def trip_interval_is_aware_and_bounded(self) -> "TripPreviewRequest":
        for name, value in (
            ("startsAt", self.starts_at),
            ("returnsAt", self.returns_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        interval = self.returns_at - self.starts_at
        if not timedelta(0) < interval <= timedelta(hours=24):
            raise ValueError("returnsAt must be after startsAt and within 24 hours")
        return self


class CoordinateResponse(ApiModel):
    latitude: float
    longitude: float


class CoverageResponse(ApiModel):
    status: Literal["SEOUL", "BUFFER", "OUT_OF_COVERAGE"]


class OriginResponse(ApiModel):
    site_id: str
    name: str
    address: str
    coordinate: CoordinateResponse


class RouteCostResponse(ApiModel):
    fare_krw: int | None = None
    fuel_krw: int | None = None
    toll_krw: int | None = None
    parking_krw: int | None = None


class RouteResponse(ApiModel):
    id: str
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None
    cost_status: str
    cost_breakdown: RouteCostResponse | None
    geometry: tuple[CoordinateResponse, ...]
    source: str
    source_as_of: datetime
    warnings: tuple[str, ...]


class BestResponse(ApiModel):
    fastest_route_id: str | None
    shortest_route_id: str | None
    cheapest_route_id: str | None


class AmountResponse(ApiModel):
    status: str
    amount_krw: int | None
    warnings: tuple[str, ...] = ()


class AllowanceResponse(AmountResponse):
    """Allowance is intentionally a separate public amount from mobility cost."""


class ClassificationPathResponse(ApiModel):
    id: str
    distance_meters: int
    geometry: tuple[CoordinateResponse, ...]
    source: str
    queried_at: datetime


class TripPreviewResponse(ApiModel):
    coverage: CoverageResponse
    origin: OriginResponse
    institution_snapshot_id: str | None
    policy_scope: PolicyProfile
    classification: str
    classification_distance_meters: int | None
    classification_path: ClassificationPathResponse | None
    routes: tuple[RouteResponse, ...]
    best: BestResponse
    mobility_cost: AmountResponse
    allowance: AmountResponse
    rule_set_id: str | None
    effective_from: str | None
    source_refs: tuple[str, ...]
    warnings: tuple[str, ...]


class InstitutionSearchResponse(ApiModel):
    items: tuple[dict[str, object], ...]


class PlacesResponse(ApiModel):
    items: tuple[dict[str, object], ...]
    warnings: tuple[str, ...] = ()


class ReversePlaceResponse(ApiModel):
    item: dict[str, object] | None
    warnings: tuple[str, ...] = ()
