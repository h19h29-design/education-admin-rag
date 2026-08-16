import csv
import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from app.institutions.sources.common import (
    SourceDataError,
    SourceFetchResult,
    SourceInstitutionRecord,
    SourceInstitutionSiteRecord,
    SourceProvenance,
    normalized_records_sha256,
    observation_date_counts,
    utc_now,
)

_ALLOWED_TYPES = {
    "HEADQUARTERS",
    "DISTRICT_OFFICE",
    "DIRECT_AGENCY",
    "LIBRARY",
    "LIFELONG_LEARNING_CENTER",
}
_FIELDS = {
    "institution_id",
    "official_name",
    "institution_type",
    "foundation_type",
    "education_office",
    "road_address",
    "district",
    "latitude",
    "longitude",
    "site_code",
    "site_name",
    "is_default",
}
_SOURCE_URL = "https://www.sen.go.kr/www/website.jsp"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_INSTITUTION_ID = re.compile(r"sen:[a-z0-9]+(?:-[a-z0-9]+)*")
_SAFE_SITE_CODE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PINNED_SOURCE_SHA256 = (
    "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2"
)
_PINNED_NORMALIZED_SHA256 = (
    "c2b7e84c476175586b9f3764f54ee008fc35cb7831b4a8a0186ded9b608aac50"
)
_PINNED_RECORDS_SHA256 = (
    "8cd2aa66f3df95a25a2127eaa2791e876f2d21cd7bc47aa700d34be75293b3b3"
)
_DIRECTORY_RAW_SHA256 = (
    "9f202202edc653b09b4debb5a0ff939cf9fcdc64dd58174b28f8d009bb1b7424"
)
_MAIN_SITE_SOURCE_URL = (
    "https://gslib.sen.go.kr/gslib/html.do?menu_idx=52"
)
_MAIN_SITE_RAW_SHA256 = (
    "312ca8f63086188dabcb272ed3a2bfdfdb0d2c360f010cdc1fb59e6ff90288e7"
)
_BRANCH_SITE_SOURCE_URL = (
    "https://gylib.sen.go.kr/gylib/html.do?menu_idx=43"
)
_BRANCH_SITE_RAW_SHA256 = (
    "b3036767d04ef37b77d72c617ca21b052b83682eeab6f19ecb15a8a0aa54dd49"
)
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


class SenCsvSource:
    def __init__(
        self,
        path: Path,
        *,
        expected_type_counts: Mapping[str, int],
    ) -> None:
        self._path = Path(path)
        self._expected_type_counts = dict(expected_type_counts)

    def load(self) -> SourceFetchResult:
        records, metadata, fetched_row_count = _parse_sen_csv(self._path)
        if (
            metadata["source_sha256"] != _PINNED_SOURCE_SHA256
            or metadata["normalized_sha256"] != _PINNED_NORMALIZED_SHA256
            or normalized_records_sha256(records) != _PINNED_RECORDS_SHA256
            or metadata["source_as_of"] != "2026-08-10"
            or metadata["license_name"] != "KOGL_TYPE_1_ATTRIBUTION"
            or metadata["attribution"]
            != "Source: Seoul Metropolitan Office of Education "
            "(organization directory and 2026 civil-service handbook)"
        ):
            raise SourceDataError("SEN CSV is not the reviewed official resource")
        actual_counts = Counter(record.institution_type for record in records)
        if actual_counts != Counter(self._expected_type_counts):
            raise SourceDataError("SEN CSV organization totals do not match official counts")
        raw_counts = observation_date_counts(
            metadata["source_as_of"] for _ in range(fetched_row_count)
        )
        normalized_counts = observation_date_counts(
            metadata["source_as_of"] for _ in records
        )
        return SourceFetchResult(
            records=records,
            provenance=SourceProvenance(
                source="SEN_REVIEWED_CSV",
                endpoint=metadata["source_url"],
                license_name=metadata["license_name"],
                attribution=metadata["attribution"],
                fetched_at=utc_now(),
                source_as_of=metadata["source_as_of"],
                source_observation_date_counts=raw_counts,
                normalized_observation_date_counts=normalized_counts,
                raw_sha256=metadata["source_sha256"],
                page_count=1,
                row_count=len(records),
                fetched_row_count=fetched_row_count,
                request_region_code="SEOUL",
                request_timing=None,
                normalized_sha256=normalized_records_sha256(records),
            ),
        )


def parse_sen_csv(path: Path) -> tuple[SourceInstitutionRecord, ...]:
    records, _metadata, _fetched_row_count = _parse_sen_csv(path)
    return records


