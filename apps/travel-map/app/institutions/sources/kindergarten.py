import csv
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

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

_ENDPOINT = "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
_REGION_SOURCE_URL = (
    "https://e-childschoolinfo.moe.go.kr/openApi/sidoSigunguCode.do"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PINNED_REGION_RAW_SHA256 = (
    "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925"
)
_PINNED_REGION_NORMALIZED_SHA256 = (
    "13d86558212df3cc0739d240ee902cfd38da4d6d54ae87cea71bda112d5cd1f3"
)
_MAX_CUMULATIVE_BYTES = 25 * 1024 * 1024

_FOUNDATION_TYPES = {
    "\uad6d\ub9bd": "NATIONAL",
    "\uacf5\ub9bd(\ub2e8\uc124)": "PUBLIC",
    "\uacf5\ub9bd(\ubcd1\uc124)": "PUBLIC",
    "\uc0ac\ub9bd(\ubc95\uc778)": "PRIVATE",
    "\uc0ac\ub9bd(\uc0ac\uc778)": "PRIVATE",
}


class KindergartenSource:
    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        region_codes_path: Path,
        timing: str,
        page_size: int = 100,
    ) -> None:
        if not api_key.strip():
            raise SourceDataError(
                "KINDERGARTEN_API_KEY is required for a complete sync"
            )
        if re.fullmatch(r"\d{4}[12]", timing) is None:
            raise SourceDataError("kindergarten disclosure timing is invalid")
        if page_size < 1 or page_size > 100:
            raise SourceDataError(
                "kindergarten page size must be between 1 and 100"
            )
        self._api_key = api_key
        self._client = client
        self._regions_path = Path(region_codes_path)
        self._timing = timing
        self._page_size = page_size

    async def fetch(self) -> SourceFetchResult:
        failure: str | None = None
        try:
            return await self._fetch_impl()
        except SourceDataError as exc:
            failure = str(exc)
        finally:
            self.clear_credentials()
        raise SourceDataError(
            failure or "kindergarten source validation failed"
        )

    def clear_credentials(self) -> None:
        self._api_key = ""

    async def _fetch_impl(self) -> SourceFetchResult:
        regions = parse_kindergarten_region_codes(
            self._regions_path,
            expected_timing=self._timing,
        )
        pages: list[bytes] = []
        cumulative_bytes = 0
        records: list[SourceInstitutionRecord] = []
        for sido_code, sgg_code, _district in regions:
            seen_page_ids: set[tuple[str, ...]] = set()
            page = 1
            while True:
                if page > 100:
                    raise SourceDataError(
                        "kindergarten pagination exceeded the page limit"
                    )
                payload, raw = await get_json_with_retry(
                    client=self._client,
                    url=_ENDPOINT,
                    params={
                        "key": self._api_key,
                        "sidoCode": sido_code,
                        "sggCode": sgg_code,
                        "pageCnt": self._page_size,
                        "currentPage": page,
                        "timing": self._timing,
                    },
                    headers=None,
                    source_label="kindergarten",
                )
                cumulative_bytes += len(raw)
                if cumulative_bytes > _MAX_CUMULATIVE_BYTES:
                    raise SourceDataError(
                        "kindergarten cumulative response size exceeds the trusted limit"
                    )
                _validate_response_echo(
                    payload,
                    sido_code=sido_code,
                    sgg_code=sgg_code,
                    page=page,
                    page_size=self._page_size,
                )
                parsed = parse_kindergarten_rows(
                    payload,
                    source_as_of=_timing_as_date(self._timing),
                    expected_timing=self._timing,
                )
                page_ids = tuple(record.institution_id for record in parsed)
                if page_ids in seen_page_ids:
                    raise SourceDataError("kindergarten returned a repeated page")
                seen_page_ids.add(page_ids)
                pages.append(raw)
                records.extend(parsed)
                if len(parsed) < self._page_size:
                    break
                page += 1
        ids = [record.institution_id for record in records]
        if len(ids) != len(set(ids)):
            raise SourceDataError("kindergarten source returned duplicate identifiers")
        return SourceFetchResult(
            records=tuple(records),
            provenance=SourceProvenance(
                source="KINDERGARTEN_INFO",
                endpoint=_ENDPOINT,
                license_name="PUBLIC_DATA_PORTAL_TERMS",
                attribution="Ministry of Education Kindergarten Info",
                fetched_at=utc_now(),
                source_as_of=_timing_as_date(self._timing),
                raw_sha256=hashlib.sha256(b"".join(pages)).hexdigest(),
                page_count=len(pages),
                row_count=len(records),
                fetched_row_count=len(records),
                request_region_code="11",
                request_timing=self._timing,
                normalized_sha256=normalized_records_sha256(records),
            ),
        )


