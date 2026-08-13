import unicodedata
from pathlib import Path
from typing import Any

import pytest
from app.institutions.sources.common import (
    SourceInstitutionRecord,
    SourceProvenance,
    normalized_records_sha256,
    observation_date_counts,
)
from app.institutions.sources.neis_classification import (
    load_neis_unclassified_policy,
)
from app.institutions.store import InstitutionStore, UnknownSiteError
from app.institutions.sync import (
    approve_candidate_snapshot,
    build_candidate_review_packet,
    build_candidate_snapshot,
)
from app.policy.coverage import CoverageService

SNAPSHOT_ROOT = Path("apps/travel-map/tests/fixtures/institutions/snapshot")
_POLICY_PATH = Path(
    "apps/travel-map/resources/institution-sources/"
    "neis-unclassified-school-kinds.csv"
)


def load_store_with_verified_unclassified_school(tmp_path: Path) -> InstitutionStore:
    policy = load_neis_unclassified_policy(_POLICY_PATH)
    active_record = SourceInstitutionRecord(
        institution_id="neis:B10:public-school",
        official_name="공개학교",
        institution_type="ELEMENTARY_SCHOOL",
        foundation_type="PUBLIC",
        education_office="서울특별시교육청",
        road_address="서울특별시 중구 공개로 1",
        district="중구",
        latitude=37.56,
        longitude=126.98,
        source="NEIS",
        source_region_code="B10",
        source_as_of="2026-08-10",
        coordinate_quality="MANUALLY_VERIFIED",
    )
    unclassified_records = tuple(
        SourceInstitutionRecord(
            institution_id=f"neis:B10:quarantine-{index:02d}",
            official_name=f"공개 제외 평생학교 {index:02d}",
            institution_type="UNCLASSIFIED_SCHOOL",
            foundation_type="PUBLIC",
            education_office="서울특별시교육청",
            road_address="서울특별시 중구 검토로 1",
            district="중구",
            latitude=37.56,
            longitude=126.98,
            source="NEIS",
            source_region_code="B10",
            source_as_of="2026-08-10",
            coordinate_quality="MANUALLY_VERIFIED",
            source_kind_label=label,
        )
        for index, label in enumerate(
            (
                label
                for label, count in policy.counts
                for _ in range(count)
            ),
            start=1,
        )
    )
    records = (active_record, *unclassified_records)
    dates = observation_date_counts(record.source_as_of for record in records)
    provenance = SourceProvenance(
        source="NEIS",
        endpoint="https://open.neis.go.kr/hub/schoolInfo",
        license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
        attribution="Ministry of Education NEIS education data",
        fetched_at="2026-08-13T09:00:00Z",
        source_as_of="2026-08-10",
        source_observation_date_counts=dates,
        normalized_observation_date_counts=dates,
        raw_sha256="a" * 64,
        page_count=1,
        row_count=len(records),
        fetched_row_count=len(records),
        request_region_code="B10",
        normalized_sha256=normalized_records_sha256(records),
        unclassified_school_kind_counts=policy.counts,
        unclassified_school_policy_sha256=policy.sha256,
    )
    coverage = CoverageService.from_geojson(
        seoul_path="apps/travel-map/tests/fixtures/geodata/seoul-square.geojson",
        buffer_distance_m=12_000,
    )
    snapshot_root = tmp_path / "verified-unclassified"
    candidate = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=snapshot_root,
        snapshot_id="verified-unclassified",
        coverage=coverage,
        source_provenance={"NEIS": provenance},
    )
    review = build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=snapshot_root,
        coverage=coverage,
    )
    approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=str(review["reviewDigest"]),
        reviewer_role="data-steward",
        snapshot_root=snapshot_root,
        coverage=coverage,
    )
    return InstitutionStore.load(snapshot_root)


# Production break caught: collapsing two institutions because they share an address.
def test_search_keeps_co_located_school_and_kindergarten_separate() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="샘물", limit=20)

    assert [
        (item.institution_type, item.official_name, item.site_id) for item in results
    ] == [
        (
            "KINDERGARTEN",
            "샘물초등학교병설유치원",
            "test-neis:B10:SEMWATER-KG:main",
        ),
        (
            "ELEMENTARY_SCHOOL",
            "샘물초등학교",
            "test-neis:B10:SEMWATER-ES:main",
        ),
    ]


