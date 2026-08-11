import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Self, cast

import httpx
from pydantic import SecretStr

from app.providers.http import BoundedHttpClient, ProviderRequestError
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
from app.settings import Settings

_TRANSIT_URL = "https://dapi.kakao.com/v2/routing/publictraffic"
_WALK_URL = "https://dapi.kakao.com/v2/routing/walking"
_WALK_MODES = ("BROAD_FIRST", "SHORTEST", "ACCESSIBLE")


class _LimitExceeded(ValueError):
    pass


class _KakaoMapProvider:
    supported_modes: frozenset[TravelMode]
    name: str

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        rest_key: SecretStr | None = None,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 1_000_000,
        max_routes: int = 10,
        max_geometry_points: int = 20_000,
    ) -> None:
        if rest_key is not None and type(rest_key) is not SecretStr:
            raise TypeError("rest_key must be an exact SecretStr or None")
        if now is not None and not callable(now):
            raise TypeError("now must be callable or None")
        if type(max_routes) is not int or not 1 <= max_routes <= 50:
            raise ValueError("max_routes must be in [1, 50]")
        if (
            type(max_geometry_points) is not int
            or not 2 <= max_geometry_points <= 100_000
        ):
            raise ValueError("max_geometry_points must be in [2, 100000]")
        self._rest_key = rest_key
        self._now = now or (lambda: datetime.now(UTC))
        self._max_routes = max_routes
        self._max_geometry_points = max_geometry_points
        self._transport = BoundedHttpClient(
            http=http,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._http = self._transport.http

    async def aclose(self) -> None:
        await self._transport.aclose()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if type(settings) is not Settings:
            raise TypeError("settings must be an exact Settings")
        return cls(
            rest_key=settings.kakao_rest_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
        )

    def _unsupported(self) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            routes=(),
            warnings=(
                ProviderWarning(
                    code="UNSUPPORTED_MODE",
                    message="Provider does not support the requested travel mode",
                    source=self.name,
                ),
            ),
        )

    def _failure(self, exc: ProviderRequestError) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            routes=(),
            warnings=(exc.warning(self.name),),
        )

    def _source_time(self) -> datetime:
        value = self._now()
        if type(value) is not datetime or value.tzinfo is None:
            raise ProviderRequestError(
                "CLOCK_INVALID", "Provider clock did not return an aware datetime"
            )
        return value

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self)


class KakaoTransitProvider(_KakaoMapProvider):
    name = "KAKAO_TRANSIT"
    supported_modes = frozenset({TravelMode.TRANSIT})

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if type(query) is not RouteQuery:
            raise TypeError("query must be an exact RouteQuery")
        if query.mode is not TravelMode.TRANSIT:
            return self._unsupported()
        try:
            payload = await self._transport.get_json(
                url=_TRANSIT_URL,
                params=_coordinate_params(query),
                header_secret=self._rest_key,
            )
            source_time = self._source_time()
            routes = _parse_transit(
                payload,
                source_time=source_time,
                max_routes=self._max_routes,
                max_geometry_points=self._max_geometry_points,
            )
        except _LimitExceeded:
            return self._failure(
                ProviderRequestError(
                    "RESPONSE_LIMIT_EXCEEDED",
                    "Provider response exceeded a route or geometry limit",
                )
            )
        except ProviderRequestError as exc:
            return self._failure(exc)
        except (TypeError, ValueError, KeyError, IndexError):
            return self._failure(
                ProviderRequestError(
                    "SCHEMA_MISMATCH",
                    "Kakao transit response did not match the documented schema",
                )
            )
        return ProviderResult(provider=self.name, routes=routes)


class KakaoWalkProvider(_KakaoMapProvider):
    name = "KAKAO_WALK"
    supported_modes = frozenset({TravelMode.WALK})

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if type(query) is not RouteQuery:
            raise TypeError("query must be an exact RouteQuery")
        if query.mode is not TravelMode.WALK:
            return self._unsupported()
        routes: list[RouteOption] = []
        try:
            source_time = self._source_time()
            for route_mode in _WALK_MODES:
                params = _coordinate_params(query)
                params["route_mode"] = route_mode
                payload = await self._transport.get_json(
                    url=_WALK_URL,
                    params=params,
                    header_secret=self._rest_key,
                )
                routes.append(
                    _parse_walk(
                        payload,
                        route_mode=route_mode,
                        source_time=source_time,
                        max_geometry_points=self._max_geometry_points,
                    )
                )
            if len(routes) > self._max_routes:
                raise _LimitExceeded
        except _LimitExceeded:
            return self._failure(
                ProviderRequestError(
                    "RESPONSE_LIMIT_EXCEEDED",
                    "Provider response exceeded a route or geometry limit",
                )
            )
        except ProviderRequestError as exc:
            return self._failure(exc)
        except (TypeError, ValueError, KeyError, IndexError):
            return self._failure(
                ProviderRequestError(
                    "SCHEMA_MISMATCH",
                    "Kakao walk response did not match the documented schema",
                )
            )
        return ProviderResult(provider=self.name, routes=tuple(routes))


