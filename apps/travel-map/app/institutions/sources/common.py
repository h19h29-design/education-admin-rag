import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class SourceDataError(ValueError):
    """Raised when an official source response cannot be trusted."""


@dataclass(frozen=True)
class SourceInstitutionRecord:
    institution_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    latitude: float | None
    longitude: float | None
    source: str
    source_region_code: str
    source_as_of: str
    coordinate_quality: str


@dataclass(frozen=True)
class SourceProvenance:
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str
    raw_sha256: str
    page_count: int
    row_count: int
    fetched_row_count: int | None = None
    request_region_code: str | None = None
    request_timing: str | None = None
    normalized_sha256: str | None = None


@dataclass(frozen=True)
class EnrichmentProvenance:
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str
    raw_sha256: str
    normalized_sha256: str
    request_region_code: str
    request_timing: str | None
    page_count: int
    fetched_row_count: int
    matched_row_count: int


@dataclass(frozen=True)
class SourceFetchResult:
    records: tuple[SourceInstitutionRecord, ...]
    provenance: SourceProvenance


async def get_json_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str | int],
    headers: dict[str, str] | None,
    source_label: str,
) -> tuple[dict[str, Any], bytes]:
    timeout = httpx.Timeout(5.0, connect=2.0)
    for attempt in range(2):
        try:
            response = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            if response.status_code >= 500 and attempt == 0:
                continue
            response.raise_for_status()
            value = response.json()
            if type(value) is not dict:
                raise SourceDataError(f"{source_label} response must be a JSON object")
            return value, response.content
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt == 0 and (
                isinstance(exc, httpx.RequestError)
                or exc.response.status_code >= 500
            ):
                continue
            raise SourceDataError(f"{source_label} request failed") from None
        except ValueError:
            raise SourceDataError(
                f"{source_label} response is not valid JSON"
            ) from None
    raise SourceDataError(f"{source_label} request failed")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalized_records_sha256(
    records: tuple[SourceInstitutionRecord, ...] | list[SourceInstitutionRecord],
) -> str:
    normalized = json.dumps(
        [record.__dict__ for record in sorted(records, key=lambda row: row.institution_id)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()