# Production break caught: merging same-name institutions in different districts.
def test_search_keeps_same_official_name_in_two_districts_separate() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="한빛학교", limit=20)

    assert [(item.district, item.institution_id) for item in results] == [
        ("은평구", "test-neis:B10:HANBIT-EUNPYEONG"),
        ("강남구", "test-neis:B10:HANBIT-GANGNAM"),
    ]
    assert [item.district for item in store.search(query="한빛", district="강남구")] == [
        "강남구"
    ]


# Production break caught: returning one origin for a multi-site institution or
# omitting the physical-site label that lets the user distinguish the origins.
def test_search_returns_each_physical_site_with_its_site_name() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="새봄학교", limit=20)

    assert [(item.site_id, item.site_name) for item in results] == [
        ("test-neis:B10:SAEBOM:branch", "분교장"),
        ("test-neis:B10:SAEBOM:main", "본교"),
    ]


# Production break caught: exposing a closed or review-required origin to routing.
@pytest.mark.parametrize(
    "site_id",
    [
        "test-neis:B10:CLOSED:main",
        "test-neis:B10:MISSING:main",
        "test-neis:B10:TEMP-PARENT:main",
        "test-neis:B10:REVIEW-PARENT:main",
        "test-neis:B10:NONACTIVE-SITES:temporary",
        "test-neis:B10:NONACTIVE-SITES:review",
        "unknown:site",
    ],
)
def test_inactive_or_unknown_site_cannot_be_route_origin(site_id: str) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(
        UnknownSiteError,
        match=f"unknown or inactive institution site: {site_id}",
    ):
        store.require_site(site_id)


# Production break caught: showing an inactive site or a site whose parent is inactive.
def test_search_lists_only_active_sites_of_active_institutions() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="", limit=50)

    assert {item.site_id for item in results} == {
        "test-neis:B10:SEMWATER-KG:main",
        "test-neis:B10:SEMWATER-ES:main",
        "test-neis:B10:HANBIT-GANGNAM:main",
        "test-neis:B10:HANBIT-EUNPYEONG:main",
        "test-neis:B10:SAEBOM:main",
        "test-neis:B10:SAEBOM:branch",
    }
    assert store.search(query="폐교", limit=20) == ()
    assert store.search(query="누락", limit=20) == ()
    assert store.search(query="휴교", limit=20) == ()
    assert store.search(query="검토", limit=20) == ()
    assert store.search(query="비활성", limit=20) == ()


# Production break caught: allowing a review-required school with valid Seoul
# coordinates to re-enter the public store index by name or direct site lookup.
def test_review_required_school_is_excluded_from_every_public_store_boundary() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    quarantined_site_id = "test-neis:B10:REVIEW-PARENT:main"

    assert store.search(query="검토학교", limit=20) == ()
    assert all(
        item.site_id != quarantined_site_id
        for item in store.search(query="", institution_type="ELEMENTARY_SCHOOL")
    )
    with pytest.raises(UnknownSiteError, match="unknown or inactive institution site"):
        store.require_site(quarantined_site_id)


# The quarantine type must remain excluded even when its rows form a valid,
# signed NEIS snapshot with complete policy provenance and valid coordinates.
def test_verified_unclassified_school_is_excluded_from_every_public_store_boundary(
    tmp_path: Path,
) -> None:
    store = load_store_with_verified_unclassified_school(tmp_path)
    quarantined_site_id = "neis:B10:quarantine-01:main"

    assert store.search(query="공개 제외", limit=20) == ()
    assert (
        store.search(
            query="",
            institution_type="UNCLASSIFIED_SCHOOL",
            limit=20,
        )
        == ()
    )
    with pytest.raises(UnknownSiteError, match="unknown or inactive institution site"):
        store.require_site(quarantined_site_id)


# Production break caught: treating canonically equivalent Hangul, whitespace, or
# parentheses as different search text.
def test_search_normalizes_unicode_nfc_whitespace_and_parentheses() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    decomposed_query = unicodedata.normalize("NFD", " ( 새 봄 학교 ) ")

    results = store.search(query=decomposed_query, limit=20)

    assert [item.site_id for item in results] == [
        "test-neis:B10:SAEBOM:branch",
        "test-neis:B10:SAEBOM:main",
    ]


