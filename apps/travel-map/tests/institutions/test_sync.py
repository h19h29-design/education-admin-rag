import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import traceback
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import app.institutions.sources.kindergarten as kindergarten_module
import app.institutions.sources.neis as neis_module
import app.institutions.sources.standard_school as standard_school_module
import app.institutions.sync as sync_module
import app.providers.kakao_local as kakao_module
import httpx
import pytest
from app.institutions.models import InstitutionStatus
from app.institutions.snapshot import VerifiedSnapshot, verify_snapshot
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceInstitutionRecord,
    SourceInstitutionSiteRecord,
    SourceProvenance,
    get_json_with_retry,
    normalized_records_sha256,
)
from app.institutions.sources.kindergarten import (
    KindergartenSource,
    parse_kindergarten_region_codes,
    parse_kindergarten_rows,
)
from app.institutions.sources.neis import NeisSource, parse_neis_rows
from app.institutions.sources.sen import SenCsvSource, parse_sen_csv
from app.institutions.sources.standard_school import (
    StandardSchoolLocationSource,
    enrich_neis_coordinates,
    parse_standard_school_locations,
)
from app.institutions.sync import (
    SnapshotBuildResult,
    SnapshotQualityError,
    build_candidate_snapshot,
    enrichment_records_sha256,
    geocode_missing_records,
    promote_snapshot,
    reconcile_selectable_school_counts,
)
from app.policy.coverage import CoverageService
from app.providers.kakao_local import KakaoLocalClient

SOURCE_FIXTURES = Path("apps/travel-map/tests/fixtures/institutions/sources")
SOURCE_RESOURCES = Path("apps/travel-map/resources/institution-sources")
TEST_COVERAGE = CoverageService.from_geojson(
    seoul_path="apps/travel-map/resources/geodata/seoul.geojson",
    buffer_distance_m=12_000,
)


def load_json(name: str) -> dict[str, object]:
    path = SOURCE_FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8"))


def assert_secret_absent_from_app_traceback(
    error: BaseException,
    traceback_value: TracebackType | None,
    secret: str,
) -> None:
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    current = traceback_value
    while current is not None:
        frame = current.tb_frame
        if "/apps/travel-map/app/" in frame.f_code.co_filename:
            assert not _contains_secret(frame.f_locals, secret)
        current = current.tb_next


def _contains_secret(
    value: object,
    secret: str,
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> bool:
    if seen is None:
        seen = set()
    if depth > 6 or id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, str):
        return secret in value
    if isinstance(value, bytes):
        return secret.encode() in value
    if isinstance(value, Mapping):
        return any(
            _contains_secret(item, secret, seen=seen, depth=depth + 1)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _contains_secret(item, secret, seen=seen, depth=depth + 1)
            for item in value
        )
    if isinstance(value, httpx.Request):
        return _contains_secret(
            (str(value.url), dict(value.headers)),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    if isinstance(value, httpx.Response):
        request = value.request if value.has_request else None
        return _contains_secret(
            (value.content, request),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    if type(value).__module__.startswith("app.") and hasattr(value, "__dict__"):
        return _contains_secret(
            vars(value),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    return False


# Production break caught: merging private schools or co-located kindergartens into
# another source's identity instead of preserving the official namespace.
def test_source_ids_are_namespaced_and_private_schools_are_kept() -> None:
    neis = parse_neis_rows(load_json("neis-school-info.json"))
    kinder = parse_kindergarten_rows(load_json("kindergarten-info.json"))
    sen = parse_sen_csv(SOURCE_FIXTURES / "sen-institutions.csv")

    assert {row.institution_id for row in neis} == {
        "neis:B10:7010001",
        "neis:B10:7010002",
    }
    assert {row.foundation_type for row in neis} == {"PUBLIC", "PRIVATE"}
    assert kinder[0].institution_id == "kinder:K12345678"
    assert sen[0].institution_id == "sen:headquarters"
    assert not hasattr(kinder[0], "telephone")
    assert not hasattr(kinder[0], "representative")


# Production break caught: the live NEIS type labels being dropped because the
# importer recognizes only the broad labels shown in an older implementation plan.
@pytest.mark.parametrize(
    ("source_type", "expected_type"),
    [
        ("\uc678\uad6d\uc778\ud559\uad50", "MISC_SCHOOL"),
        ("\ubc29\uc1a1\ud1b5\uc2e0\uc911\ud559\uad50", "MIDDLE_SCHOOL"),
        ("\ubc29\uc1a1\ud1b5\uc2e0\uace0\ub4f1\ud559\uad50", "HIGH_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\ucd08)", "MISC_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\uc911)", "MISC_SCHOOL"),
        ("\uac01\uc885\ud559\uad50(\uace0)", "MISC_SCHOOL"),
        ("\uace0\ub4f1\uae30\uc220\ud559\uad50", "MISC_SCHOOL"),
    ],
)
def test_neis_maps_every_verified_selectable_school_type(
    source_type: str,
    expected_type: str,
) -> None:
    payload = neis_payload(source_type=source_type)

    assert parse_neis_rows(payload)[0].institution_type == expected_type


# Production break caught: publishing a training facility as a route-selectable school.
def test_neis_explicitly_excludes_nonselectable_joint_training_center() -> None:
    payload = neis_payload(source_type="\uacf5\ub3d9\uc2e4\uc2b5\uc18c")

    assert parse_neis_rows(payload) == ()


# Production break caught: coercing a newly introduced establishment category to
# PRIVATE and silently skewing public/private totals.
def test_neis_rejects_unknown_foundation() -> None:
    payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["FOND_SC_NM"] = "\ubbf8\ud655\uc778"

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload)


# Production break caught: losing live kindergarten rows because the documentation
# and documentation UI use two different exact aliases for the identifier.
def test_kindergarten_accepts_observed_lowercase_aliases() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["kindercode"] = row.pop("kinderCode")
    row["rpst_yn"] = row.pop("rpstYn")

    assert parse_kindergarten_rows(payload)[0].institution_id == "kinder:K12345678"


# Production break caught: accepting an ambiguous record whose documented and
# observed identifier aliases disagree.
def test_kindergarten_rejects_conflicting_identifier_aliases() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["kindercode"] = "DIFFERENT"

    with pytest.raises(SourceDataError, match="conflicting"):
        parse_kindergarten_rows(payload)


# Production break caught: aborting an otherwise complete disclosure round instead
# of quarantining the single live row with no coordinates.
def test_kindergarten_preserves_missing_coordinate_for_quarantine() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["lttdcdnt"] = ""
    row["lngtcdnt"] = ""

    parsed = parse_kindergarten_rows(payload)

    assert (parsed[0].latitude, parsed[0].longitude) == (None, None)
    assert parsed[0].coordinate_quality == "MISSING"


# Production break caught: silently mixing disclosure rounds when timing is omitted
# or a row contains a different official disclosure period.
def test_kindergarten_requires_one_pinned_disclosure_timing() -> None:
    payload = kindergarten_payload()
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row["pbnttmng"] = "20252"

    with pytest.raises(SourceDataError, match="timing"):
        parse_kindergarten_rows(payload, expected_timing="20261")


def test_kindergarten_region_codes_require_pinned_official_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "regions.csv"
    body = (
        "# source_url=https://e-childschoolinfo.moe.go.kr/openApi/"
        "sidoSigunguCode.do\n"
        "# source_as_of=2026-08-10\n"
        "# source_sha256="
        "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925\n"
        "# normalized_sha256="
        "04e31dd3a83f8d58397ae24aabc894dd17530c5102826f603317a3ae8a3122c5\n"
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Source: Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        "11,11110,Jongno-gu\n"
    )
    path.write_text(body, encoding="utf-8")

    regions = parse_kindergarten_region_codes(path, expected_count=1)

    assert regions == (("11", "11110", "Jongno-gu"),)


def test_kindergarten_region_codes_reject_hash_drift(tmp_path: Path) -> None:
    path = tmp_path / "regions.csv"
    path.write_text(
        "# source_url=https://e-childschoolinfo.moe.go.kr/openApi/"
        "sidoSigunguCode.do\n"
        "# source_as_of=2026-08-10\n"
        "# source_sha256=not-a-hash\n"
        "# normalized_sha256="
        "04e31dd3a83f8d58397ae24aabc894dd17530c5102826f603317a3ae8a3122c5\n"
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Source: Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        "11,11110,Jongno-gu\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceDataError, match="SHA-256"):
        parse_kindergarten_region_codes(path, expected_count=1)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925",
            "f" * 64,
        ),
        ("11,11740,Gangdong-gu", "11,11999,Gangdong-gu"),
    ],
)
def test_kindergarten_region_resource_is_bound_to_reviewed_content(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source_text = (
        SOURCE_RESOURCES / "kindergarten-region-codes.csv"
    ).read_text(encoding="utf-8")
    path = tmp_path / "tampered-regions.csv"
    path.write_text(source_text.replace(old, new), encoding="utf-8")

    with pytest.raises(SourceDataError, match="reviewed|SHA-256|normalized"):
        parse_kindergarten_region_codes(path)


def test_kindergarten_region_codes_must_match_requested_timing(
    tmp_path: Path,
) -> None:
    path = write_region_fixture(tmp_path)

    with pytest.raises(SourceDataError, match="timing"):
        parse_kindergarten_region_codes(
            path,
            expected_timing="20252",
        )


def test_reviewed_sen_resource_matches_official_organization_totals() -> None:
    source = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    )

    result = source.load()

    assert len(result.records) == 41
    assert result.provenance.row_count == 41
    assert all(record.foundation_type == "PUBLIC" for record in result.records)
    assert all(record.source_region_code == "SEOUL" for record in result.records)
    assert all(not hasattr(record, "telephone") for record in result.records)


