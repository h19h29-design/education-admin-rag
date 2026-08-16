import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from datetime import date

import httpx

from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceInstitutionRecord,
    get_bytes_with_retry,
    utc_now,
)

DOWNLOAD_URL = (
    "https://www.data.go.kr/cmm/cmm/fileDownload.do?"
    "atchFileId=FILE_000000003635904&fileDetailSn=1&"
    "dataNm=%ED%95%9C%EA%B5%AD%EA%B5%90%EC%9C%A1%EC%8B%9C%EC%84%A4"
    "%EC%95%88%EC%A0%84%EC%9B%90_%EC%B4%88%EC%A4%91%EB%93%B1%ED%95%99"
    "%EA%B5%90%EC%9C%84%EC%B9%98"
)
PINNED_SHA256 = "05fc53d5920aea0161cbb5f31aedb9c466450c7939fd60083b797095afe9eab1"
PINNED_SOURCE_AS_OF = "2026-03-20"
PINNED_SEOUL_COUNT = 1_313
PINNED_NATIONWIDE_COUNT = 12_011
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

_FIELDS = [
    "\ud559\uad50ID",
    "\ud559\uad50\uba85",
    "\ud559\uad50\uae09\uad6c\ubd84",
    "\uc124\ub9bd\uc77c\uc790",
    "\uc124\ub9bd\ud615\ud0dc",
    "\ubcf8\uad50\ubd84\uad50\uad6c\ubd84",
    "\uc6b4\uc601\uc0c1\ud0dc",
    "\uc18c\uc7ac\uc9c0\uc9c0\ubc88\uc8fc\uc18c",
    "\uc18c\uc7ac\uc9c0\ub3c4\ub85c\uba85\uc8fc\uc18c",
    "\uc2dc\ub3c4\uad50\uc721\uccad\ucf54\ub4dc",
    "\uc2dc\ub3c4\uad50\uc721\uccad\uba85",
    "\uad50\uc721\uc9c0\uc6d0\uccad\ucf54\ub4dc",
    "\uad50\uc721\uc9c0\uc6d0\uccad\uba85",
    "\uc0dd\uc131\uc77c\uc790",
    "\ubcc0\uacbd\uc77c\uc790",
    "\uc704\ub3c4",
    "\uacbd\ub3c4",
    "\ub370\uc774\ud130\uae30\uc900\uc77c\uc790",
]
_TYPE_MAP = {
    "\ucd08\ub4f1\ud559\uad50": "ELEMENTARY_SCHOOL",
    "\uc911\ud559\uad50": "MIDDLE_SCHOOL",
    "\uace0\ub4f1\ud559\uad50": "HIGH_SCHOOL",
}
_FOUNDATION_MAP = {
    "\uad6d\ub9bd": "NATIONAL",
    "\uacf5\ub9bd": "PUBLIC",
    "\uc0ac\ub9bd": "PRIVATE",
}


@dataclass(frozen=True)
class StandardSchoolLocation:
    school_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    road_address: str
    latitude: float
    longitude: float
    source_as_of: str


@dataclass(frozen=True)
class StandardSchoolLocationFetch:
    locations: tuple[StandardSchoolLocation, ...]
    raw_sha256: str
    download_url: str
    provenance: EnrichmentProvenance


