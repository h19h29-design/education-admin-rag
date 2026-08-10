import csv
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from app.institutions.sources.common import (
    SourceDataError,
    SourceFetchResult,
    SourceInstitutionRecord,
    SourceProvenance,
    normalized_records_sha256,
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
}
_SOURCE_URL = "https://www.sen.go.kr/www/website.jsp"
_SHA256 = re.compile(r"[0-9a-f]{64}")
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
        records, metadata = _parse_sen_csv(self._path)
        actual_counts = Counter(record.institution_type for record in records)
        if actual_counts != Counter(self._expected_type_counts):
            raise SourceDataError("SEN CSV organization totals do not match official counts")
        return SourceFetchResult(
            records=records,
            provenance=SourceProvenance(
                source="SEN_REVIEWED_CSV",
                endpoint=metadata["source_url"],
                license_name=metadata["license_name"],
                attribution=metadata["attribution"],
                fetched_at=utc_now(),
                source_as_of=metadata["source_as_of"],
                raw_sha256=metadata["source_sha256"],
                page_count=1,
                row_count=len(records),
                fetched_row_count=len(records),
                request_region_code="SEOUL",
                request_timing=None,
                normalized_sha256=normalized_records_sha256(records),
            ),
        )


def parse_sen_csv(path: Path) -> tuple[SourceInstitutionRecord, ...]:
    records, _metadata = _parse_sen_csv(path)
    return records


def _parse_sen_csv(
    path: Path,
) -> tuple[tuple[SourceInstitutionRecord, ...], dict[str, str]]:
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
        "license_name",
        "attribution",
    }:
        raise SourceDataError("SEN CSV provenance is incomplete")
    if metadata["source_url"] != _SOURCE_URL:
        raise SourceDataError("SEN CSV source URL is not official")
    if _SHA256.fullmatch(metadata["source_sha256"]) is None:
        raise SourceDataError("SEN CSV source SHA-256 is invalid")
    reader = csv.DictReader(data_lines)
    if reader.fieldnames is None or set(reader.fieldnames) != _FIELDS:
        raise SourceDataError("SEN CSV fields are invalid")
    records: list[SourceInstitutionRecord] = []
    for row in reader:
        institution_type = _nonblank(row, "institution_type")
        if institution_type not in _ALLOWED_TYPES:
            raise SourceDataError("SEN CSV institution type is unsupported")
        records.append(
            SourceInstitutionRecord(
                institution_id=_decoded_nonblank(row, "institution_id"),
                official_name=_decoded_nonblank(row, "official_name"),
                institution_type=institution_type,
                foundation_type=_decoded_nonblank(row, "foundation_type"),
                education_office=_decoded_nonblank(row, "education_office"),
                road_address=_decoded_nonblank(row, "road_address"),
                district=_decoded_nonblank(row, "district"),
                latitude=_optional_float(row, "latitude"),
                longitude=_optional_float(row, "longitude"),
                source="SEN_REVIEWED_CSV",
                source_region_code="SEOUL",
                source_as_of=metadata["source_as_of"],
                coordinate_quality=(
                    "MANUALLY_VERIFIED"
                    if row.get("latitude", "").strip()
                    else "MISSING"
                ),
            )
        )
    for record in records:
        if (record.latitude is None) != (record.longitude is None):
            raise SourceDataError("SEN CSV coordinate pair is incomplete")
    return tuple(records), metadata


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
