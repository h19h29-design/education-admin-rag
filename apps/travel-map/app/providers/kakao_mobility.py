import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from typing import cast

import httpx
from pydantic import SecretStr

from app.providers.http import BoundedHttpClient, ProviderRequestError
from app.providers.opinet import OpinetClient, estimate_car_cost
from app.routing.models import (
    Coordinate,
    CostStatus,
    FuelType,
    ProviderResult,
    ProviderWarning,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.settings import Settings

_DIRECTIONS_URL = "https://apis-navi.kakaomobility.com/v1/directions"
_PRIORITIES = frozenset({"RECOMMEND", "TIME", "DISTANCE"})
_FUEL_NAMES = {
    FuelType.GASOLINE: "GASOLINE",
    FuelType.DIESEL: "DIESEL",
    FuelType.LPG: "LPG",
}


class _LimitExceeded(ValueError):
    pass


class KakaoCarProvider:
    name = "KAKAO_CAR"
    supported_modes = frozenset({TravelMode.CAR})

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        rest_key: SecretStr | None = None,
        opinet: OpinetClient | None = None,
        now: Callable[[], datetime] | None = None,
        priority: str = "RECOMMEND",
        alternatives: bool = True,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 2_000_000,
        max_routes: int = 5,
        max_roads: int = 5_000,
        max_geometry_points: int = 50_000,
    ) -> None:
        if rest_key is not None and type(rest_key) is not SecretStr:
            raise TypeError("rest_key must be an exact SecretStr or None")
        if type(priority) is not str or priority not in _PRIORITIES:
            raise ValueError("unsupported Kakao car priority")
        if type(alternatives) is not bool:
            raise TypeError("alternatives must be an exact bool")
        if opinet is not None and type(opinet) is not OpinetClient:
            raise TypeError("opinet must be an exact OpinetClient or None")
        if now is not None and not callable(now):
            raise TypeError("now must be callable or None")
        if type(max_routes) is not int or not 1 <= max_routes <= 10:
            raise ValueError("max_routes must be in [1, 10]")
        if type(max_roads) is not int or not 1 <= max_roads <= 20_000:
            raise ValueError("max_roads must be in [1, 20000]")
        if (
            type(max_geometry_points) is not int
            or not 2 <= max_geometry_points <= 100_000
        ):
            raise ValueError("max_geometry_points must be in [2, 100000]")
        self.priority = priority
        self.alternatives = alternatives
        self._rest_key = rest_key
        self._now = now or (lambda: datetime.now(UTC))
        self._max_routes = max_routes
        self._max_roads = max_roads
        self._max_geometry_points = max_geometry_points
        self._transport = BoundedHttpClient(
            http=http,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._http = self._transport.http
        self._owns_opinet = opinet is None
        self._opinet = opinet or OpinetClient(http=self._http, cert_key=None, now=now)

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if type(query) is not RouteQuery:
            raise TypeError("query must be an exact RouteQuery")
        if query.mode is not TravelMode.CAR:
            return self._warning_result(
                "UNSUPPORTED_MODE",
                "Provider does not support the requested travel mode",
            )
        params = {
            "origin": _pair(query.origin),
            "destination": _pair(query.destination),
            "priority": self.priority,
            "alternatives": str(self.alternatives).lower(),
            "summary": "false",
        }
        if query.car_assumptions is not None:
            params["car_fuel"] = _FUEL_NAMES[query.car_assumptions.fuel_type]
        try:
            payload = await self._transport.get_json(
                url=_DIRECTIONS_URL,
                params=params,
                header_secret=self._rest_key,
            )
            normalized = _parse_payload(
                payload,
                max_routes=self._max_routes,
                max_roads=self._max_roads,
                max_geometry_points=self._max_geometry_points,
            )
            source_time = self._source_time()
        except _LimitExceeded:
            return self._warning_result(
                "RESPONSE_LIMIT_EXCEEDED",
                "Provider response exceeded a route, road, or geometry limit",
            )
        except ProviderRequestError as exc:
            return ProviderResult(
                provider=self.name,
                routes=(),
                warnings=(exc.warning(self.name),),
            )
        except (TypeError, ValueError, KeyError, IndexError):
            return self._warning_result(
                "SCHEMA_MISMATCH",
                "Kakao directions response did not match the documented schema",
            )

        fuel_price: float | None = None
        warnings: tuple[ProviderWarning, ...] = ()
        if query.car_assumptions is None:
            warnings = (
                ProviderWarning(
                    code="CAR_ASSUMPTIONS_MISSING",
                    message="Car cost assumptions were not provided",
                    source=self.name,
                ),
            )
        else:
            try:
                fuel_price = (
                    await self._opinet.average_price(query.car_assumptions.fuel_type)
                ).krw_per_liter
            except ProviderRequestError as exc:
                warnings = (exc.warning(self.name),)

        routes: list[RouteOption] = []
        for index, item in enumerate(normalized):
            duration, distance, toll, geometry = item
            breakdown = None
            if query.car_assumptions is not None and fuel_price is not None:
                breakdown = estimate_car_cost(
                    distance_meters=distance,
                    fuel_price_krw_per_liter=fuel_price,
                    assumptions=query.car_assumptions,
                    toll_krw=toll,
                )
            routes.append(
                RouteOption(
                    id=_route_id(index, duration, distance, toll, geometry),
                    mode=TravelMode.CAR,
                    duration_seconds=duration,
                    distance_meters=distance,
                    mobility_cost_krw=(
                        breakdown.total_krw if breakdown is not None else None
                    ),
                    cost_status=(
                        CostStatus.ESTIMATED
                        if breakdown is not None
                        else CostStatus.UNKNOWN
                    ),
                    cost_breakdown=breakdown,
                    geometry=geometry,
                    source=self.name,
                    source_as_of=source_time,
                )
            )
        return ProviderResult(
            provider=self.name,
            routes=tuple(routes),
            warnings=warnings,
        )

    async def aclose(self) -> None:
        if self._owns_opinet:
            await self._opinet.aclose()
        await self._transport.aclose()

    @property
    def last_status_code(self) -> int | None:
        return self._transport.last_status_code

    @property
    def last_schema_fingerprint(self) -> str | None:
        return self._transport.last_schema_fingerprint

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        priority: str = "RECOMMEND",
        alternatives: bool = True,
    ) -> "KakaoCarProvider":
        if type(settings) is not Settings:
            raise TypeError("settings must be an exact Settings")
        opinet = OpinetClient(
            cert_key=settings.opinet_cert_key,
            timeout_seconds=settings.provider_timeout_seconds,
        )
        provider = cls(
            rest_key=settings.kakao_rest_api_key,
            opinet=opinet,
            timeout_seconds=settings.provider_timeout_seconds,
            priority=priority,
            alternatives=alternatives,
        )
        provider._owns_opinet = True
        return provider

    def _warning_result(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            routes=(),
            warnings=(ProviderWarning(code=code, message=message, source=self.name),),
        )

    def _source_time(self) -> datetime:
        value = self._now()
        if type(value) is not datetime or value.tzinfo is None:
            raise ProviderRequestError(
                "CLOCK_INVALID", "Provider clock did not return an aware datetime"
            )
        return value

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is KakaoCarProvider
            and other.priority == self.priority
            and other.alternatives is self.alternatives
        )


