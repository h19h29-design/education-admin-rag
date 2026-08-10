import copy
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import httpx
import pytest
from app.institutions.snapshot import verify_snapshot
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceInstitutionRecord,
    SourceProvenance,
)
from app.institutions.sources.kindergarten import (
    KindergartenSource,
    parse_kindergarten_region_codes,
    parse_kindergarten_rows,
)
from app.institutions.sources.neis import NeisSource, parse_neis_rows
from app.institutions.sources.sen import SenCsvSource, parse_sen_csv
from app.institutions.sources.standard_school import (
    enrich_neis_coordinates,
    parse_standard_school_locations,
)
from app.institutions.sync import (
    SnapshotBuildResult,
    SnapshotQualityError,
    build_candidate_snapshot,
    geocode_missing_records,
    promote_snapshot,
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
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Ministry of Education Kindergarten Info\n"
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
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        "11,11110,Jongno-gu\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceDataError, match="SHA-256"):
        parse_kindergarten_region_codes(path, expected_count=1)


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

    assert result is not None
    assert result.road_address == address
    assert result.confidence == "EXACT_ROAD_ADDRESS"
    assert provenance.fetched_row_count == 1
    assert provenance.matched_row_count == 1
    assert secret not in repr(provenance)

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
        build_candidate_snapshot(
            records=(invalid,),
            previous=None,
            output_root=tmp_path,
            snapshot_id="invalid-source-contract",
            coverage=TEST_COVERAGE,
        )


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
            coordinate_quality="GEOCODED",
        )
        for index in range(10)
    )
    initial = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="initial",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, root)
    before = (root / "current.json").read_bytes()
    result = build_candidate_snapshot(
        records=records[:6],
        previous=verify_snapshot(root),
        output_root=root,
        snapshot_id="candidate-with-drop",
        coverage=TEST_COVERAGE,
    )

    assert result.approved is False
    forged_result = SnapshotBuildResult(
        snapshot_id=result.snapshot_id,
        candidate_path=result.candidate_path,
        approved=False,
        issues=(),
    )
    with pytest.raises(SnapshotQualityError, match="record count drop"):
        promote_snapshot(forged_result, root)
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
    initial = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="existing-current",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path)
    before = (tmp_path / "current.json").read_bytes()
    omitted = build_candidate_snapshot(
        records=records[:1],
        previous=None,
        output_root=tmp_path,
        snapshot_id="omitted-previous",
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="previous snapshot"):
        promote_snapshot(omitted, tmp_path)
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
    initial = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=root,
        snapshot_id="full",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, root)
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

    candidate = build_candidate_snapshot(
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


def test_manifest_counts_changed_institution_records(tmp_path: Path) -> None:
    initial = build_candidate_snapshot(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="before-change",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(initial, tmp_path)
    original = source_record()
    changed = SourceInstitutionRecord(
        **{**original.__dict__, "official_name": "Changed Official Name"}
    )

    candidate = build_candidate_snapshot(
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

    candidate = build_candidate_snapshot(
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

    candidate = build_candidate_snapshot(
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
    forged_candidate = SnapshotBuildResult(
        snapshot_id=candidate.snapshot_id,
        candidate_path=candidate.candidate_path,
        approved=False,
        issues=(),
    )
    with pytest.raises(SnapshotQualityError, match="coordinate validation"):
        promote_snapshot(forged_candidate, tmp_path)


def test_namesake_across_sources_is_not_merged(tmp_path: Path) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **first.__dict__,
            "institution_id": "sen:verified-office",
            "institution_type": "DIRECT_AGENCY",
            "source": "SEN_REVIEWED_CSV",
            "source_region_code": "SEOUL",
        }
    )

    candidate = build_candidate_snapshot(
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
    candidate = build_candidate_snapshot(
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
        promote_snapshot(candidate, tmp_path)
    assert not (tmp_path / "current.json").exists()


def test_candidate_cannot_self_approve_before_promotion(tmp_path: Path) -> None:
    candidate = build_candidate_snapshot(
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
        promote_snapshot(candidate, tmp_path)
    assert not (tmp_path / "current.json").exists()


def test_promotion_recounts_candidate_manifest_before_pointer_change(
    tmp_path: Path,
) -> None:
    candidate = build_candidate_snapshot(
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
        promote_snapshot(candidate, tmp_path)
    assert not (tmp_path / "current.json").exists()


def test_manifest_replays_live_source_provenance(tmp_path: Path) -> None:
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
    )
    enrichment = EnrichmentProvenance(
        source="OFFICIAL_STANDARD_SCHOOL_LOCATION",
        endpoint="https://www.data.go.kr/data/15021148/standard.do",
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Korea Education Facilities Safety Authority",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-03-20",
        raw_sha256="c" * 64,
        normalized_sha256="d" * 64,
        request_region_code="7010000",
        request_timing=None,
        page_count=1,
        fetched_row_count=12_011,
        matched_row_count=1,
    )

    candidate = build_candidate_snapshot(
        records=(source_record(),),
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
    assert manifest["enrichments"][0]["rawSha256"] == "c" * 64
    assert manifest["enrichments"][0]["matchedRowCount"] == 1


def test_pointer_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_candidate_snapshot(
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
        promote_snapshot(candidate, tmp_path)
    assert not (tmp_path / "current.json").exists()
    assert (tmp_path / "recoverable").is_dir()

    monkeypatch.setattr(os, "replace", real_replace)
    promote_snapshot(candidate, tmp_path)
    assert verify_snapshot(tmp_path).manifest.snapshot_id == "recoverable"


def test_candidate_directory_replace_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_candidate_snapshot(
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
        promote_snapshot(candidate, tmp_path)
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["approved"] is False
    assert not (tmp_path / "current.json").exists()

    monkeypatch.setattr(os, "replace", real_replace)
    promote_snapshot(candidate, tmp_path)
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
    region_rows = "\n".join(
        f"11,{code},District-{index}"
        for index, code in enumerate(
            (
                "11110",
                "11140",
                "11170",
                "11200",
                "11215",
                "11230",
                "11260",
                "11290",
                "11305",
                "11320",
                "11350",
                "11380",
                "11410",
                "11440",
                "11470",
                "11500",
                "11530",
                "11545",
                "11560",
                "11590",
                "11620",
                "11650",
                "11680",
                "11710",
                "11740",
            ),
            start=1,
        )
    )
    path.write_text(
        "# source_url=https://e-childschoolinfo.moe.go.kr/openApi/"
        "sidoSigunguCode.do\n"
        "# source_as_of=2026-08-10\n"
        "# source_sha256="
        "94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925\n"
        "# timing=20261\n"
        "# license_name=PUBLIC_DATA_PORTAL_TERMS\n"
        "# attribution=Ministry of Education Kindergarten Info\n"
        "sido_code,sgg_code,district\n"
        f"{region_rows}\n",
        encoding="utf-8",
    )
    return path


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
        coordinate_quality="GEOCODED",
    )