def parse_kindergarten_rows(
    payload: Mapping[str, object],
    *,
    source_as_of: str | None = None,
    expected_timing: str | None = None,
) -> tuple[SourceInstitutionRecord, ...]:
    if payload.get("status") != "SUCCESS":
        raise SourceDataError("kindergarten source did not return SUCCESS")
    rows = payload.get("kinderInfo")
    if type(rows) is not list:
        raise SourceDataError("kindergarten source rows are missing")
    if expected_timing is not None:
        response_timing = payload.get("timing")
        if response_timing is not None and str(response_timing) != expected_timing:
            raise SourceDataError("kindergarten response timing does not match request")
        for row in rows:
            if type(row) is not dict or row.get("pbnttmng") != expected_timing:
                raise SourceDataError("kindergarten row timing does not match request")
    timing = payload.get("timing")
    if source_as_of is not None:
        selected_as_of = source_as_of
    elif type(timing) is str and re.fullmatch(r"\d{5}", timing):
        selected_as_of = _timing_as_date(timing)
    else:
        raise SourceDataError("kindergarten source timing must be pinned")
    return tuple(_parse_row(row, selected_as_of) for row in rows)


def parse_kindergarten_region_codes(
    path: Path,
    *,
    expected_count: int = 25,
    expected_timing: str | None = None,
) -> tuple[tuple[str, str, str], ...]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    metadata: dict[str, str] = {}
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("# "):
            key, separator, value = line[2:].partition("=")
            if not separator or not key or not value:
                raise SourceDataError("kindergarten region metadata is invalid")
            metadata[key] = value
        elif line.strip():
            data_lines.append(line)
    expected_metadata = {
        "source_url",
        "source_as_of",
        "source_sha256",
        "normalized_sha256",
        "timing",
        "license_name",
        "attribution",
    }
    if set(metadata) != expected_metadata:
        raise SourceDataError("kindergarten region provenance is incomplete")
    if metadata["source_url"] != _REGION_SOURCE_URL:
        raise SourceDataError("kindergarten region source URL is not official")
    if (
        _SHA256.fullmatch(metadata["source_sha256"]) is None
        or metadata["source_sha256"] != _PINNED_REGION_RAW_SHA256
    ):
        raise SourceDataError("kindergarten region source SHA-256 is invalid")
    normalized_digest = hashlib.sha256(
        ("\n".join(data_lines) + "\n").encode("utf-8")
    ).hexdigest()
    if (
        metadata["normalized_sha256"] != normalized_digest
        or expected_count == 25
        and normalized_digest != _PINNED_REGION_NORMALIZED_SHA256
    ):
        raise SourceDataError(
            "kindergarten region normalized resource is not reviewed"
        )
    if re.fullmatch(r"\d{4}[12]", metadata["timing"]) is None:
        raise SourceDataError("kindergarten region timing is invalid")
    if (
        metadata["source_as_of"] != "2026-08-10"
        or metadata["license_name"] != "PUBLIC_DATA_PORTAL_TERMS"
        or metadata["attribution"]
        != "Source: Ministry of Education Kindergarten Info"
    ):
        raise SourceDataError("kindergarten region provenance is not reviewed")
    if (
        expected_timing is not None
        and metadata["timing"] != expected_timing
    ):
        raise SourceDataError(
            "kindergarten region timing does not match requested timing"
        )
    reader = csv.DictReader(data_lines)
    if reader.fieldnames != ["sido_code", "sgg_code", "district"]:
        raise SourceDataError("kindergarten region fields are invalid")
    regions: list[tuple[str, str, str]] = []
    for row in reader:
        sido = _region_value(row, "sido_code")
        sgg = _region_value(row, "sgg_code")
        district = _region_value(row, "district")
        if sido != "11" or re.fullmatch(r"11\d{3}", sgg) is None:
            raise SourceDataError("kindergarten region row is outside Seoul")
        regions.append((sido, sgg, district))
    if len(regions) != expected_count or len({row[1] for row in regions}) != len(
        regions
    ):
        raise SourceDataError("kindergarten region rows are incomplete or duplicated")
    return tuple(regions)


