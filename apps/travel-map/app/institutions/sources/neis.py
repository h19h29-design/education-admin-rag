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
    observation_date_counts,
    source_as_of_for,
    utc_now,
)

_ENDPOINT = "https://open.neis.go.kr/hub/schoolInfo"
_MAX_DECLARED_ROWS = 5_000
_MAX_PAGE_COUNT = 200
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_CUMULATIVE_BYTES = 25 * 1024 * 1024
_KNOWN_NO_KEY_SAMPLE_ROWS = 5

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
        failure: str | None = None
        try:
            return await self._fetch_impl()
        except SourceDataError as exc:
            failure = str(exc)
        finally:
            self.clear_credentials()
        raise SourceDataError(failure or "NEIS source validation failed")

    def clear_credentials(self) -> None:
        self._api_key = ""

    async def _fetch_impl(self) -> SourceFetchResult:
        pages: list[bytes] = []
        records: list[SourceInstitutionRecord] = []
        seen_page_ids: set[tuple[str, ...]] = set()
        raw_source_dates: list[str] = []
        declared_total: int | None = None
        raw_row_count = 0
        cumulative_raw_bytes = 0
        page = 1
        while declared_total is None or raw_row_count < declared_total:
            if page > _MAX_PAGE_COUNT:
                raise SourceDataError("NEIS pagination exceeded the page limit")
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
                max_response_bytes=_MAX_RESPONSE_BYTES,
            )
            _raise_neis_error(payload)
            total = _neis_total(payload)
            if declared_total is None:
                if total == _KNOWN_NO_KEY_SAMPLE_ROWS:
                    raise SourceDataError(
                        "NEIS returned the known five-row no-key sample"
                    )
                if total < 1:
                    raise SourceDataError("NEIS returned no selectable source rows")
                if total > _MAX_DECLARED_ROWS:
                    raise SourceDataError("NEIS declared total exceeds row ceiling")
                expected_pages = (total + self._page_size - 1) // self._page_size
                if expected_pages > _MAX_PAGE_COUNT:
                    raise SourceDataError("NEIS declared total exceeds page limit")
                declared_total = total
            elif total != declared_total:
                raise SourceDataError("NEIS list_total_count changed during pagination")
            raw_rows = _neis_rows(payload)
            raw_source_dates.extend(_raw_neis_load_dates(raw_rows))
            if len(raw_rows) > self._page_size:
                raise SourceDataError("NEIS returned more rows than requested page size")
            if raw_row_count + len(raw_rows) > declared_total:
                raise SourceDataError("NEIS returned more rows than list_total_count")
            if (
                raw_row_count + len(raw_rows) < declared_total
                and len(raw_rows) != self._page_size
            ):
                raise SourceDataError(
                    "NEIS returned a short page before list_total_count"
                )
            cumulative_raw_bytes += len(raw)
            if cumulative_raw_bytes > _MAX_CUMULATIVE_BYTES:
                raise SourceDataError("NEIS cumulative response size exceeds limit")
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
        if not records:
            raise SourceDataError("NEIS returned no selectable source rows")
        raw_counts = observation_date_counts(raw_source_dates)
        normalized_counts = observation_date_counts(
            record.source_as_of for record in records
        )
        return SourceFetchResult(
            records=tuple(records),
            provenance=SourceProvenance(
                source="NEIS",
                endpoint=_ENDPOINT,
                license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
                attribution="Ministry of Education NEIS education data",
                fetched_at=utc_now(),
                source_as_of=source_as_of_for(raw_counts),
                source_observation_date_counts=raw_counts,
                normalized_observation_date_counts=normalized_counts,
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
    _raw_neis_load_dates(rows)

    selectable_rows = [
        row
        for row in rows
        if _required_string_from_object(row, "SCHUL_KND_SC_NM")
        not in _NONSELECTABLE_TYPES
    ]
    return tuple(_parse_row(row)[0] for row in selectable_rows)


def _raw_neis_load_dates(rows: list[object]) -> tuple[str, ...]:
    try:
        return tuple(
            _yyyymmdd_as_iso(_required_string_from_object(row, "LOAD_DTM"))
            for row in rows
        )
    except ValueError as exc:
        raise SourceDataError("NEIS row contains an unsupported value") from exc


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