def _parse_payload(
    payload: dict[str, object],
    *,
    max_routes: int,
    max_roads: int,
    max_geometry_points: int,
) -> tuple[tuple[int, int, int, tuple[Coordinate, ...]], ...]:
    raw_routes = payload.get("routes")
    if type(raw_routes) is not list:
        raise ValueError
    if len(raw_routes) > max_routes:
        raise _LimitExceeded
    result: list[tuple[int, int, int, tuple[Coordinate, ...]]] = []
    for route in raw_routes:
        if type(route) is not dict or _integer(route, "result_code") != 0:
            raise ValueError
        summary = _object(route, "summary")
        duration = _integer(summary, "duration")
        distance = _integer(summary, "distance")
        fare = _object(summary, "fare")
        toll = _integer(fare, "toll")
        sections = route.get("sections")
        if type(sections) is not list or not sections or len(sections) > 100:
            raise ValueError
        points: list[Coordinate] = []
        roads_seen = 0
        raw_points_seen = 0
        for section in sections:
            if type(section) is not dict:
                raise ValueError
            roads = section.get("roads")
            if type(roads) is not list:
                raise ValueError
            roads_seen += len(roads)
            if roads_seen > max_roads:
                raise _LimitExceeded
            for road in roads:
                if type(road) is not dict:
                    raise ValueError
                vertexes = road.get("vertexes")
                if type(vertexes) is not list or len(vertexes) % 2 or not vertexes:
                    raise ValueError
                raw_points_seen += len(vertexes) // 2
                if raw_points_seen > max_geometry_points:
                    raise _LimitExceeded
                for offset in range(0, len(vertexes), 2):
                    coordinate = _coordinate(
                        longitude_value=vertexes[offset],
                        latitude_value=vertexes[offset + 1],
                    )
                    if not points or points[-1] != coordinate:
                        points.append(coordinate)
                    if len(points) > max_geometry_points:
                        raise _LimitExceeded
        if len(points) < 2:
            raise ValueError
        result.append((duration, distance, toll, tuple(points)))
    return tuple(result)


def _object(value: dict[object, object], name: str) -> dict[object, object]:
    selected = value.get(name)
    if type(selected) is not dict:
        raise ValueError
    return selected


def _integer(value: dict[object, object], name: str) -> int:
    selected = value.get(name)
    if type(selected) is not int or selected < 0:
        raise ValueError
    return selected


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError
    selected = float(cast("int | float", value))
    if not isfinite(selected):
        raise ValueError
    return selected


def _coordinate(
    *,
    longitude_value: object,
    latitude_value: object,
) -> Coordinate:
    longitude = _number(longitude_value)
    latitude = _number(latitude_value)
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        raise ValueError
    return Coordinate(latitude=latitude, longitude=longitude)


def _pair(coordinate: Coordinate) -> str:
    return (
        f"{format(coordinate.longitude, '.15g')},{format(coordinate.latitude, '.15g')}"
    )


def _route_id(index: int, *parts: object) -> str:
    encoded = json.dumps((index, *parts), separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()[:20]
    return f"KAKAO_CAR:{digest}"