# Production break caught: indexing only the official name and dropping approved aliases.
def test_search_indexes_aliases_without_merging_institutions() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="샘물부설유치원", limit=20)

    assert [item.institution_id for item in results] == [
        "test-neis:B10:SEMWATER-KG"
    ]


# Production break caught: failing Korean initial-consonant lookup.
def test_search_indexes_korean_initial_consonants() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="ㅎㅂㅎㄱ", limit=20)

    assert [(item.official_name, item.district) for item in results] == [
        ("한빛학교", "은평구"),
        ("한빛학교", "강남구"),
    ]


# Production break caught: fuzzy matching a typo into a server-approved origin.
def test_search_does_not_fuzzy_auto_match() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    assert store.search(query="샘믈", limit=20) == ()


# Production break caught: making exact site ID lookup depend on name tokenization.
def test_search_matches_exact_site_id() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="test-neis:B10:SAEBOM:branch", limit=20)

    assert [(item.site_id, item.site_name) for item in results] == [
        ("test-neis:B10:SAEBOM:branch", "분교장")
    ]


# Production break caught: returning a different ordering across repeated empty queries.
def test_empty_search_is_deterministic_by_name_then_site_id() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)
    expected_site_ids = [
        "test-neis:B10:SAEBOM:branch",
        "test-neis:B10:SAEBOM:main",
        "test-neis:B10:SEMWATER-ES:main",
        "test-neis:B10:SEMWATER-KG:main",
        "test-neis:B10:HANBIT-EUNPYEONG:main",
        "test-neis:B10:HANBIT-GANGNAM:main",
    ]

    assert [item.site_id for item in store.search(query="", limit=50)] == (
        expected_site_ids
    )
    assert [item.site_id for item in store.search(query="", limit=50)] == (
        expected_site_ids
    )


# Production break caught: ignoring one of the supported exact metadata filters.
@pytest.mark.parametrize(
    ("filters", "expected_site_ids"),
    [
        (
            {"institution_type": "KINDERGARTEN"},
            ["test-neis:B10:SEMWATER-KG:main"],
        ),
        (
            {"foundation_type": "PUBLIC", "district": "성동구"},
            ["test-neis:B10:SAEBOM:branch"],
        ),
        (
            {"education_office": "강남서초교육지원청"},
            ["test-neis:B10:HANBIT-GANGNAM:main"],
        ),
        (
            {"district": "샘물구", "institution_type": "ELEMENTARY_SCHOOL"},
            ["test-neis:B10:SEMWATER-ES:main"],
        ),
    ],
)
def test_search_applies_supported_filters(
    filters: dict[str, Any],
    expected_site_ids: list[str],
) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    results = store.search(query="", limit=50, **filters)

    assert [item.site_id for item in results] == expected_site_ids


# Production break caught: accepting an unbounded, zero, fractional, or boolean limit.
@pytest.mark.parametrize("limit", [0, 51, -1, True, 1.5])
def test_search_rejects_limit_outside_strict_integer_range(limit: Any) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(ValueError, match="limit must be an integer from 1 to 50"):
        store.search(query="", limit=limit)


# Production break caught: silently treating a bool/int/list filter as an ordinary
# unmatched value instead of rejecting a malformed request.
@pytest.mark.parametrize(
    "filter_name",
    ["institution_type", "foundation_type", "education_office", "district"],
)
@pytest.mark.parametrize("invalid_value", [True, 1, ["강남구"]])
def test_search_rejects_non_string_filter(
    filter_name: str,
    invalid_value: Any,
) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(
        TypeError,
        match=f"{filter_name} must be a string or None",
    ):
        store.search(query="", **{filter_name: invalid_value})


# Production break caught: leaking a raw unhashable-dict-key error from require_site
# for malformed request input.
@pytest.mark.parametrize("site_id", [None, True, 1, ["site"]])
def test_require_site_rejects_non_string_input(site_id: Any) -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    with pytest.raises(TypeError, match="site_id must be a string"):
        store.require_site(site_id)


# Production break caught: require_site returning a reconstructed or user-supplied site.
def test_require_site_returns_only_the_loaded_server_approved_site() -> None:
    store = InstitutionStore.load(SNAPSHOT_ROOT)

    site = store.require_site("test-neis:B10:SAEBOM:branch")

    assert site.site_id == "test-neis:B10:SAEBOM:branch"
    assert site.site_name == "분교장"
    assert site.institution_id == "test-neis:B10:SAEBOM"
