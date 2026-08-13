import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx


class SourceDataError(ValueError):
    """Raised when an official source response cannot be trusted."""


ObservationDateCounts = tuple[tuple[str, int], ...]


def validate_observation_date_counts(
    counts: ObservationDateCounts,
    *,
    expected_total: int,
    label: str,
) -> None:
    if type(counts) is not tuple:
        raise SourceDataError(f"{label} must be a tuple")
    dates: list[str] = []
    total = 0
    for entry in counts:
        if type(entry) is not tuple or len(entry) != 2:
            raise SourceDataError(f"{label} entries must be date/count pairs")
        source_date, count = entry
        if type(source_date) is not str or not source_date.strip():
            raise SourceDataError(f"{label} dates must be nonblank ISO dates")
        try:
            if date.fromisoformat(source_date).isoformat() != source_date:
                raise ValueError
        except ValueError:
            raise SourceDataError(f"{label} dates must be valid ISO dates") from None
        if type(count) is not int or count <= 0:
            raise SourceDataError(f"{label} counts must be positive integers")
        dates.append(source_date)
        total += count
    if len(dates) != len(set(dates)):
        raise SourceDataError(f"{label} dates must not be duplicated")
    if dates != sorted(dates):
        raise SourceDataError(f"{label} dates must be lexicographically sorted")
    if total != expected_total:
        raise SourceDataError(f"{label} total does not match expected row count")


def observation_date_counts(dates: Iterable[str]) -> ObservationDateCounts:
    if isinstance(dates, Mapping):
        raise SourceDataError("source observation dates must be an iterable of dates")
    values = Counter(dates)
    result = tuple(sorted(values.items()))
    validate_observation_date_counts(
        result,
        expected_total=sum(values.values()),
        label="source observation dates",
    )
    return result


def observation_counts_as_dict(
    counts: ObservationDateCounts,
) -> dict[str, int]:
    validate_observation_date_counts(
        counts,
        expected_total=sum(count for _, count in counts),
        label="observation dates",
    )
    return dict(counts)


def source_as_of_for(counts: ObservationDateCounts) -> str | None:
    return counts[0][0] if len(counts) == 1 else None


@dataclass(frozen=True)
class SourceInstitutionSiteRecord:
    site_code: str
    site_name: str
    road_address: str
    district: str
    latitude: float | None
    longitude: float | None
    coordinate_quality: str


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
    site_name: str = "main"
    additional_sites: tuple[SourceInstitutionSiteRecord, ...] = ()
    source_kind_label: str | None = None


@dataclass(frozen=True)
class SourceProvenance:
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str | None
    source_observation_date_counts: ObservationDateCounts
    normalized_observation_date_counts: ObservationDateCounts
    raw_sha256: str
    page_count: int
    row_count: int
    fetched_row_count: int | None = None
    request_region_code: str | None = None
    request_timing: str | None = None
    normalized_sha256: str | None = None
    unclassified_school_kind_counts: tuple[tuple[str, int], ...] = ()
    unclassified_school_policy_sha256: str | None = None


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
    matched_normalized_sha256: str | None = None


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
    max_response_bytes: int = 5 * 1024 * 1024,
) -> tuple[dict[str, Any], bytes]:
    failure: str | None = None
    try:
        return await _get_json_with_retry_impl(
            client=client,
            url=url,
            params=params,
            headers=headers,
            source_label=source_label,
            max_response_bytes=max_response_bytes,
        )
    except SourceDataError as exc:
        failure = str(exc)
    except Exception:  # noqa: BLE001
        # This is the public secret-scrubbing boundary for transport callbacks.
        failure = f"{source_label} request failed"
    finally:
        params.clear()
        if headers is not None:
            headers.clear()
    raise SourceDataError(failure or f"{source_label} request failed")


async def get_bytes_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    source_label: str,
    max_response_bytes: int,
) -> bytes:
    """Download a public attachment without buffering beyond its trusted limit."""
    timeout = httpx.Timeout(5.0, connect=2.0)
    for attempt in range(2):
        try:
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 500 and attempt == 0:
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        raise SourceDataError(
                            f"{source_label} response size exceeds the trusted limit"
                        )
                    body.extend(chunk)
            return bytes(body)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt == 0 and (
                isinstance(exc, httpx.RequestError)
                or exc.response.status_code >= 500
            ):
                continue
            raise SourceDataError(f"{source_label} request failed") from None
    raise SourceDataError(f"{source_label} request failed")


async def _get_json_with_retry_impl(
    *,
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str | int],
    headers: dict[str, str] | None,
    source_label: str,
    max_response_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    timeout = httpx.Timeout(5.0, connect=2.0)
    for attempt in range(2):
        try:
            async with client.stream(
                "GET",
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status_code >= 500 and attempt == 0:
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        raise SourceDataError(
                            f"{source_label} response size exceeds the trusted limit"
                        )
                    body.extend(chunk)
            raw = bytes(body)
            value = json.loads(raw)
            if type(value) is not dict:
                raise SourceDataError(f"{source_label} response must be a JSON object")
            return value, raw
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt == 0 and (
                isinstance(exc, httpx.RequestError)
                or exc.response.status_code >= 500
            ):
                continue
            raise SourceDataError(f"{source_label} request failed") from None
        except SourceDataError:
            raise
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
        [
            {
                "institution_id": record.institution_id,
                "official_name": record.official_name,
                "institution_type": record.institution_type,
                "foundation_type": record.foundation_type,
                "education_office": record.education_office,
                "source": record.source,
                "source_region_code": record.source_region_code,
                "source_as_of": record.source_as_of,
                "sites": [
                    {
                        "site_code": site.site_code,
                        "site_name": site.site_name,
                        "road_address": site.road_address,
                        "district": site.district,
                        "latitude": site.latitude,
                        "longitude": site.longitude,
                        "coordinate_quality": site.coordinate_quality,
                    }
                    for site in sorted(
                        (
                            SourceInstitutionSiteRecord(
                                site_code="main",
                                site_name=record.site_name,
                                road_address=record.road_address,
                                district=record.district,
                                latitude=record.latitude,
                                longitude=record.longitude,
                                coordinate_quality=record.coordinate_quality,
                            ),
                            *record.additional_sites,
                        ),
                        key=lambda item: item.site_code,
                    )
                ],
            }
            for record in sorted(records, key=lambda row: row.institution_id)
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()