def test_reviewed_sen_provenance_is_accepted_by_candidate_builder(
    tmp_path: Path,
) -> None:
    result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()

    candidate = build_candidate_snapshot(
        records=result.records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="sen-provenance-contract",
        coverage=TEST_COVERAGE,
        source_provenance={result.provenance.source: result.provenance},
    )

    assert candidate.issues == (
        "coordinate validation success rate is below 98 percent",
    )


def test_reviewed_sen_provenance_rejects_valid_looking_wrong_raw_digest(
    tmp_path: Path,
) -> None:
    result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()
    forged = replace(result.provenance, raw_sha256="f" * 64)

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_candidate_snapshot(
            records=result.records,
            previous=None,
            output_root=tmp_path,
            snapshot_id="sen-wrong-raw-digest",
            coverage=TEST_COVERAGE,
            source_provenance={forged.source: forged},
        )


def test_school_reconciliation_blocks_more_than_one_percent_mismatch() -> None:
    records = tuple(
        source_record(institution_id=f"neis:B10:{index:07d}")
        for index in range(99)
    )

    boundary = reconcile_selectable_school_counts(
        records,
        expected_count=100,
    )
    assert boundary["passed"] is True
    assert boundary["deltaRatio"] == 0.01

    with pytest.raises(SnapshotQualityError, match="reconciliation"):
        reconcile_selectable_school_counts(records[:-1], expected_count=100)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "9f202202edc653b09b4debb5a0ff939cf9fcdc64dd58174b28f8d009bb1b7424",
            "f" * 64,
        ),
        (
            r"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uc1a1\uc6d4\uae38 48",
            r"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \ubcc0\uc870\ub85c 1",
        ),
    ],
)
def test_sen_resource_is_bound_to_reviewed_content(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source_text = (SOURCE_RESOURCES / "sen-institutions.csv").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "tampered-sen.csv"
    path.write_text(source_text.replace(old, new), encoding="utf-8")
    source = SenCsvSource(
        path,
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    )

    with pytest.raises(SourceDataError, match="reviewed|SHA-256|normalized"):
        source.load()


def test_keyless_official_school_csv_only_enriches_matching_neis_identity() -> None:
    csv_bytes = (
        "\ufeff\ud559\uad50ID,\ud559\uad50\uba85,\ud559\uad50\uae09\uad6c\ubd84,"
        "\uc124\ub9bd\uc77c\uc790,\uc124\ub9bd\ud615\ud0dc,\ubcf8\uad50\ubd84\uad50\uad6c\ubd84,"
        "\uc6b4\uc601\uc0c1\ud0dc,\uc18c\uc7ac\uc9c0\uc9c0\ubc88\uc8fc\uc18c,"
        "\uc18c\uc7ac\uc9c0\ub3c4\ub85c\uba85\uc8fc\uc18c,\uc2dc\ub3c4\uad50\uc721\uccad\ucf54\ub4dc,"
        "\uc2dc\ub3c4\uad50\uc721\uccad\uba85,\uad50\uc721\uc9c0\uc6d0\uccad\ucf54\ub4dc,"
        "\uad50\uc721\uc9c0\uc6d0\uccad\uba85,\uc0dd\uc131\uc77c\uc790,\ubcc0\uacbd\uc77c\uc790,"
        "\uc704\ub3c4,\uacbd\ub3c4,\ub370\uc774\ud130\uae30\uc900\uc77c\uc790\n"
        "B100000001,\uac80\uc99d\ud559\uad50,\ucd08\ub4f1\ud559\uad50,20000101,"
        "\uacf5\ub9bd,\ubcf8\uad50,\uc6b4\uc601,\uc11c\uc6b8 \uc911\uad6c,"
        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1,7010000,"
        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad,7011000,"
        "\uc911\ubd80\uad50\uc721\uc9c0\uc6d0\uccad,20260320,20260320,37.56,126.97,"
        "2026-03-20\n"
    ).encode("utf-8")
    locations = parse_standard_school_locations(
        csv_bytes,
        expected_seoul_count=1,
    )
    neis = SourceInstitutionRecord(
        **{
            **source_record(
                institution_id="neis:B10:7010001"
            ).__dict__,
            "latitude": None,
            "longitude": None,
            "coordinate_quality": "MISSING",
        }
    )

    enriched = enrich_neis_coordinates((neis,), locations)

    assert (enriched[0].latitude, enriched[0].longitude) == (37.56, 126.97)
    assert enriched[0].institution_id == "neis:B10:7010001"
    assert enriched[0].source == "NEIS"
    assert enriched[0].coordinate_quality == "OFFICIAL_STANDARD_COORDINATE"


@pytest.mark.asyncio
async def test_neis_source_requires_real_key_and_paginates_to_declared_total() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        page = int(request.url.params["pIndex"])
        section = payload["schoolInfo"]
        assert type(section) is list
        section[0]["head"][0]["list_total_count"] = 2
        row = section[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        result = await source.fetch()

    assert len(result.records) == 2
    assert result.provenance.page_count == 2
    assert [request.url.params["pIndex"] for request in requests] == ["1", "2"]
    assert all(request.url.params["ATPT_OFCDC_SC_CODE"] == "B10" for request in requests)

    with pytest.raises(SourceDataError, match="NEIS_API_KEY"):
        NeisSource(api_key="", client=httpx.AsyncClient())


@pytest.mark.asyncio
async def test_neis_source_rejects_keyless_sample_and_redacts_invalid_key() -> None:
    secret = "never-show-this-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["KEY"] == secret
        return httpx.Response(
            200,
            json={"RESULT": {"CODE": "ERROR-290", "MESSAGE": secret}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_source_http_failure_traceback_does_not_retain_api_key() -> None:
    secret = "traceback-secret-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    formatted = "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )
    assert secret not in formatted
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_unexpected_transport_failure_does_not_retain_api_key() -> None:
    secret = "unexpected-transport-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport exploded")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="NEIS request failed") as raised:
            await NeisSource(api_key=secret, client=client).fetch()

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_kindergarten_http_failure_traceback_does_not_retain_api_key(
    tmp_path: Path,
) -> None:
    secret = "kindergarten-traceback-secret"
    region_path = write_region_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await KindergartenSource(
                api_key=secret,
                client=client,
                region_codes_path=region_path,
                timing="20261",
            ).fetch()

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_kakao_http_failure_traceback_does_not_retain_api_key() -> None:
    secret = "kakao-traceback-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key=secret, client=client)
        with pytest.raises(SourceDataError) as raised:
            await kakao.geocode("서울특별시 종로구 송월길 48")

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_shared_http_boundary_scrubs_secret_parameters_and_headers() -> None:
    secret = "shared-helper-traceback-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError) as raised:
            await get_json_with_retry(
                client=client,
                url="https://example.invalid/source",
                params={"key": secret},
                headers={"Authorization": f"Bearer {secret}"},
                source_label="test source",
            )

    assert_secret_absent_from_app_traceback(raised.value, raised.tb, secret)


@pytest.mark.asyncio
async def test_successful_source_fetches_clear_api_keys(tmp_path: Path) -> None:
    neis_secret = "successful-neis-secret"
    kindergarten_secret = "successful-kindergarten-secret"

    def neis_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=neis_payload(source_type="초등학교"))

    def kindergarten_handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = request.url.params["sggCode"]
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{request.url.params['sggCode']}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(neis_handler)
    ) as client:
        neis = NeisSource(api_key=neis_secret, client=client)
        await neis.fetch()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(kindergarten_handler)
    ) as client:
        kindergarten = KindergartenSource(
            api_key=kindergarten_secret,
            client=client,
            region_codes_path=write_region_fixture(tmp_path),
            timing="20261",
        )
        await kindergarten.fetch()

    assert neis_secret not in repr(neis.__dict__)
    assert kindergarten_secret not in repr(kindergarten.__dict__)


