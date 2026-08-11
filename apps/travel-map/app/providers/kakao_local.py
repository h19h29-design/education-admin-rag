import hashlib
import json
from dataclasses import dataclass
from math import isfinite

import httpx
from pydantic import SecretStr

from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    get_json_with_retry,
    utc_now,
)
from app.providers.http import BoundedHttpClient, ProviderRequestError
from app.routing.models import Coordinate

_ENDPOINT = "https://dapi.kakao.com/v2/local/search/address.json"
_KEYWORD_ENDPOINT = "https://dapi.kakao.com/v2/local/search/keyword.json"
_REVERSE_ENDPOINT = "https://dapi.kakao.com/v2/local/geo/coord2address.json"
_MAX_REQUEST_COUNT = 5_000
_MAX_CUMULATIVE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class GeocodeResult:
    road_address: str
    latitude: float
    longitude: float
    confidence: str


@dataclass(frozen=True)
class BoundingBox:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        for name in ("west", "south", "east", "north"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"{name} must be an exact float")
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not -180.0 <= self.west < self.east <= 180.0:
            raise ValueError("west/east bounds are invalid")
        if not -90.0 <= self.south < self.north <= 90.0:
            raise ValueError("south/north bounds are invalid")

    def parameter(self) -> str:
        return ",".join(_decimal(value) for value in self.__dict__.values())


@dataclass(frozen=True)
class PlaceCandidate:
    place_id: str
    name: str
    road_address: str
    lot_address: str
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        for name in ("place_id", "name"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or value != value.strip():
                raise TypeError(f"{name} must be a canonical nonblank string")
        for name in ("road_address", "lot_address"):
            value = getattr(self, name)
            if type(value) is not str or value != value.strip():
                raise TypeError(f"{name} must be a canonical string")
        for name, low, high in (
            ("latitude", -90.0, 90.0),
            ("longitude", -180.0, 180.0),
        ):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"{name} must be an exact float")
            if not isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} is outside geographic bounds")


