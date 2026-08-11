import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import SecretStr

from app.providers.http import BoundedHttpClient, ProviderRequestError
from app.routing.models import (
    CostStatus,
    ProviderResult,
    ProviderWarning,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.settings import Settings

_PATH_URL = "http://ws.bus.go.kr/api/rest/pathinfo/getPathInfoByBusNSub"


class _LimitExceeded(ValueError):
    pass


class _UpstreamRejected(ValueError):
    pass


class SeoulTransitProvider:
    name = "SEOUL_TRANSIT"
    supported_modes = frozenset({TravelMode.TRANSIT})

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        service_key: SecretStr | None = None,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 3_000_000,
        max_routes: int = 20,
        max_path_items: int = 1_000,
    ) -> None:
        if service_key is not None and type(service_key) is not SecretStr:
            raise TypeError("service_key must be an exact SecretStr or None")
        if now is not None and not callable(now):
            raise TypeError("now must be callable or None")
        if type(max_routes) is not int or not 1 <= max_routes <= 100:
            raise ValueError("max_routes must be in [1, 100]")
        if type(max_path_items) is not int or not 1 <= max_path_items <= 10_000:
            raise ValueError("max_path_items must be in [1, 10000]")
        self._service_key = service_key
        self._now = now or (lambda: datetime.now(UTC))
        self._max_routes = max_routes
        self._max_path_items = max_path_items
        self._last_schema_fingerprint: str | None = None
        self._transport = BoundedHttpClient(
            http=http,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._http = self._transport.http

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        if type(query) is not RouteQuery:
            raise TypeError("query must be an exact RouteQuery")
        if query.mode is not TravelMode.TRANSIT:
            return self._failure(
                "UNSUPPORTED_MODE",
                "Provider does not support the requested travel mode",
            )
        if self._service_key is None:
            return self._failure(
                "MISSING_CREDENTIAL", "Provider credential is unavailable"
            )
        try:
            self._last_schema_fingerprint = None
            raw = await self._transport.get_xml(
                url=_PATH_URL,
                params={
                    "startX": _decimal(query.origin.longitude),
                    "startY": _decimal(query.origin.latitude),
                    "endX": _decimal(query.destination.longitude),
                    "endY": _decimal(query.destination.latitude),
                    "resultType": "xml",
                },
                query_secret=("serviceKey", self._service_key),
            )
            source_time = self._source_time()
            routes, self._last_schema_fingerprint = _parse_routes(
                raw,
                query=query,
                source_time=source_time,
                max_routes=self._max_routes,
                max_path_items=self._max_path_items,
            )
        except _LimitExceeded:
            return self._failure(
                "RESPONSE_LIMIT_EXCEEDED",
                "Public transit response exceeded a route or path limit",
            )
        except _UpstreamRejected:
            return self._failure(
                "UPSTREAM_REJECTED", "Public transit service rejected the request"
            )
        except ProviderRequestError as exc:
            return ProviderResult(
                provider=self.name,
                routes=(),
                warnings=(exc.warning(self.name),),
            )
        except (
            DefusedXmlException,
            ParseError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            return self._failure(
                "SCHEMA_MISMATCH",
                "Public transit response did not match the documented XML schema",
            )
        warnings = (
            ProviderWarning(
                code="GEOMETRY_MISSING",
                message="Public route geometry is unavailable",
                source=self.name,
            ),
            ProviderWarning(
                code="FARE_MISSING",
                message="Public route fare is unavailable",
                source=self.name,
            ),
        )
        return ProviderResult(provider=self.name, routes=routes, warnings=warnings)

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def last_status_code(self) -> int | None:
        return self._transport.last_status_code

    @property
    def last_schema_fingerprint(self) -> str | None:
        return self._last_schema_fingerprint

    @classmethod
    def from_settings(cls, settings: Settings) -> "SeoulTransitProvider":
        if type(settings) is not Settings:
            raise TypeError("settings must be an exact Settings")
        return cls(
            service_key=settings.seoul_transit_service_key,
            timeout_seconds=settings.provider_timeout_seconds,
        )

    def _failure(self, code: str, message: str) -> ProviderResult:
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


def _parse_routes(
    raw: bytes,
    *,
    query: RouteQuery,
    source_time: datetime,
    max_routes: int,
    max_path_items: int,
) -> tuple[tuple[RouteOption, ...], str]:
    root = ElementTree.fromstring(
        raw,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    if root.tag != "ServiceResult" or sum(1 for _ in root.iter()) > 50_000:
        raise ValueError
    header = root.find("msgHeader")
    body = root.find("msgBody")
    if header is None or body is None:
        raise ValueError
    header_code = _element_text(header, "headerCd")
    if header_code != "0":
        raise _UpstreamRejected
    declared_count = _nonnegative_int(_element_text(header, "itemCount"))
    items = body.findall("itemList")
    if declared_count != len(items):
        raise ValueError
    if len(items) > max_routes:
        raise _LimitExceeded
    routes: list[RouteOption] = []
    for index, item in enumerate(items):
        distance = _nonnegative_int(_element_text(item, "distance"))
        minutes = _nonnegative_int(_element_text(item, "time"))
        if minutes > 1_000_000:
            raise ValueError
        paths = item.findall("pathList")
        if not paths or len(paths) > max_path_items:
            raise _LimitExceeded if len(paths) > max_path_items else ValueError
        lineage: list[str] = []
        for path in paths:
            route_id = _element_text(path, "routeId")
            if not route_id or len(route_id) > 100:
                raise ValueError
            _path_coordinate(path, "fx", "fy")
            _path_coordinate(path, "tx", "ty")
            lineage.append(route_id)
        route_digest = hashlib.sha256(
            f"{index}|{distance}|{minutes}|{'|'.join(lineage)}".encode()
        ).hexdigest()[:20]
        routes.append(
            RouteOption(
                id=f"SEOUL_TRANSIT:{route_digest}",
                mode=TravelMode.TRANSIT,
                duration_seconds=minutes * 60,
                distance_meters=distance,
                mobility_cost_krw=None,
                cost_status=CostStatus.UNKNOWN,
                cost_breakdown=None,
                geometry=(query.origin, query.destination),
                source="SEOUL_TRANSIT",
                source_as_of=source_time,
                warnings=("GEOMETRY_MISSING", "FARE_MISSING"),
            )
        )
    return tuple(routes), _xml_schema_fingerprint(root)


def _xml_schema_fingerprint(root: Element) -> str:
    encoded = json.dumps(
        _xml_schema_shape(root),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _xml_schema_shape(element: Element) -> dict[str, object]:
    child_shapes = {
        json.dumps(
            _xml_schema_shape(child),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        for child in element
    }
    return {
        "tag": element.tag,
        "attributeNames": sorted(element.attrib),
        "hasText": bool(element.text and element.text.strip()),
        "childShapes": sorted(child_shapes),
    }


def _element_text(parent: Element, name: str) -> str:
    child = parent.find(name)
    if child is None or child.text is None:
        raise ValueError
    value = child.text.strip()
    if not value or len(value) > 1_000:
        raise ValueError
    return value


def _nonnegative_int(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError
    result = int(value)
    if result < 0:
        raise ValueError
    return result


def _path_coordinate(
    path: Element,
    longitude_name: str,
    latitude_name: str,
) -> None:
    longitude = float(_element_text(path, longitude_name))
    latitude = float(_element_text(path, latitude_name))
    if (
        not isfinite(longitude)
        or not isfinite(latitude)
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
    ):
        raise ValueError


def _decimal(value: float) -> str:
    return format(value, ".15g")