@pytest.mark.asyncio
async def test_neis_pagination_counts_explicitly_excluded_source_rows() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        excluded = dict(first)
        excluded["SD_SCHUL_CODE"] = "7010999"
        excluded["SCHUL_KND_SC_NM"] = "\uacf5\ub3d9\uc2e4\uc2b5\uc18c"
        sections[0]["head"][0]["list_total_count"] = 2
        sections[1]["row"] = [first, excluded]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            page_size=2,
        ).fetch()

    assert len(result.records) == 1
    assert result.provenance.page_count == 1
    assert result.provenance.fetched_row_count == 2
    assert result.provenance.row_count == 1


@pytest.mark.asyncio
async def test_neis_source_rejects_mixed_load_dates_across_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="\ucd08\ub4f1\ud559\uad50")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 2
        row = sections[1]["row"][0]
        row["SD_SCHUL_CODE"] = f"701000{page}"
        row["LOAD_DTM"] = "20260809" if page == 1 else "20260810"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(api_key="test-key", client=client, page_size=1)
        with pytest.raises(SourceDataError, match="source_as_of"):
            await source.fetch()


@pytest.mark.asyncio
async def test_neis_source_rejects_five_row_sample_success_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        sections[0]["head"][0]["list_total_count"] = 5
        sections[1]["row"] = [
            {**first, "SD_SCHUL_CODE": f"701000{index}"}
            for index in range(1, 6)
        ]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="sample"):
            await NeisSource(api_key="test-key", client=client).fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_total", "message"),
    [
        (2_147_483_647, "row ceiling"),
        (201, "page limit"),
    ],
)
async def test_neis_source_bounds_declared_total_before_second_request(
    declared_total: int,
    message: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = declared_total
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match=message):
            await NeisSource(api_key="test-key", client=client, page_size=1).fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_neis_source_rejects_oversized_response_before_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_RESPONSE_BYTES", 100)

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="초등학교")
        payload["padding"] = "x" * 500
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await NeisSource(api_key="test-key", client=client).fetch()


@pytest.mark.asyncio
async def test_neis_response_stream_stops_after_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_RESPONSE_BYTES", 100)
    yielded_chunks = 0

    class CountingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal yielded_chunks
            for _ in range(10):
                yielded_chunks += 1
                yield b"x" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await NeisSource(api_key="test-key", client=client).fetch()

    assert yielded_chunks < 10


@pytest.mark.asyncio
async def test_neis_source_rejects_more_rows_than_requested_page_size() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        first = sections[1]["row"][0]
        sections[0]["head"][0]["list_total_count"] = 2
        sections[1]["row"] = [
            first,
            {**first, "SD_SCHUL_CODE": "7010002"},
        ]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="page size"):
            await NeisSource(api_key="test-key", client=client, page_size=1).fetch()


@pytest.mark.asyncio
async def test_neis_source_bounds_actual_page_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(neis_module, "_MAX_PAGE_COUNT", 2)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["pIndex"])
        payload = neis_payload(source_type="초등학교")
        sections = payload["schoolInfo"]
        assert type(sections) is list
        sections[0]["head"][0]["list_total_count"] = 3
        sections[1]["row"][0]["SD_SCHUL_CODE"] = f"701{page:04d}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="page limit|short page"):
            await NeisSource(
                api_key="test-key",
                client=client,
                page_size=1_000,
            ).fetch()

    assert len(requests) <= 2


@pytest.mark.asyncio
async def test_kindergarten_source_requires_key_and_detects_repeated_page(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["currentPage"] = int(request.url.params["currentPage"])
        payload["pageCnt"] = 1
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
            page_size=1,
        )
        with pytest.raises(SourceDataError, match="repeated page"):
            await source.fetch()

    with pytest.raises(SourceDataError, match="KINDERGARTEN_API_KEY"):
        KindergartenSource(
            api_key="",
            client=httpx.AsyncClient(),
            region_codes_path=region_path,
            timing="20261",
        )


@pytest.mark.asyncio
async def test_kindergarten_source_rejects_mismatched_response_echo(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = "99999"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
        )
        with pytest.raises(SourceDataError, match="response echo"):
            await source.fetch()


@pytest.mark.asyncio
async def test_kindergarten_source_bounds_pagination_without_total(
    tmp_path: Path,
) -> None:
    region_path = write_region_fixture(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        page = int(request.url.params["currentPage"])
        payload["currentPage"] = page
        payload["pageCnt"] = 1
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{page:08d}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=region_path,
            timing="20261",
            page_size=1,
        )
        with pytest.raises(SourceDataError, match="page limit"):
            await source.fetch()


