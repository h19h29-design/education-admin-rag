import hashlib
from collections.abc import Mapping
from datetime import date

import httpx

from app.institutions.sources.common import (
    SourceDataError,
    SourceFetchResult,
    SourceInstitutionRecord,
    SourceProvenance,
    get_json_with_retry,
    normalized_records_sha256,
    utc_now,
)

_ENDPOINT = "https://open.neis.go.kr/hub/schoolInfo"

_FOUNDATION_TYPES = {
    "\uad6d\ub9bd": "NATIONAL",
    "\uacf5\ub9bd": "PUBLIC",
    "\uc0ac\ub9bd": "PRIVATE",
}
_INSTITUTION_TYPES = {
    "\ucd08\ub4f1\ud559\uad50": "ELEMENTARY_SCHOOL",
    "\uc911\ud559\uad50": "MIDDLE_SCHOOL",
    "\uace0\ub4f1\ud559\uad50": "HIGH_SCHOOL",
    "\ud2b9\uc218\ud559\uad50": "SPECIAL_SCHOOL",
    "\uc678\uad6d\uc778\ud559\uad50": "MISC_SCHOOL",
    "\ubc29\uc1a1\ud1b5\uc2e0\uc911\ud559\uad50": "MIDDLE_SCHOOL",
    "\ubc29\uc1a1\ud1b5\uc2e0\uace0\ub4f1\ud559\uad50": "HIGH_SCHOOL",
    "\uac01\uc885\ud559\uad50(\ucd08)": "MISC_SCHOOL",
    "\uac01\uc885\ud559\uad50(\uc911)": "MISC_SCHOOL",
    "\uac01\uc885\ud559\uad50(\uace0)": "MISC_SCHOOL",
    "\uace0\ub4f1\uae30\uc220\ud559\uad50": "MISC_SCHOOL",
}
_NONSELECTABLE_TYPES = {"\uacf5\ub3d9\uc2e4\uc2b5\uc18c"}


class NeisSource:
    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        page_size: int = 1_000,
    ) -> None:
        if not api_key.strip():
            raise SourceDataError("NEIS_API_KEY is required for a complete sync")
        if page_size < 1 or page_size > 1_000:
            raise SourceDataError("NEIS page size must be between 1 and 1000")
        self._api_key = api_key
        self._client = client
        self._page_size = page_size

    async def fetch(self) -> SourceFetchResult:
        pages: list[bytes] = []
        records: list[SourceInstitutionRecord] = []
        seen_page_ids: set[tuple[str, ...]] = set()
        declared_total: int | None = None
        raw_row_count = 0
        page = 1
        while declared_total is None or raw_row_count < declared_total:
            payload, raw = await get_json_with_retry(
                client=self._client,
                url=_ENDPOINT,
                params={
                    "KEY": self._api_key,
                    "Type": "json",
                    "pIndex": page,
                    "pSize": self._page_size,
                    "ATPT_OFCDC_SC_CODE": "B10",
                },
                headers=None,
                source_label="NEIS",
            )
            _raise_neis_error(payload)
            total = _neis_total(payload)
            if declared_total is None:
                declared_total = total
            elif total != declared_total:
                raise SourceDataError("NEIS list_total_count changed during pagination")
            raw_rows = _neis_rows(payload)
            page_ids = tuple(
                _required_string_from_object(row, "SD_SCHUL_CODE")
                for row in raw_rows
            )
            if page_ids in seen_page_ids:
                raise SourceDataError("NEIS returned a repeated page")
            seen_page_ids.add(page_ids)
            parsed = parse_neis_rows(payload)
            pages.append(raw)
            raw_row_count += len(raw_rows)
            records.extend(parsed)
            if not raw_rows and raw_row_count < declared_total:
                raise SourceDataError("NEIS pagination ended before list_total_count")
            page += 1
        if raw_row_count != declared_total:
            raise SourceDataError("NEIS row count does not match list_total_count")
        return SourceFetchResult(
            records=tuple(records),
            provenance=SourceProvenance(
                source="NEIS",
                endpoint=_ENDPOINT,
                license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
                attribution="Ministry of Education NEIS education data",
                fetched_at=utc_now(),
                source_as_of=max(record.source_as_of for record in records),
                raw_sha256=hashlib.sha256(b"".join(pages)).hexdigest(),
                page_count=len(pages),
                row_count=len(records),
                fetched_row_count=raw_row_count,
                request_region_code="B10",
                request_timing=None,
                normalized_sha256=normalized_records_sha256(records),
            ),
        )