class KakaoLocalClient:
    def __init__(
        self,
        *,
        rest_key: SecretStr | None = None,
        http: httpx.AsyncClient | None = None,
        max_response_bytes: int = 1_000_000,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        legacy = api_key is not None or client is not None
        if legacy:
            if rest_key is not None or http is not None:
                raise TypeError("legacy and provider constructor arguments cannot mix")
            if type(api_key) is not str or not api_key.strip():
                raise SourceDataError(
                    "KAKAO_REST_API_KEY is required to geocode missing coordinates"
                )
            if type(client) is not httpx.AsyncClient:
                raise TypeError("client must be an exact AsyncClient")
            rest_key = SecretStr(api_key)
            http = client
        elif rest_key is not None and type(rest_key) is not SecretStr:
            raise TypeError("rest_key must be an exact SecretStr or None")
        self._rest_key = rest_key
        self._transport = BoundedHttpClient(
            http=http,
            timeout_seconds=5.0,
            max_response_bytes=max_response_bytes,
        )
        self._http = self._transport.http
        self._client = self._http
        self._legacy = legacy
        self.last_warnings: tuple[str, ...] = ()
        self._raw_sha256 = hashlib.sha256()
        self._request_count = 0
        self._cumulative_bytes = 0
        self._accepted: list[GeocodeResult] = []

    async def geocode(self, address: str) -> GeocodeResult | None:
        failure: str | None = None
        try:
            return await self._geocode_impl(address)
        except SourceDataError as exc:
            failure = str(exc)
        self.clear_credentials()
        raise SourceDataError(failure or "Kakao Local validation failed")

    async def _geocode_impl(self, address: str) -> GeocodeResult | None:
        if type(address) is not str:
            raise TypeError("geocoding address must be an exact string")
        if not address.strip():
            raise SourceDataError("geocoding address must be nonblank")
        if self._request_count >= _MAX_REQUEST_COUNT:
            raise SourceDataError("Kakao Local request limit exceeded")
        secret = ""
        headers: dict[str, str] = {}
        try:
            if self._rest_key is None:
                raise SourceDataError(
                    "KAKAO_REST_API_KEY is required to geocode missing coordinates"
                )
            secret = self._rest_key.get_secret_value()
            if not secret.strip():
                raise SourceDataError(
                    "KAKAO_REST_API_KEY is required to geocode missing coordinates"
                )
            headers["Authorization"] = f"KakaoAK {secret}"
            payload, raw = await get_json_with_retry(
                client=self._client,
                url=_ENDPOINT,
                params={"query": address},
                headers=headers,
                source_label="Kakao Local",
            )
        finally:
            secret = ""
            headers.clear()
        if self._cumulative_bytes + len(raw) > _MAX_CUMULATIVE_BYTES:
            raise SourceDataError(
                "Kakao Local cumulative response size exceeds the trusted limit"
            )
        self._cumulative_bytes += len(raw)
        self._request_count += 1
        self._raw_sha256.update(raw)
        documents = payload.get("documents")
        if type(documents) is not list:
            raise SourceDataError("Kakao Local documents are missing")
        exact: list[dict[object, object]] = []
        normalized = _normalize_address(address)
        for document in documents:
            if type(document) is not dict:
                raise SourceDataError("Kakao Local document is invalid")
            road = document.get("road_address")
            if type(road) is not dict:
                continue
            road_name = road.get("address_name")
            if type(road_name) is str and _normalize_address(road_name) == normalized:
                exact.append(document)
        if len(exact) != 1:
            return None
        selected = exact[0]
        try:
            result = GeocodeResult(
                road_address=address.strip(),
                latitude=float(_required_string(selected, "y")),
                longitude=float(_required_string(selected, "x")),
                confidence="EXACT_ROAD_ADDRESS",
            )
            self._accepted.append(result)
            return result
        except ValueError as exc:
            raise SourceDataError("Kakao Local coordinates are invalid") from exc

    def clear_credentials(self) -> None:
        self._rest_key = None

    async def search(
        self,
        query: str,
        *,
        bounds: BoundingBox,
    ) -> tuple[PlaceCandidate, ...]:
        if type(query) is not str or type(bounds) is not BoundingBox:
            raise TypeError("query and bounds must use exact provider input types")
        normalized = query.strip()
        if not 2 <= len(normalized) <= 80:
            self.last_warnings = ("INVALID_QUERY",)
            return ()
        try:
            payload = await self._transport.get_json(
                url=_KEYWORD_ENDPOINT,
                params={
                    "query": normalized,
                    "rect": bounds.parameter(),
                    "page": "1",
                    "size": "15",
                },
                header_secret=self._rest_key,
            )
            places, parse_warnings = _parse_places(payload, bounds)
        except ProviderRequestError as exc:
            self.last_warnings = (exc.code,)
            return ()
        self.last_warnings = parse_warnings
        return places

    async def reverse_geocode(self, coordinate: Coordinate) -> PlaceCandidate | None:
        _require_coordinate(coordinate)
        try:
            payload = await self._transport.get_json(
                url=_REVERSE_ENDPOINT,
                params={
                    "x": _decimal(coordinate.longitude),
                    "y": _decimal(coordinate.latitude),
                    "input_coord": "WGS84",
                },
                header_secret=self._rest_key,
            )
            place = _parse_reverse(payload, coordinate)
        except ProviderRequestError as exc:
            self.last_warnings = (exc.code,)
            return None
        self.last_warnings = ()
        return place

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def last_status_code(self) -> int | None:
        return self._transport.last_status_code

    def provenance(self) -> EnrichmentProvenance:
        fetched_at = utc_now()
        normalized = json.dumps(
            [
                result.__dict__
                for result in sorted(
                    self._accepted,
                    key=lambda item: (
                        item.road_address,
                        item.latitude,
                        item.longitude,
                    ),
                )
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EnrichmentProvenance(
            source="KAKAO_LOCAL_GEOCODING",
            endpoint=_ENDPOINT,
            license_name="KAKAO_LOCAL_API_TERMS",
            attribution="Kakao Local API",
            fetched_at=fetched_at,
            source_as_of=fetched_at[:10],
            raw_sha256=self._raw_sha256.hexdigest(),
            normalized_sha256=hashlib.sha256(normalized).hexdigest(),
            request_region_code="SEOUL_ADDRESS_BATCH",
            request_timing=None,
            page_count=self._request_count,
            fetched_row_count=self._request_count,
            matched_row_count=len(self._accepted),
        )


def _normalize_address(value: str) -> str:
    return " ".join(value.split())


def _required_string(value: dict[object, object], name: str) -> str:
    selected = value.get(name)
    if type(selected) is not str or not selected.strip():
        raise SourceDataError(f"Kakao Local field {name} must be nonblank")
    return selected.strip()


def _decimal(value: float) -> str:
    return format(value, ".15g")


def _require_coordinate(value: Coordinate) -> None:
    if type(value) is not Coordinate:
        raise TypeError("coordinate must be an exact Coordinate")
    for item, low, high in (
        (value.latitude, -90.0, 90.0),
        (value.longitude, -180.0, 180.0),
    ):
        if type(item) is not float or not isfinite(item) or not low <= item <= high:
            raise ValueError("coordinate is invalid")


def _parse_places(
    payload: dict[str, object],
    bounds: BoundingBox,
) -> tuple[tuple[PlaceCandidate, ...], tuple[str, ...]]:
    documents = payload.get("documents")
    if type(documents) is not list:
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local place schema mismatch"
        )
    if len(documents) > 15:
        raise ProviderRequestError(
            "RESPONSE_LIMIT_EXCEEDED", "Kakao Local place result limit exceeded"
        )
    places: list[PlaceCandidate] = []
    seen_ids: set[str] = set()
    warnings: list[str] = []
    try:
        for document in documents:
            if type(document) is not dict:
                raise ValueError
            place = PlaceCandidate(
                place_id=_text(document, "id", required=True),
                name=_text(document, "place_name", required=True),
                road_address=_text(document, "road_address_name"),
                lot_address=_text(document, "address_name"),
                latitude=_number(document, "y"),
                longitude=_number(document, "x"),
            )
            if not (
                bounds.west <= place.longitude <= bounds.east
                and bounds.south <= place.latitude <= bounds.north
            ):
                if "OUT_OF_BOUNDS_RESULT" not in warnings:
                    warnings.append("OUT_OF_BOUNDS_RESULT")
                continue
            if place.place_id in seen_ids:
                if "DUPLICATE_PLACE_ID" not in warnings:
                    warnings.append("DUPLICATE_PLACE_ID")
                continue
            seen_ids.add(place.place_id)
            places.append(place)
    except (TypeError, ValueError):
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local place schema mismatch"
        ) from None
    return tuple(places), tuple(warnings)


def _parse_reverse(
    payload: dict[str, object], coordinate: Coordinate
) -> PlaceCandidate | None:
    documents = payload.get("documents")
    if type(documents) is not list:
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local address schema mismatch"
        )
    if len(documents) > 10:
        raise ProviderRequestError(
            "RESPONSE_LIMIT_EXCEEDED", "Kakao Local address result limit exceeded"
        )
    if not documents:
        return None
    document = documents[0]
    if type(document) is not dict:
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local address schema mismatch"
        )
    road = document.get("road_address")
    lot = document.get("address")
    if road is not None and type(road) is not dict:
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local address schema mismatch"
        )
    if lot is not None and type(lot) is not dict:
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local address schema mismatch"
        )
    if type(road) is not dict and type(lot) is not dict:
        return None
    try:
        road_address = _text(road, "address_name") if type(road) is dict else ""
        lot_address = _text(lot, "address_name") if type(lot) is dict else ""
        building = _text(road, "building_name") if type(road) is dict else ""
        name = building or road_address or lot_address
        return PlaceCandidate(
            place_id=f"reverse:{_decimal(coordinate.latitude)}:{_decimal(coordinate.longitude)}",
            name=name,
            road_address=road_address,
            lot_address=lot_address,
            latitude=coordinate.latitude,
            longitude=coordinate.longitude,
        )
    except (TypeError, ValueError):
        raise ProviderRequestError(
            "SCHEMA_MISMATCH", "Kakao Local address schema mismatch"
        ) from None


def _text(value: dict[object, object], name: str, *, required: bool = False) -> str:
    item = value.get(name, "")
    if type(item) is not str or item != item.strip() or (required and not item):
        raise ValueError
    return item


def _number(value: dict[object, object], name: str) -> float:
    item = value.get(name)
    if type(item) is not str or not item.strip():
        raise ValueError
    result = float(item)
    if not isfinite(result):
        raise ValueError
    return result