@pytest.mark.asyncio
async def test_kindergarten_source_bounds_cumulative_response_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kindergarten_module, "_MAX_CUMULATIVE_BYTES", 100)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=kindergarten_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = KindergartenSource(
            api_key="test-key",
            client=client,
            region_codes_path=write_region_fixture(tmp_path),
            timing="20261",
        )
        with pytest.raises(SourceDataError, match="cumulative response size"):
            await source.fetch()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_standard_school_source_stops_stream_at_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(standard_school_module, "_MAX_RESPONSE_BYTES", 100)
    yielded_chunks = 0

    class CountingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal yielded_chunks
            for _ in range(10):
                yielded_chunks += 1
                yield b"x" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceDataError, match="response size"):
            await StandardSchoolLocationSource(client=client).fetch()

    assert yielded_chunks < 10


@pytest.mark.asyncio
async def test_kakao_geocoder_bounds_paid_requests_and_cumulative_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kakao_module, "_MAX_REQUEST_COUNT", 1)
    monkeypatch.setattr(kakao_module, "_MAX_CUMULATIVE_BYTES", 10_000)
    address = "서울특별시 종로구 송월길 48"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"documents": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        assert await kakao.geocode(address) is None
        with pytest.raises(SourceDataError, match="request limit"):
            await kakao.geocode(address)

    assert requests == 1


@pytest.mark.asyncio
async def test_kakao_geocoder_does_not_retain_unbounded_raw_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kakao_module, "_MAX_CUMULATIVE_BYTES", 100)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": [], "padding": "x" * 200})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        with pytest.raises(SourceDataError, match="cumulative response size"):
            await kakao.geocode("서울특별시 종로구 송월길 48")

    assert not hasattr(kakao, "_raw_responses")


@pytest.mark.asyncio
async def test_kakao_geocode_accepts_one_exact_road_address_and_redacts_key() -> None:
    secret = "never-show-kakao-key"
    address = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uc1a1\uc6d4\uae38 48"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"KakaoAK {secret}"
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "x": "126.9680",
                        "y": "37.5710",
                        "road_address": {"address_name": address},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key=secret, client=client)
        result = await kakao.geocode(address)
        provenance = kakao.provenance()
        kakao.clear_credentials()

    assert result is not None
    assert result.road_address == address
    assert result.confidence == "EXACT_ROAD_ADDRESS"
    assert provenance.fetched_row_count == 1
    assert provenance.matched_row_count == 1
    assert secret not in repr(provenance)
    assert secret not in repr(kakao.__dict__)

    with pytest.raises(SourceDataError, match="KAKAO_REST_API_KEY"):
        KakaoLocalClient(api_key="", client=httpx.AsyncClient())


@pytest.mark.asyncio
async def test_missing_coordinate_is_filled_only_by_exact_kakao_result() -> None:
    address = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "x": "126.97",
                        "y": "37.56",
                        "road_address": {"address_name": address},
                    }
                ]
            },
        )

    missing = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "latitude": None,
            "longitude": None,
            "coordinate_quality": "MISSING",
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        kakao = KakaoLocalClient(api_key="test-key", client=client)
        records = await geocode_missing_records((missing,), kakao)

    assert (records[0].latitude, records[0].longitude) == (37.56, 126.97)
    assert records[0].coordinate_quality == "GEOCODED"


def test_candidate_requires_seoul_coverage_service(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="CoverageService"):
        build_candidate_snapshot(
            records=(source_record(),),
            previous=None,
            output_root=tmp_path,
            snapshot_id="missing-coverage",
        )


def test_candidate_requires_explicit_source_provenance(tmp_path: Path) -> None:
    with pytest.raises(SnapshotQualityError, match="source provenance is required"):
        build_candidate_snapshot(
            records=(source_record(),),
            previous=None,
            output_root=tmp_path,
            snapshot_id="missing-provenance",
            coverage=TEST_COVERAGE,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"institution_id": "kinder:wrong"}, "namespace"),
        ({"institution_type": "UNKNOWN_SCHOOL"}, "institution type"),
        ({"institution_type": "LIBRARY"}, "institution type"),
        ({"foundation_type": "UNKNOWN"}, "foundation type"),
        ({"coordinate_quality": "GUESSED"}, "coordinate quality"),
    ],
)
def test_candidate_rejects_cross_source_ids_and_unknown_enums(
    tmp_path: Path,
    updates: dict[str, str],
    message: str,
) -> None:
    original = source_record()
    invalid = SourceInstitutionRecord(**{**original.__dict__, **updates})

    with pytest.raises(SnapshotQualityError, match=message):
        build_test_candidate(
            records=(invalid,),
            previous=None,
            output_root=tmp_path,
            snapshot_id="invalid-source-contract",
            coverage=TEST_COVERAGE,
        )