def parse_neis_rows(payload: Mapping[str, object]) -> tuple[SourceInstitutionRecord, ...]:
    rows = _neis_rows(payload)

    selectable_rows = [
        row
        for row in rows
        if _required_string_from_object(row, "SCHUL_KND_SC_NM")
        not in _NONSELECTABLE_TYPES
    ]
    parsed_rows = [_parse_row(row) for row in selectable_rows]
    if not parsed_rows:
        return ()
    source_as_of = max(row[1] for row in parsed_rows)
    return tuple(
        SourceInstitutionRecord(
            institution_id=record.institution_id,
            official_name=record.official_name,
            institution_type=record.institution_type,
            foundation_type=record.foundation_type,
            education_office=record.education_office,
            road_address=record.road_address,
            district=record.district,
            latitude=None,
            longitude=None,
            source="NEIS",
            source_region_code="B10",
            source_as_of=source_as_of,
            coordinate_quality="MISSING",
        )
        for record, _ in parsed_rows
    )


def _neis_rows(payload: Mapping[str, object]) -> list[object]:
    try:
        sections = payload["schoolInfo"]
        if type(sections) is not list or len(sections) != 2:
            raise SourceDataError("NEIS schoolInfo response shape is invalid")
        rows_node = sections[1]
        if type(rows_node) is not dict or type(rows_node.get("row")) is not list:
            raise SourceDataError("NEIS schoolInfo rows are missing")
        rows = rows_node["row"]
    except KeyError as exc:
        raise SourceDataError("NEIS schoolInfo response shape is invalid") from exc
    return rows


def _raise_neis_error(payload: Mapping[str, object]) -> None:
    result = payload.get("RESULT")
    if type(result) is dict and result.get("CODE") != "INFO-000":
        raise SourceDataError("NEIS rejected the request or API key")


def _neis_total(payload: Mapping[str, object]) -> int:
    try:
        sections = payload["schoolInfo"]
        if type(sections) is not list or not sections:
            raise SourceDataError("NEIS schoolInfo response shape is invalid")
        first = sections[0]
        if type(first) is not dict:
            raise SourceDataError("NEIS schoolInfo head is invalid")
        head = first["head"]
        if type(head) is not list or not head:
            raise SourceDataError("NEIS schoolInfo head is invalid")
        total_node = head[0]
        if type(total_node) is not dict:
            raise SourceDataError("NEIS list_total_count is invalid")
        total = total_node["list_total_count"]
        if type(total) is not int or total < 0:
            raise SourceDataError("NEIS list_total_count is invalid")
        return total
    except KeyError as exc:
        raise SourceDataError("NEIS list_total_count is missing") from exc


def _parse_row(row: object) -> tuple[SourceInstitutionRecord, str]:
    if type(row) is not dict:
        raise SourceDataError("NEIS row must be an object")
    try:
        region_code = _required_string(row, "ATPT_OFCDC_SC_CODE")
        if region_code != "B10":
            raise SourceDataError("NEIS row is not in the B10 source region")
        school_code = _required_string(row, "SD_SCHUL_CODE")
        foundation = _FOUNDATION_TYPES[_required_string(row, "FOND_SC_NM")]
        institution_type = _INSTITUTION_TYPES[
            _required_string(row, "SCHUL_KND_SC_NM")
        ]
        road_address = _required_string(row, "ORG_RDNMA")
        loaded = _yyyymmdd_as_iso(_required_string(row, "LOAD_DTM"))
    except (KeyError, ValueError) as exc:
        raise SourceDataError("NEIS row contains an unsupported value") from exc
    record = SourceInstitutionRecord(
        institution_id=f"neis:B10:{school_code}",
        official_name=_required_string(row, "SCHUL_NM"),
        institution_type=institution_type,
        foundation_type=foundation,
        education_office=_required_string(row, "JU_ORG_NM"),
        road_address=road_address,
        district=_district_from_address(road_address),
        latitude=None,
        longitude=None,
        source="NEIS",
        source_region_code="B10",
        source_as_of=loaded,
        coordinate_quality="MISSING",
    )
    return record, loaded


def _required_string(row: dict[object, object], name: str) -> str:
    value = row.get(name)
    if type(value) is not str or not value.strip():
        raise SourceDataError(f"NEIS field {name} must be a nonblank string")
    return value.strip()


def _required_string_from_object(row: object, name: str) -> str:
    if type(row) is not dict:
        raise SourceDataError("NEIS row must be an object")
    return _required_string(row, name)


def _district_from_address(address: str) -> str:
    parts = address.split()
    if len(parts) < 2:
        raise SourceDataError("NEIS road address has no district")
    return parts[1]


def _yyyymmdd_as_iso(value: str) -> str:
    if len(value) != 8 or not value.isdigit():
        raise ValueError("date must be YYYYMMDD")
    return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