def _parse_row(row: object, source_as_of: str) -> SourceInstitutionRecord:
    if type(row) is not dict:
        raise SourceDataError("kindergarten row must be an object")
    code = _aliased_required_string(row, "kinderCode", "kindercode")
    _aliased_required_string(row, "rpstYn", "rpst_yn")
    address = _required_string(row, "addr")
    try:
        foundation = _FOUNDATION_TYPES[_required_string(row, "establish")]
        latitude, longitude = _coordinates(row)
    except (KeyError, ValueError) as exc:
        raise SourceDataError("kindergarten row contains an unsupported value") from exc
    return SourceInstitutionRecord(
        institution_id=f"kinder:{code}",
        official_name=_required_string(row, "kindername"),
        institution_type="KINDERGARTEN",
        foundation_type=foundation,
        education_office=_required_string(row, "subofficeedu"),
        road_address=address,
        district=_district_from_address(address),
        latitude=latitude,
        longitude=longitude,
        source="KINDERGARTEN_INFO",
        source_region_code="11",
        source_as_of=source_as_of,
        coordinate_quality=(
            "SOURCE_COORDINATE" if latitude is not None else "MISSING"
        ),
    )


def _required_string(row: dict[object, object], name: str) -> str:
    value = row.get(name)
    if type(value) is not str or not value.strip():
        raise SourceDataError(f"kindergarten field {name} must be nonblank")
    return value.strip()


def _aliased_required_string(
    row: dict[object, object], documented: str, observed: str
) -> str:
    documented_value = row.get(documented)
    observed_value = row.get(observed)
    if (
        documented_value is not None
        and observed_value is not None
        and documented_value != observed_value
    ):
        raise SourceDataError(
            f"kindergarten fields {documented}/{observed} have conflicting values"
        )
    selected = documented_value if documented_value is not None else observed_value
    if type(selected) is not str or not selected.strip():
        raise SourceDataError(
            f"kindergarten field {documented}/{observed} must be nonblank"
        )
    return selected.strip()


def _coordinates(
    row: dict[object, object],
) -> tuple[float | None, float | None]:
    latitude = row.get("lttdcdnt")
    longitude = row.get("lngtcdnt")
    if latitude in (None, "") and longitude in (None, ""):
        return None, None
    if type(latitude) is not str or type(longitude) is not str:
        raise SourceDataError("kindergarten coordinates must be strings")
    if not latitude.strip() or not longitude.strip():
        raise SourceDataError("kindergarten coordinate pair is incomplete")
    return float(latitude), float(longitude)


def _district_from_address(address: str) -> str:
    parts = address.split()
    if len(parts) < 2:
        raise SourceDataError("kindergarten address has no district")
    return parts[1]


def _region_value(row: dict[str, str | None], name: str) -> str:
    value = row.get(name)
    if value is None or not value.strip():
        raise SourceDataError(f"kindergarten region field {name} must be nonblank")
    return value.strip()


def _timing_as_date(timing: str) -> str:
    year = int(timing[:4])
    round_number = timing[4]
    return f"{year}-04-01" if round_number == "1" else f"{year}-10-01"


def _validate_response_echo(
    payload: Mapping[str, object],
    *,
    sido_code: str,
    sgg_code: str,
    page: int,
    page_size: int,
) -> None:
    expected = {
        "sidoList": sido_code,
        "sggList": sgg_code,
        "currentPage": str(page),
        "pageCnt": str(page_size),
    }
    if any(
        name not in payload or str(payload[name]) != value
        for name, value in expected.items()
    ):
        raise SourceDataError(
            "kindergarten response echo does not match the request"
        )