class StandardSchoolLocationSource:
    def __init__(self, *, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch(self) -> StandardSchoolLocationFetch:
        raw = await get_bytes_with_retry(
            client=self._client,
            url=DOWNLOAD_URL,
            source_label="official school-location download",
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        digest = hashlib.sha256(raw).hexdigest()
        if digest != PINNED_SHA256:
            raise SourceDataError(
                "official school-location attachment SHA-256 changed"
            )
        locations = parse_standard_school_locations(
            raw,
            expected_seoul_count=PINNED_SEOUL_COUNT,
            expected_total_count=PINNED_NATIONWIDE_COUNT,
        )
        return StandardSchoolLocationFetch(
            locations=locations,
            raw_sha256=digest,
            download_url=DOWNLOAD_URL,
            provenance=EnrichmentProvenance(
                source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
                endpoint=DOWNLOAD_URL,
                license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
                attribution="Korea Education Facilities Safety Authority",
                fetched_at=utc_now(),
                source_as_of=PINNED_SOURCE_AS_OF,
                raw_sha256=digest,
                normalized_sha256=_locations_sha256(locations),
                request_region_code="7010000",
                request_timing=None,
                page_count=1,
                fetched_row_count=PINNED_NATIONWIDE_COUNT,
                matched_row_count=0,
            ),
        )


def parse_standard_school_locations(
    raw: bytes,
    *,
    expected_seoul_count: int,
    expected_total_count: int | None = None,
) -> tuple[StandardSchoolLocation, ...]:
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceDataError(
            "official school-location CSV must be UTF-8 with optional BOM"
        ) from exc
    reader = csv.DictReader(io.StringIO(decoded))
    if reader.fieldnames != _FIELDS:
        raise SourceDataError("official school-location CSV fields are invalid")
    locations: list[StandardSchoolLocation] = []
    total_count = 0
    for row in reader:
        total_count += 1
        if _required(row, "\uc2dc\ub3c4\uad50\uc721\uccad\ucf54\ub4dc") != "7010000":
            continue
        if _required(row, "\uc6b4\uc601\uc0c1\ud0dc") != "\uc6b4\uc601":
            raise SourceDataError("Seoul school-location row is not operating")
        try:
            institution_type = _TYPE_MAP[_required(row, "\ud559\uad50\uae09\uad6c\ubd84")]
            foundation_type = _FOUNDATION_MAP[_required(row, "\uc124\ub9bd\ud615\ud0dc")]
            latitude = float(_required(row, "\uc704\ub3c4"))
            longitude = float(_required(row, "\uacbd\ub3c4"))
            source_as_of = _source_date_as_iso(
                _required(row, "\ub370\uc774\ud130\uae30\uc900\uc77c\uc790")
            )
        except (KeyError, ValueError) as exc:
            raise SourceDataError(
                "official school-location row contains an unsupported value"
            ) from exc
        if not (33.0 <= latitude <= 39.5 and 124.0 <= longitude <= 132.0):
            raise SourceDataError("official school-location coordinate is outside Korea")
        locations.append(
            StandardSchoolLocation(
                school_id=_required(row, "\ud559\uad50ID"),
                official_name=_required(row, "\ud559\uad50\uba85"),
                institution_type=institution_type,
                foundation_type=foundation_type,
                road_address=_required(
                    row, "\uc18c\uc7ac\uc9c0\ub3c4\ub85c\uba85\uc8fc\uc18c"
                ),
                latitude=latitude,
                longitude=longitude,
                source_as_of=source_as_of,
            )
        )
    ids = [location.school_id for location in locations]
    if (
        len(locations) != expected_seoul_count
        or len(ids) != len(set(ids))
        or expected_total_count is not None
        and total_count != expected_total_count
    ):
        raise SourceDataError(
            "official Seoul school-location rows are incomplete or duplicated"
        )
    return tuple(locations)


def enrich_neis_coordinates(
    records: tuple[SourceInstitutionRecord, ...],
    locations: tuple[StandardSchoolLocation, ...],
) -> tuple[SourceInstitutionRecord, ...]:
    by_id = {location.school_id: location for location in locations}
    by_composite: dict[
        tuple[str, str, str, str],
        list[StandardSchoolLocation],
    ] = {}
    for source_location in locations:
        by_composite.setdefault(_location_key(source_location), []).append(
            source_location
        )
    enriched: list[SourceInstitutionRecord] = []
    for record in records:
        if record.source != "NEIS":
            enriched.append(record)
            continue
        school_id = record.institution_id.rsplit(":", 1)[-1]
        location: StandardSchoolLocation | None = by_id.get(school_id)
        if location is None:
            composite_matches = by_composite.get(_record_key(record), [])
            if len(composite_matches) > 1:
                raise SourceDataError(
                    "official school-location composite match is ambiguous"
                )
            location = composite_matches[0] if composite_matches else None
        if location is None:
            enriched.append(record)
            continue
        if (
            record.official_name != location.official_name
            or record.institution_type != location.institution_type
            or record.foundation_type != location.foundation_type
            or _normalize(record.road_address) != _normalize(location.road_address)
        ):
            raise SourceDataError(
                "official school-location identity conflicts with NEIS"
            )
        enriched.append(
            replace(
                record,
                latitude=location.latitude,
                longitude=location.longitude,
                coordinate_quality="OFFICIAL_STANDARD_COORDINATE",
            )
        )
    return tuple(enriched)


def _required(row: dict[str, str | None], name: str) -> str:
    value = row.get(name)
    if value is None or not value.strip():
        raise SourceDataError(
            f"official school-location field {name} must be nonblank"
        )
    return value.strip()


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _location_key(
    location: StandardSchoolLocation,
) -> tuple[str, str, str, str]:
    return (
        _normalize(location.official_name),
        location.institution_type,
        location.foundation_type,
        _normalize(location.road_address),
    )


def _record_key(
    record: SourceInstitutionRecord,
) -> tuple[str, str, str, str]:
    return (
        _normalize(record.official_name),
        record.institution_type,
        record.foundation_type,
        _normalize(record.road_address),
    )


def _source_date_as_iso(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def _locations_sha256(locations: tuple[StandardSchoolLocation, ...]) -> str:
    normalized = json.dumps(
        [
            location.__dict__
            for location in sorted(locations, key=lambda row: row.school_id)
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()
