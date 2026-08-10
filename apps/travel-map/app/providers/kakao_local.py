import hashlib
import json
from dataclasses import dataclass

import httpx

from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    get_json_with_retry,
    utc_now,
)

_ENDPOINT = "https://dapi.kakao.com/v2/local/search/address.json"
_MAX_REQUEST_COUNT = 5_000
_MAX_CUMULATIVE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class GeocodeResult:
    road_address: str
    latitude: float
    longitude: float
    confidence: str


class KakaoLocalClient:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient) -> None:
        if not api_key.strip():
            raise SourceDataError(
                "KAKAO_REST_API_KEY is required to geocode missing coordinates"
            )
        self._api_key = api_key
        self._client = client
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
        self._api_key = ""
        raise SourceDataError(failure or "Kakao Local validation failed")

    async def _geocode_impl(self, address: str) -> GeocodeResult | None:
        if not address.strip():
            raise SourceDataError("geocoding address must be nonblank")
        if self._request_count >= _MAX_REQUEST_COUNT:
            raise SourceDataError("Kakao Local request limit exceeded")
        payload, raw = await get_json_with_retry(
            client=self._client,
            url=_ENDPOINT,
            params={"query": address},
            headers={"Authorization": f"KakaoAK {self._api_key}"},
            source_label="Kakao Local",
        )
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
        self._api_key = ""

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
