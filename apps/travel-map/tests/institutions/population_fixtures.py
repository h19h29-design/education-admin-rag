from pathlib import Path

from app.institutions.sources.common import (
    SourceInstitutionRecord,
    SourceProvenance,
    normalized_records_sha256,
)
from app.institutions.sources.neis_classification import (
    PINNED_POLICY_SHA256,
    NeisUnclassifiedPolicy,
)
from app.institutions.sources.school_count_profile import (
    SchoolCountPopulationProfile,
    load_school_count_population_profile,
)
from app.institutions.sources.sen_counts import (
    ReviewedSchoolCounts,
    load_reviewed_school_counts,
)

SOURCE_RESOURCES = Path("apps/travel-map/resources/institution-sources")
REVIEWED_NEIS_UNCLASSIFIED_POLICY = NeisUnclassifiedPolicy(
    counts=(
        ("평생학교(고)-2년6학기", 7),
        ("평생학교(고)-3년6학기", 4),
        ("평생학교(중)-2년6학기", 5),
        ("평생학교(초)-3년6학기", 2),
    ),
    sha256=PINNED_POLICY_SHA256,
    reviewed_as_of="2026-08-13",
    reviewer_role="data-steward",
)


def reviewed_population_fixture() -> tuple[
    SchoolCountPopulationProfile,
    ReviewedSchoolCounts,
    tuple[SourceInstitutionRecord, ...],
    dict[str, SourceProvenance],
]:
    profile = load_school_count_population_profile(
        SOURCE_RESOURCES / "school-count-population-profile.csv",
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    benchmark = load_reviewed_school_counts(
        SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    )
    records: list[SourceInstitutionRecord] = []
    sequence = 1
    for row in profile.rows:
        if row.reconciliation_role == "NONSELECTABLE":
            continue
        assert row.normalized_type is not None
        for _ in range(row.observed_count):
            source = row.source
            records.append(
                SourceInstitutionRecord(
                    institution_id=(
                        f"kinder:{sequence:07d}"
                        if source == "KINDERGARTEN_INFO"
                        else f"neis:B10:{sequence:07d}"
                    ),
                    official_name=f"검증학교-{sequence:07d}",
                    institution_type=row.normalized_type,
                    foundation_type="PUBLIC",
                    education_office="서울특별시교육청",
                    road_address="서울특별시 중구 검증로 1",
                    district="중구",
                    latitude=37.56,
                    longitude=126.97,
                    source=source,
                    source_region_code=(
                        "11" if source == "KINDERGARTEN_INFO" else "B10"
                    ),
                    source_as_of=(
                        profile.kindergarten_source_as_of
                        if source == "KINDERGARTEN_INFO"
                        else "2026-06-07"
                    ),
                    coordinate_quality="MANUALLY_VERIFIED",
                    source_kind_label=(
                        row.source_category if source == "NEIS" else None
                    ),
                )
            )
            sequence += 1
    record_tuple = tuple(records)
    neis_records = tuple(row for row in record_tuple if row.source == "NEIS")
    kindergarten_records = tuple(
        row for row in record_tuple if row.source == "KINDERGARTEN_INFO"
    )
    common = {
        "fetched_at": "2026-08-10T09:00:00Z",
        "raw_sha256": "a" * 64,
        "request_timing": None,
    }
    provenance = {
        "NEIS": SourceProvenance(
            source="NEIS",
            endpoint="https://open.neis.go.kr/hub/schoolInfo",
            license_name="PUBLIC_DATA_NO_USE_RESTRICTION",
            attribution="Ministry of Education NEIS education data",
            source_as_of="2026-06-07",
            source_observation_date_counts=(("2026-06-07", 1_415),),
            normalized_observation_date_counts=(("2026-06-07", 1_414),),
            page_count=2,
            row_count=1_414,
            fetched_row_count=1_415,
            request_region_code="B10",
            normalized_sha256=normalized_records_sha256(neis_records),
            unclassified_school_kind_counts=(
                REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts
            ),
            unclassified_school_policy_sha256=(
                REVIEWED_NEIS_UNCLASSIFIED_POLICY.sha256
            ),
            source_category_counts=tuple(
                sorted(profile.source_category_counts("NEIS").items())
            ),
            **common,
        ),
        "KINDERGARTEN_INFO": SourceProvenance(
            source="KINDERGARTEN_INFO",
            endpoint=(
                "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
            ),
            license_name="PUBLIC_DATA_PORTAL_TERMS",
            attribution="Ministry of Education Kindergarten Info",
            source_as_of="2026-04-01",
            source_observation_date_counts=(("2026-04-01", 706),),
            normalized_observation_date_counts=(("2026-04-01", 706),),
            page_count=25,
            row_count=706,
            fetched_row_count=706,
            request_region_code="11",
            request_timing="20261",
            normalized_sha256=normalized_records_sha256(kindergarten_records),
            source_category_counts=(("KINDERGARTEN_TOTAL", 706),),
            fetched_at=common["fetched_at"],
            raw_sha256=common["raw_sha256"],
        ),
    }
    return profile, benchmark, record_tuple, provenance