def test_source_record_persists_official_branch_as_second_site(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="gayang",
        site_name="Gay ang branch",
        road_address="서울특별시 강서구 양천로 61",
        district="강서구",
        latitude=37.5701,
        longitude=126.8412,
        coordinate_quality="MANUALLY_VERIFIED",
    )
    record = SourceInstitutionRecord(
        **{**source_record().__dict__, "additional_sites": (branch,)}
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="official-branch",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    assert len(verified.institutions) == 1
    assert {site.site_id for site in verified.sites} == {
        "neis:B10:7010001:main",
        "neis:B10:7010001:gayang",
    }
    assert sum(site.is_default for site in verified.sites) == 1


def test_missing_coordinate_branch_is_persisted_for_review(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="future-branch",
        site_name="Future branch",
        road_address="서울특별시 강서구 검증로 2",
        district="강서구",
        latitude=None,
        longitude=None,
        coordinate_quality="MISSING",
    )
    record = SourceInstitutionRecord(
        **{**source_record().__dict__, "additional_sites": (branch,)}
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="missing-branch",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    branch_site = next(
        site for site in verified.sites if site.site_id.endswith(":future-branch")
    )
    assert branch_site.status.value == "REVIEW_REQUIRED"
    assert branch_site.latitude is None
    assert branch_site.routing_anchor_latitude is None


def test_manifest_persists_cross_source_possible_match_pairs(
    tmp_path: Path,
) -> None:
    neis = source_record()
    kindergarten = SourceInstitutionRecord(
        **{
            **neis.__dict__,
            "institution_id": "kinder:K12345678",
            "institution_type": "KINDERGARTEN",
            "source": "KINDERGARTEN_INFO",
            "source_region_code": "11",
            "source_as_of": "2026-04-01",
        }
    )
    candidate = build_test_candidate(
        records=(neis, kindergarten),
        previous=None,
        output_root=tmp_path,
        snapshot_id="possible-pair",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    assert {item.institution_id for item in verified.institutions} == {
        "neis:B10:7010001",
        "kinder:K12345678",
    }
    assert verified.manifest.possible_match_count == 1
    assert verified.manifest.possible_matches[0].institution_ids == (
        "kinder:K12345678",
        "neis:B10:7010001",
    )


def test_candidate_rejects_mixed_source_dates_before_writing(tmp_path: Path) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **source_record(institution_id="neis:B10:7010002").__dict__,
            "source_as_of": "2026-08-09",
        }
    )

    with pytest.raises(SnapshotQualityError, match="source_as_of"):
        build_test_candidate(
            records=(first, second),
            previous=None,
            output_root=tmp_path,
            snapshot_id="mixed-source-dates",
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / ".mixed-source-dates.candidate").exists()


# Production break caught: replacing an approved pointer after a source loses 40%
# of its active rows.
def test_failed_candidate_does_not_replace_current_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    records = tuple(
        SourceInstitutionRecord(
            institution_id=f"neis:B10:{index:07d}",
            official_name=f"\uac80\uc99d\ud559\uad50{index}",
            institution_type="ELEMENTARY_SCHOOL",
            foundation_type="PUBLIC",
            education_office="\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            road_address=f"\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c {index}",
            district="\uc911\uad6c",
            latitude=37.56,
            longitude=126.97 + index / 100_000,
            source="NEIS",
            source_region_code="B10",
            source_as_of="2026-08-10",
            coordinate_quality="MANUALLY_VERIFIED",
        )
        for index in range(10)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="initial",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, root, coverage=TEST_COVERAGE)
    before = (root / "current.json").read_bytes()
    result = build_test_candidate(
        records=records[:6],
        previous=verify_snapshot(root),
        output_root=root,
        snapshot_id="candidate-with-drop",
        coverage=TEST_COVERAGE,
    )

    assert result.approved is False
    forged_result = replace(result, issues=())
    with pytest.raises(SnapshotQualityError, match="record count drop"):
        promote_snapshot(forged_result, root, coverage=TEST_COVERAGE)
    assert (root / "current.json").read_bytes() == before


def test_existing_current_cannot_be_replaced_when_previous_is_omitted(
    tmp_path: Path,
) -> None:
    records = tuple(
        SourceInstitutionRecord(
            **{
                **source_record(
                    institution_id=f"neis:B10:{index:07d}",
                    road_address=(
                        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c "
                        f"\uac80\uc99d\ub85c {index + 1}"
                    ),
                ).__dict__,
                "longitude": 126.97 + index / 100_000,
            }
        )
        for index in range(10)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="existing-current",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path, coverage=TEST_COVERAGE)
    before = (tmp_path / "current.json").read_bytes()
    omitted = build_test_candidate(
        records=records[:1],
        previous=None,
        output_root=tmp_path,
        snapshot_id="omitted-previous",
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="previous snapshot"):
        promote_snapshot(omitted, tmp_path, coverage=TEST_COVERAGE)
    assert (tmp_path / "current.json").read_bytes() == before


def test_coordinate_gate_uses_only_current_rows_and_stale_sites_are_inactive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    records = tuple(
        SourceInstitutionRecord(
            **{
                **source_record(
                    institution_id=f"neis:B10:{index:07d}",
                    road_address=(
                        "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c "
                        f"\uac80\uc99d\ub85c {index + 1}"
                    ),
                ).__dict__,
                "longitude": 126.97 + index / 100_000,
            }
        )
        for index in range(100)
    )
    initial = build_test_candidate(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="full",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, root, coverage=TEST_COVERAGE)
    current = list(records[:90])
    for index in (88, 89):
        current[index] = SourceInstitutionRecord(
            **{
                **current[index].__dict__,
                "latitude": None,
                "longitude": None,
                "coordinate_quality": "MISSING",
            }
        )

    candidate = build_test_candidate(
        records=tuple(current),
        previous=verify_snapshot(root),
        output_root=root,
        snapshot_id="partial",
        coverage=TEST_COVERAGE,
    )
    site_rows = [
        json.loads(line)
        for line in (candidate.candidate_path / "sites.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert any("coordinate validation" in issue for issue in candidate.issues)
    stale_sites = [
        row
        for row in site_rows
        if int(row["institutionId"].rsplit(":", 1)[-1]) >= 90
    ]
    assert stale_sites
    assert {row["status"] for row in stale_sites} == {"MISSING_FROM_SOURCE"}


def test_preserved_enriched_site_does_not_require_current_enrichment_match(
    tmp_path: Path,
) -> None:
    enriched = replace(
        source_record(),
        coordinate_quality="OFFICIAL_STANDARD_COORDINATE",
    )
    initial = build_test_candidate(
        records=(enriched,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="enriched-before-missing",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    promote_snapshot(initial, tmp_path, coverage=TEST_COVERAGE)
    replacement = source_record(institution_id="neis:B10:7010002")
    candidate = build_test_candidate(
        records=(replacement,),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="enriched-now-missing",
        coverage=TEST_COVERAGE,
    )

    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    old = next(
        institution
        for institution in verified.institutions
        if institution.institution_id == enriched.institution_id
    )
    assert old.status is InstitutionStatus.MISSING_FROM_SOURCE
    assert len(verified.manifest.enrichments) == 1
    assert verified.manifest.enrichments[0].preserved_matched_row_count == 1


def test_missing_official_branch_is_preserved_when_parent_remains(
    tmp_path: Path,
) -> None:
    branch = SourceInstitutionSiteRecord(
        site_code="gayang",
        site_name="Gay ang branch",
        road_address="서울특별시 강서구 양천로 61",
        district="강서구",
        latitude=37.5701,
        longitude=126.8412,
        coordinate_quality="MANUALLY_VERIFIED",
    )
    with_branch = replace(source_record(), additional_sites=(branch,))
    initial = build_test_candidate(
        records=(with_branch,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="branch-before-missing",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path, coverage=TEST_COVERAGE)
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="branch-now-missing",
        coverage=TEST_COVERAGE,
    )

    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    verified = verify_snapshot(tmp_path)

    old_branch = next(
        site for site in verified.sites if site.site_id.endswith(":gayang")
    )
    assert old_branch.status is InstitutionStatus.MISSING_FROM_SOURCE


def test_concurrent_promotions_from_same_previous_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="concurrent-base",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path, coverage=TEST_COVERAGE)
    previous = verify_snapshot(tmp_path)
    first = build_test_candidate(
        records=(source_record(),),
        previous=previous,
        output_root=tmp_path,
        snapshot_id="concurrent-first",
        coverage=TEST_COVERAGE,
    )
    second = build_test_candidate(
        records=(source_record(),),
        previous=previous,
        output_root=tmp_path,
        snapshot_id="concurrent-second",
        coverage=TEST_COVERAGE,
    )
    real_quality = sync_module._recheck_promotion_quality
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def controlled_quality(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return real_quality(*args, **kwargs)

    monkeypatch.setattr(
        sync_module,
        "_recheck_promotion_quality",
        controlled_quality,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            promote_snapshot,
            first,
            tmp_path,
            coverage=TEST_COVERAGE,
        )
        assert first_entered.wait(timeout=2)
        second_future = executor.submit(
            promote_snapshot,
            second,
            tmp_path,
            coverage=TEST_COVERAGE,
        )
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        outcomes = []
        for future in (first_future, second_future):
            try:
                future.result(timeout=3)
                outcomes.append("success")
            except SnapshotQualityError:
                outcomes.append("blocked")

    assert sorted(outcomes) == ["blocked", "success"]


def test_manifest_counts_changed_institution_records(tmp_path: Path) -> None:
    initial = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="before-change",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path, coverage=TEST_COVERAGE)
    original = source_record()
    changed = SourceInstitutionRecord(
        **{**original.__dict__, "official_name": "Changed Official Name"}
    )

    candidate = build_test_candidate(
        records=(changed,),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="after-change",
        coverage=TEST_COVERAGE,
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["diff"]["changedCount"] == 1


def test_address_region_mismatch_is_quarantined(tmp_path: Path) -> None:
    record = source_record(
        institution_id="neis:B10:7010001",
        road_address="\ubd80\uc0b0\uad11\uc5ed\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
    )

    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="address-mismatch",
        coverage=TEST_COVERAGE,
    )

    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["quarantinedCount"] == 1
    assert candidate.approved is False
    assert any("coordinate validation" in issue for issue in candidate.issues)


def test_coordinate_outside_seoul_is_quarantined(tmp_path: Path) -> None:
    coverage = CoverageService.from_geojson(
        seoul_path="apps/travel-map/resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "latitude": 35.1796,
            "longitude": 129.0756,
        }
    )

    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="coordinate-mismatch",
        coverage=coverage,
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["quarantinedCount"] == 1
    assert any("coordinate validation" in issue for issue in candidate.issues)
    forged_candidate = replace(candidate, issues=())
    with pytest.raises(SnapshotQualityError, match="coordinate validation"):
        promote_snapshot(forged_candidate, tmp_path, coverage=TEST_COVERAGE)


def test_namesake_across_sources_is_not_merged(tmp_path: Path) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **first.__dict__,
            "institution_id": "kinder:verified-kindergarten",
            "institution_type": "KINDERGARTEN",
            "source": "KINDERGARTEN_INFO",
            "source_region_code": "11",
            "source_as_of": "2026-04-01",
        }
    )

    candidate = build_test_candidate(
        records=(first, second),
        previous=None,
        output_root=tmp_path,
        snapshot_id="possible-match",
        coverage=TEST_COVERAGE,
    )
    rows = (candidate.candidate_path / "institutions.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert len(rows) == 2
    assert manifest["possibleMatchCount"] == 1


def test_promotion_rechecks_hash_before_pointer_change(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered",
        coverage=TEST_COVERAGE,
    )
    (candidate.candidate_path / "institutions.jsonl").write_text(
        "tampered\n", encoding="utf-8"
    )

    with pytest.raises(SnapshotQualityError, match="hash mismatch"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_replays_coverage_for_persisted_active_site(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="tampered-coverage",
        coverage=TEST_COVERAGE,
    )
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "latitude": 35.1796,
            "longitude": 129.0756,
            "routingAnchorLatitude": 35.1796,
            "routingAnchorLongitude": 129.0756,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="Seoul coverage"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_candidate_cannot_self_approve_before_promotion(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="self-approved",
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["approved"] = True
    manifest["approvedAt"] = "2026-08-10T09:00:00Z"
    manifest["approvedByRole"] = "data-steward"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approved=false"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_rejects_candidate_from_another_snapshot_root(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir()
    external_root = tmp_path / "external"
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=external_root,
        snapshot_id="external-candidate",
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="candidate path"):
        promote_snapshot(candidate, target_root, coverage=TEST_COVERAGE)
    assert candidate.candidate_path.is_dir()
    assert not (target_root / "current.json").exists()


def test_promotion_rejects_candidate_symlink(tmp_path: Path) -> None:
    external = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path / "external",
        snapshot_id="symlinked",
        coverage=TEST_COVERAGE,
    )
    target_root = tmp_path / "target"
    target_root.mkdir()
    candidate_path = target_root / ".symlinked.candidate"
    candidate_path.symlink_to(external.candidate_path, target_is_directory=True)
    forged = replace(
        external,
        snapshot_id="symlinked",
        candidate_path=candidate_path,
        issues=(),
    )

    with pytest.raises(SnapshotQualityError, match="symlink"):
        promote_snapshot(forged, target_root, coverage=TEST_COVERAGE)
    assert not (target_root / "current.json").exists()


@pytest.mark.parametrize(
    "file_name",
    ["manifest.json", "institutions.jsonl", "sites.jsonl"],
)
def test_promotion_rejects_symlinked_candidate_file(
    tmp_path: Path,
    file_name: str,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"symlink-{file_name.split('.')[0]}",
        coverage=TEST_COVERAGE,
    )
    candidate_file = candidate.candidate_path / file_name
    external_file = tmp_path / f"external-{file_name}"
    candidate_file.rename(external_file)
    candidate_file.symlink_to(external_file)

    with pytest.raises(SnapshotQualityError, match="symlink"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_revalidates_safe_snapshot_slug(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="safe-slug",
        coverage=TEST_COVERAGE,
    )
    forged = replace(
        candidate,
        snapshot_id="../escaped-final",
        issues=(),
    )

    with pytest.raises(SnapshotQualityError, match="unsafe"):
        promote_snapshot(forged, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path.parent / "escaped-final").exists()


def test_promotion_recounts_candidate_manifest_before_pointer_change(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="bad-count",
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionCount"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="institutionCount"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_binds_source_digest_to_persisted_site_content(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="site-provenance-binding",
        coverage=TEST_COVERAGE,
    )
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "roadAddress": "서울특별시 송파구 변조로 10",
            "district": "송파구",
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_rejects_replacement_acquisition_provenance(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="replacement-acquisition",
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0].update(
        {
            "rawSha256": "f" * 64,
            "pageCount": 199,
            "fetchedRowCount": 4_999,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_standard_enrichment_binds_selected_site_mapping(
    tmp_path: Path,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="standard-selected-mapping",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    sites_path = candidate.candidate_path / "sites.jsonl"
    site = json.loads(sites_path.read_text(encoding="utf-8"))
    site.update(
        {
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        }
    )
    site_bytes = (json.dumps(site, ensure_ascii=False) + "\n").encode()
    sites_path.write_bytes(site_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered_record = replace(record, latitude=37.51, longitude=127.10)
    manifest["sitesSha256"] = hashlib.sha256(site_bytes).hexdigest()
    manifest["sources"][0]["normalizedSha256"] = normalized_records_sha256(
        [tampered_record]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="provenance"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_runs_task3_strict_checks_before_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="strict-before-pointer",
        coverage=TEST_COVERAGE,
    )
    institutions_path = candidate.candidate_path / "institutions.jsonl"
    institution = json.loads(institutions_path.read_text(encoding="utf-8"))
    institution["lastSeenSnapshot"] = "other-snapshot"
    institution_bytes = (
        json.dumps(institution, ensure_ascii=False) + "\n"
    ).encode()
    institutions_path.write_bytes(institution_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="strict snapshot verification"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/source"),
        ("requestRegionCode", "NOT-B10"),
        ("pageCount", 0),
        ("fetchedRowCount", 0),
        ("normalizedSha256", "f" * 64),
    ],
)
def test_promotion_replays_source_provenance_from_persisted_rows(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"promotion-source-{field_name}",
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0][field_name] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_promotion_replays_enrichment_provenance_from_persisted_rows(
    tmp_path: Path,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="promotion-enrichment",
        coverage=TEST_COVERAGE,
        enrichment_provenance=(
            standard_enrichment_provenance(matched_row_count=1),
        ),
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["enrichments"][0]["normalizedSha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="enrichment provenance"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_manifest_replays_live_source_provenance(tmp_path: Path) -> None:
    official_record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    provenance = SourceProvenance(
        source="NEIS",
        endpoint="https://open.neis.go.kr/hub/schoolInfo",
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Ministry of Education NEIS education data",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-08-10",
        raw_sha256="b" * 64,
        page_count=2,
        row_count=1,
        fetched_row_count=2,
        request_region_code="B10",
        request_timing=None,
        normalized_sha256=normalized_records_sha256(
            [_before_enrichment(official_record)]
        ),
    )
    enrichment = EnrichmentProvenance(
        source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
        endpoint=standard_school_module.DOWNLOAD_URL,
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Korea Education Facilities Safety Authority",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-03-20",
        raw_sha256=standard_school_module.PINNED_SHA256,
        normalized_sha256="ebb2643be10bda983ca9cb81a7ce2820474a53c2f65fc3ac6a7bcc179527cb4a",
        request_region_code="7010000",
        request_timing=None,
        page_count=1,
        fetched_row_count=12_011,
        matched_row_count=1,
        matched_normalized_sha256=enrichment_records_sha256(
            (official_record,),
            "OFFICIAL_STANDARD_COORDINATE",
        ),
    )

    candidate = build_test_candidate(
        records=(official_record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="provenance",
        coverage=TEST_COVERAGE,
        source_provenance={"NEIS": provenance},
        enrichment_provenance=(enrichment,),
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["sources"][0]["rawSha256"] == "b" * 64
    assert manifest["sources"][0]["pageCount"] == 2
    assert manifest["sources"][0]["fetchedAt"] == "2026-08-10T09:00:00Z"
    assert manifest["sources"][0]["fetchedRowCount"] == 2
    assert manifest["sources"][0]["normalizedRowCount"] == 1
    assert manifest["sources"][0]["preservedRowCount"] == 0
    assert manifest["sources"][0]["requestRegionCode"] == "B10"
    assert (
        manifest["enrichments"][0]["rawSha256"]
        == standard_school_module.PINNED_SHA256
    )
    assert manifest["enrichments"][0]["matchedRowCount"] == 1


def test_candidate_requires_matching_coordinate_enrichment(
    tmp_path: Path,
) -> None:
    for quality, message in (
        ("OFFICIAL_STANDARD_COORDINATE", "official school-location"),
        ("GEOCODED", "Kakao"),
    ):
        record = SourceInstitutionRecord(
            **{**source_record().__dict__, "coordinate_quality": quality}
        )
        with pytest.raises(SnapshotQualityError, match=message):
            build_test_candidate(
                records=(record,),
                previous=None,
                output_root=tmp_path / quality,
                snapshot_id=f"missing-{quality.lower()}",
                coverage=TEST_COVERAGE,
            )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/locations.csv"),
        ("request_region_code", "NOT-SEOUL"),
        ("page_count", 0),
        ("fetched_row_count", 0),
        ("matched_row_count", 0),
        ("raw_sha256", "c" * 64),
        ("normalized_sha256", "d" * 64),
    ],
)
def test_candidate_rejects_untrusted_standard_enrichment(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    record = SourceInstitutionRecord(
        **{
            **source_record().__dict__,
            "coordinate_quality": "OFFICIAL_STANDARD_COORDINATE",
        }
    )
    valid = standard_enrichment_provenance(matched_row_count=1)
    invalid = EnrichmentProvenance(
        **{**valid.__dict__, field_name: value}
    )

    with pytest.raises(SnapshotQualityError, match="enrichment"):
        build_test_candidate(
            records=(record,),
            previous=None,
            output_root=tmp_path,
            snapshot_id=f"invalid-enrichment-{field_name}",
            coverage=TEST_COVERAGE,
            enrichment_provenance=(invalid,),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("endpoint", "https://attacker.invalid/source"),
        ("license_name", "UNVERIFIED"),
        ("attribution", "attacker"),
        ("request_region_code", "NOT-B10"),
        ("request_timing", "20261"),
        ("page_count", 0),
        ("page_count", 2),
        ("fetched_row_count", 0),
        ("row_count", 2),
        ("source_as_of", "2026-08-09"),
        ("normalized_sha256", "b" * 64),
        ("raw_sha256", "not-a-sha256"),
    ],
)
def test_candidate_rejects_untrusted_source_provenance(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    records = (source_record(),)
    valid = source_provenance_for(records)["NEIS"]
    invalid = SourceProvenance(**{**valid.__dict__, field_name: value})

    with pytest.raises(SnapshotQualityError, match="source provenance"):
        build_test_candidate(
            records=records,
            previous=None,
            output_root=tmp_path,
            snapshot_id=f"invalid-provenance-{field_name}",
            coverage=TEST_COVERAGE,
            source_provenance={"NEIS": invalid},
        )


def test_pointer_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="recoverable",
        coverage=TEST_COVERAGE,
    )
    real_replace = os.replace

    def fail_pointer_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer_once)
    with pytest.raises(OSError, match="simulated pointer failure"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()
    assert (tmp_path / "recoverable").is_dir()

    monkeypatch.setattr(os, "replace", real_replace)
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "recoverable"


def test_manifest_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="manifest-recovery",
        coverage=TEST_COVERAGE,
    )
    real_replace = os.replace

    def fail_manifest_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "manifest.json":
            raise OSError("simulated manifest replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest_once)
    with pytest.raises(OSError, match="manifest replacement failure"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()
    assert (tmp_path / "manifest-recovery").is_dir()

    monkeypatch.setattr(os, "replace", real_replace)
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "manifest-recovery"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("approvedAt", None), ("approvedByRole", "personal-account")],
)
def test_pointer_retry_validates_real_approved_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"retry-{field_name}",
        coverage=TEST_COVERAGE,
    )
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    monkeypatch.setattr(os, "replace", real_replace)
    manifest_path = tmp_path / candidate.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field_name] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approved manifest"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_pointer_retry_rejects_duplicate_approval_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="retry-duplicate-key",
        coverage=TEST_COVERAGE,
    )
    real_replace = os.replace

    def fail_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "current.json":
            raise OSError("simulated pointer failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer failure"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    monkeypatch.setattr(os, "replace", real_replace)
    manifest_path = tmp_path / candidate.snapshot_id / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace(
            '"approvedAt":',
            '"approvedAt":null,"approvedAt":',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError, match="duplicate JSON key"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_successful_promotion_retry_is_idempotent(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="already-current-retry",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    first_pointer = (tmp_path / "current.json").read_bytes()

    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)

    assert (tmp_path / "current.json").read_bytes() == first_pointer
    assert verify_snapshot(tmp_path).manifest.snapshot_id == candidate.snapshot_id


def test_promotion_rejects_duplicate_jsonl_key_before_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="duplicate-jsonl-key",
        coverage=TEST_COVERAGE,
    )
    institutions_path = candidate.candidate_path / "institutions.jsonl"
    line = institutions_path.read_text(encoding="utf-8")
    tampered = line.replace(
        "{",
        '{"institutionId":"../unsafe",',
        1,
    ).encode("utf-8")
    institutions_path.write_bytes(tampered)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(tampered).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="duplicate JSON key"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_candidate_directory_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="rename-recovery",
        coverage=TEST_COVERAGE,
    )
    real_replace = os.replace

    def fail_directory_once(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "rename-recovery":
            raise OSError("simulated directory replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_directory_once)
    with pytest.raises(OSError, match="directory replacement failure"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["approved"] is False
    assert not (tmp_path / "current.json").exists()

    monkeypatch.setattr(os, "replace", real_replace)
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "rename-recovery"


def test_sync_cli_fails_closed_without_credentials(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "apps/travel-map",
        "NEIS_API_KEY": "must-not-appear",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "apps/travel-map/scripts/sync-institutions.py",
            "--sen-csv",
            str(SOURCE_RESOURCES / "sen-institutions.csv"),
            "--region-codes",
            str(SOURCE_RESOURCES / "kindergarten-region-codes.csv"),
            "--snapshot-root",
            str(snapshot_root),
            "--geodata-root",
            "apps/travel-map/resources/geodata",
            "--timing",
            "20261",
        ],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "KINDERGARTEN_API_KEY" in completed.stderr
    assert "KAKAO_REST_API_KEY" in completed.stderr
    assert "must-not-appear" not in completed.stdout + completed.stderr
    assert not (snapshot_root / "current.json").exists()


def neis_payload(*, source_type: str) -> dict[str, object]:
    payload = copy.deepcopy(load_json("neis-school-info.json"))
    section = payload["schoolInfo"]
    assert type(section) is list
    section[0]["head"][0]["list_total_count"] = 1
    row = section[1]["row"][0]
    section[1]["row"] = [row]
    row.update(
        {
            "ATPT_OFCDC_SC_CODE": "B10",
            "ATPT_OFCDC_SC_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "SCHUL_NM": "\uac80\uc99d\ud559\uad50",
            "SCHUL_KND_SC_NM": source_type,
            "LCTN_SC_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc",
            "JU_ORG_NM": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "FOND_SC_NM": "\uacf5\ub9bd",
            "ORG_RDNMA": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
            "LOAD_DTM": "20260810",
        }
    )
    return payload


def kindergarten_payload() -> dict[str, object]:
    payload = copy.deepcopy(load_json("kindergarten-info.json"))
    row = payload["kinderInfo"][0]  # type: ignore[index]
    row.update(
        {
            "officeedu": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
            "subofficeedu": "\uc911\ubd80\uad50\uc721\uc9c0\uc6d0\uccad",
            "kindername": "\uac80\uc99d\uc720\uce58\uc6d0",
            "establish": "\uacf5\ub9bd(\ubcd1\uc124)",
            "addr": "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc885\ub85c\uad6c \uac80\uc99d\ub85c 3",
        }
    )
    return payload


def write_region_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "regions.csv"
    path.write_text(
        (SOURCE_RESOURCES / "kindergarten-region-codes.csv").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return path


def build_test_candidate(
    *,
    records: tuple[SourceInstitutionRecord, ...],
    previous: VerifiedSnapshot | None,
    output_root: Path,
    snapshot_id: str,
    coverage: CoverageService | None = TEST_COVERAGE,
    source_provenance: Mapping[str, SourceProvenance] | None = None,
    enrichment_provenance: tuple[EnrichmentProvenance, ...] = (),
) -> SnapshotBuildResult:
    selected_provenance = (
        source_provenance
        if source_provenance is not None
        else source_provenance_for(records)
    )
    return build_candidate_snapshot(
        records=records,
        previous=previous,
        output_root=output_root,
        snapshot_id=snapshot_id,
        coverage=coverage,
        source_provenance=selected_provenance,
        enrichment_provenance=enrichment_provenance,
    )


def source_provenance_for(
    records: tuple[SourceInstitutionRecord, ...],
) -> dict[str, SourceProvenance]:
    endpoints = {
        "NEIS": "https://open.neis.go.kr/hub/schoolInfo",
        "KINDERGARTEN_INFO": (
            "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
        ),
        "SEN_REVIEWED_CSV": "https://www.sen.go.kr/www/website.jsp",
    }
    licenses = {
        "NEIS": "PUBLIC_DATA_NO_USE_RESTRICTION",
        "KINDERGARTEN_INFO": "PUBLIC_DATA_PORTAL_TERMS",
        "SEN_REVIEWED_CSV": "KOGL_TYPE_1_ATTRIBUTION",
    }
    attributions = {
        "NEIS": "Ministry of Education NEIS education data",
        "KINDERGARTEN_INFO": "Ministry of Education Kindergarten Info",
        "SEN_REVIEWED_CSV": (
            "Source: Seoul Metropolitan Office of Education "
            "(organization directory and 2026 civil-service handbook)"
        ),
    }
    regions = {
        "NEIS": "B10",
        "KINDERGARTEN_INFO": "11",
        "SEN_REVIEWED_CSV": "SEOUL",
    }
    grouped: dict[str, list[SourceInstitutionRecord]] = {}
    for record in records:
        grouped.setdefault(record.source, []).append(record)
    return {
        source: SourceProvenance(
            source=source,
            endpoint=endpoints[source],
            license_name=licenses[source],
            attribution=attributions[source],
            fetched_at="2026-08-10T09:00:00Z",
            source_as_of=max(record.source_as_of for record in source_records),
            raw_sha256=(
                "9f202202edc653b09b4debb5a0ff939cf9fcdc64dd58174b28f8d009bb1b7424"
                if source == "SEN_REVIEWED_CSV"
                else "a" * 64
            ),
            page_count=(25 if source == "KINDERGARTEN_INFO" else 1),
            row_count=len(source_records),
            fetched_row_count=len(source_records),
            request_region_code=regions[source],
            request_timing=(
                "20261" if source == "KINDERGARTEN_INFO" else None
            ),
            normalized_sha256=normalized_records_sha256(
                [_before_enrichment(record) for record in source_records]
            ),
        )
        for source, source_records in grouped.items()
    }


def standard_enrichment_provenance(
    *,
    matched_row_count: int,
) -> EnrichmentProvenance:
    official_record = replace(
        source_record(),
        coordinate_quality="OFFICIAL_STANDARD_COORDINATE",
    )
    return EnrichmentProvenance(
        source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
        endpoint=standard_school_module.DOWNLOAD_URL,
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Korea Education Facilities Safety Authority",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of=standard_school_module.PINNED_SOURCE_AS_OF,
        raw_sha256=standard_school_module.PINNED_SHA256,
        normalized_sha256=(
            "ebb2643be10bda983ca9cb81a7ce2820474a53c2f65fc3ac6a7bcc179527cb4a"
        ),
        request_region_code="7010000",
        request_timing=None,
        page_count=1,
        fetched_row_count=standard_school_module.PINNED_NATIONWIDE_COUNT,
        matched_row_count=matched_row_count,
        matched_normalized_sha256=enrichment_records_sha256(
            (official_record,),
            "OFFICIAL_STANDARD_COORDINATE",
        ),
    )


def _before_enrichment(
    record: SourceInstitutionRecord,
) -> SourceInstitutionRecord:
    if record.coordinate_quality in {
        "OFFICIAL_STANDARD_COORDINATE",
        "GEOCODED",
    }:
        record = replace(
            record,
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        )
    return replace(
        record,
        additional_sites=tuple(
            replace(
                site,
                latitude=None,
                longitude=None,
                coordinate_quality="MISSING",
            )
            if site.coordinate_quality in {
                "OFFICIAL_STANDARD_COORDINATE",
                "GEOCODED",
            }
            else site
            for site in record.additional_sites
        ),
    )


def source_record(
    *,
    institution_id: str = "neis:B10:7010001",
    road_address: str = "\uc11c\uc6b8\ud2b9\ubcc4\uc2dc \uc911\uad6c \uac80\uc99d\ub85c 1",
) -> SourceInstitutionRecord:
    return SourceInstitutionRecord(
        institution_id=institution_id,
        official_name="\uac80\uc99d\ud559\uad50",
        institution_type="ELEMENTARY_SCHOOL",
        foundation_type="PUBLIC",
        education_office="\uc11c\uc6b8\ud2b9\ubcc4\uc2dc\uad50\uc721\uccad",
        road_address=road_address,
        district="\uc911\uad6c",
        latitude=37.56,
        longitude=126.97,
        source="NEIS",
        source_region_code="B10",
        source_as_of="2026-08-10",
        coordinate_quality="MANUALLY_VERIFIED",
    )