def _parse_sen_csv(
    path: Path,
) -> tuple[tuple[SourceInstitutionRecord, ...], dict[str, str], int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    metadata: dict[str, str] = {}
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("# "):
            key, separator, value = line[2:].partition("=")
            if not separator or not key or not value:
                raise SourceDataError("SEN CSV metadata is invalid")
            metadata[key] = value
        elif line.strip():
            data_lines.append(line)
    if set(metadata) != {
        "source_url",
        "source_as_of",
        "source_sha256",
        "normalized_sha256",
        "directory_source_raw_sha256",
        "main_site_source_url",
        "main_site_source_raw_sha256",
        "branch_site_source_url",
        "branch_site_source_raw_sha256",
        "site_sources_checked_at",
        "license_name",
        "attribution",
    }:
        raise SourceDataError("SEN CSV provenance is incomplete")
    if metadata["source_url"] != _SOURCE_URL:
        raise SourceDataError("SEN CSV source URL is not official")
    if _SHA256.fullmatch(metadata["source_sha256"]) is None:
        raise SourceDataError("SEN CSV source SHA-256 is invalid")
    normalized_digest = hashlib.sha256(
        ("\n".join(data_lines) + "\n").encode("utf-8")
    ).hexdigest()
    if (
        _SHA256.fullmatch(metadata["normalized_sha256"]) is None
        or metadata["normalized_sha256"] != normalized_digest
        or metadata["directory_source_raw_sha256"] != _DIRECTORY_RAW_SHA256
        or metadata["main_site_source_url"] != _MAIN_SITE_SOURCE_URL
        or metadata["main_site_source_raw_sha256"] != _MAIN_SITE_RAW_SHA256
        or metadata["branch_site_source_url"] != _BRANCH_SITE_SOURCE_URL
        or metadata["branch_site_source_raw_sha256"]
        != _BRANCH_SITE_RAW_SHA256
        or metadata["site_sources_checked_at"] != "2026-08-11"
        or metadata["source_sha256"] != _combined_source_sha256()
    ):
        raise SourceDataError("SEN CSV normalized resource SHA-256 is invalid")
    reader = csv.DictReader(data_lines)
    if reader.fieldnames is None or set(reader.fieldnames) != _FIELDS:
        raise SourceDataError("SEN CSV fields are invalid")
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    fetched_row_count = 0
    for row in reader:
        fetched_row_count += 1
        institution_type = _nonblank(row, "institution_type")
        if institution_type not in _ALLOWED_TYPES:
            raise SourceDataError("SEN CSV institution type is unsupported")
        institution_id = _decoded_nonblank(row, "institution_id")
        site_code = _decoded_nonblank(row, "site_code")
        if (
            _SAFE_INSTITUTION_ID.fullmatch(institution_id) is None
            or _SAFE_SITE_CODE.fullmatch(site_code) is None
        ):
            raise SourceDataError("SEN CSV institution or site identifier is invalid")
        latitude = _optional_float(row, "latitude")
        longitude = _optional_float(row, "longitude")
        if (latitude is None) != (longitude is None):
            raise SourceDataError("SEN CSV coordinate pair is incomplete")
        is_default = _nonblank(row, "is_default")
        if is_default not in {"true", "false"}:
            raise SourceDataError("SEN CSV default-site flag is invalid")
        grouped[institution_id].append(
            {
                "official_name": _decoded_nonblank(row, "official_name"),
                "institution_type": institution_type,
                "foundation_type": _decoded_nonblank(row, "foundation_type"),
                "education_office": _decoded_nonblank(row, "education_office"),
                "road_address": _decoded_nonblank(row, "road_address"),
                "district": _decoded_nonblank(row, "district"),
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_quality": (
                    "MANUALLY_VERIFIED" if latitude is not None else "MISSING"
                ),
                "site_code": site_code,
                "site_name": _decoded_nonblank(row, "site_name"),
                "is_default": is_default == "true",
            }
        )

    records: list[SourceInstitutionRecord] = []
    core_fields = (
        "official_name",
        "institution_type",
        "foundation_type",
        "education_office",
    )
    for institution_id, sites in grouped.items():
        first = sites[0]
        if any(
            any(site[field] != first[field] for field in core_fields)
            for site in sites[1:]
        ):
            raise SourceDataError("SEN CSV repeated institution fields conflict")
        site_codes = [str(site["site_code"]) for site in sites]
        default_sites = [site for site in sites if site["is_default"] is True]
        if (
            len(set(site_codes)) != len(site_codes)
            or len(default_sites) != 1
            or default_sites[0]["site_code"] != "main"
        ):
            raise SourceDataError("SEN CSV site defaults or identifiers are invalid")
        main = default_sites[0]
        additional_sites = tuple(
            SourceInstitutionSiteRecord(
                site_code=str(site["site_code"]),
                site_name=str(site["site_name"]),
                road_address=str(site["road_address"]),
                district=str(site["district"]),
                latitude=cast(float | None, site["latitude"]),
                longitude=cast(float | None, site["longitude"]),
                coordinate_quality=str(site["coordinate_quality"]),
            )
            for site in sorted(sites, key=lambda item: str(item["site_code"]))
            if site is not main
        )
        records.append(
            SourceInstitutionRecord(
                institution_id=institution_id,
                official_name=str(main["official_name"]),
                institution_type=str(main["institution_type"]),
                foundation_type=str(main["foundation_type"]),
                education_office=str(main["education_office"]),
                road_address=str(main["road_address"]),
                district=str(main["district"]),
                latitude=cast(float | None, main["latitude"]),
                longitude=cast(float | None, main["longitude"]),
                source="SEN_REVIEWED_CSV",
                source_region_code="SEOUL",
                source_as_of=metadata["source_as_of"],
                coordinate_quality=str(main["coordinate_quality"]),
                site_name=str(main["site_name"]),
                additional_sites=additional_sites,
            )
        )
    return tuple(records), metadata, fetched_row_count


def _combined_source_sha256() -> str:
    body = (
        f"directory={_DIRECTORY_RAW_SHA256}\n"
        f"main={_MAIN_SITE_RAW_SHA256}\n"
        f"gayang={_BRANCH_SITE_RAW_SHA256}\n"
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _nonblank(row: dict[str, str | None], name: str) -> str:
    value = row.get(name)
    if value is None or not value.strip():
        raise SourceDataError(f"SEN CSV field {name} must be nonblank")
    return value.strip()


def _decoded_nonblank(row: dict[str, str | None], name: str) -> str:
    value = _nonblank(row, name)
    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), value)


def _optional_float(row: dict[str, str | None], name: str) -> float | None:
    value = row.get(name)
    return None if value is None or not value.strip() else float(value)