def _coordinate_params(query: RouteQuery) -> dict[str, str]:
    return {
        "start_x": _decimal(query.origin.longitude),
        "start_y": _decimal(query.origin.latitude),
        "end_x": _decimal(query.destination.longitude),
        "end_y": _decimal(query.destination.latitude),
        "input_coord": "WGS84",
        "output_coord": "WGS84",
    }


def _parse_transit(
    payload: dict[str, object],
    *,
    source_time: datetime,
    max_routes: int,
    max_geometry_points: int,
) -> tuple[RouteOption, ...]:
    if payload.get("status") != "OK":
        raise ValueError
    raw_routes = payload.get("routes")
    if type(raw_routes) is not list:
        raise ValueError
    if len(raw_routes) > max_routes:
        raise _LimitExceeded
    routes: list[RouteOption] = []
    for index, raw_route in enumerate(raw_routes):
        if type(raw_route) is not dict:
            raise ValueError
        properties = _object(raw_route, "properties")
        fare = _object(properties, "fare")
        duration = _integer(properties, "totalTime")
        distance = _integer(properties, "totalDistance")
        fare_value = _integer(fare, "value")
        steps = raw_route.get("steps")
        geometry = _steps_geometry(steps, max_geometry_points)
        routes.append(
            RouteOption(
                id=_route_id(
                    "KAKAO_TRANSIT", index, duration, distance, fare_value, geometry
                ),
                mode=TravelMode.TRANSIT,
                duration_seconds=duration,
                distance_meters=distance,
                mobility_cost_krw=fare_value,
                cost_status=CostStatus.KNOWN,
                cost_breakdown=RouteCostBreakdown(fare_krw=fare_value),
                geometry=geometry,
                source="KAKAO_TRANSIT",
                source_as_of=source_time,
            )
        )
    return tuple(routes)


def _parse_walk(
    payload: dict[str, object],
    *,
    route_mode: str,
    source_time: datetime,
    max_geometry_points: int,
) -> RouteOption:
    if payload.get("status") != "OK":
        raise ValueError
    route = _object(payload, "route")
    properties = _object(route, "properties")
    duration = _integer(properties, "totalTime")
    distance = _integer(properties, "totalDistance")
    legs = route.get("legs")
    if type(legs) is not list or not legs or len(legs) > 20:
        raise ValueError
    steps: list[object] = []
    for leg in legs:
        if type(leg) is not dict:
            raise ValueError
        leg_steps = leg.get("steps")
        if type(leg_steps) is not list or len(leg_steps) > 1_000:
            raise ValueError
        steps.extend(leg_steps)
    geometry = _steps_geometry(steps, max_geometry_points)
    return RouteOption(
        id=_route_id("KAKAO_WALK", route_mode, duration, distance, 0, geometry),
        mode=TravelMode.WALK,
        duration_seconds=duration,
        distance_meters=distance,
        mobility_cost_krw=0,
        cost_status=CostStatus.KNOWN,
        cost_breakdown=RouteCostBreakdown(),
        geometry=geometry,
        source="KAKAO_WALK",
        source_as_of=source_time,
    )


def _steps_geometry(value: object, max_points: int) -> tuple[Coordinate, ...]:
    if type(value) is not list or len(value) > 2_000:
        raise ValueError
    points: list[Coordinate] = []
    raw_point_count = 0
    for step in value:
        if type(step) is not dict:
            raise ValueError
        path = _object(step, "path")
        raw_points = path.get("points")
        if type(raw_points) is not list:
            raise ValueError
        raw_point_count += len(raw_points)
        if len(raw_points) > max_points or raw_point_count > max_points:
            raise _LimitExceeded
        for point in raw_points:
            if type(point) is not list or len(point) != 2:
                raise ValueError
            longitude = _float_number(point[0])
            latitude = _float_number(point[1])
            coordinate = Coordinate(latitude=latitude, longitude=longitude)
            if not points or points[-1] != coordinate:
                points.append(coordinate)
    if len(points) < 2:
        raise ValueError
    return tuple(points)


def _object(value: Mapping[str, object], name: str) -> dict[str, object]:
    result = value.get(name)
    if type(result) is not dict:
        raise ValueError
    return result


def _integer(value: Mapping[str, object], name: str) -> int:
    result = value.get(name)
    if type(result) is not int or result < 0:
        raise ValueError
    return result


def _float_number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError
    result = float(cast("int | float", value))
    if not isfinite(result):
        raise ValueError
    return result


def _route_id(source: str, *parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()[:20]
    return f"{source}:{digest}"


def _decimal(value: float) -> str:
    return format(value, ".15g")
