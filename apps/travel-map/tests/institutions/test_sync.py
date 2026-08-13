import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import runpy
import subprocess
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace, TracebackType

import app.institutions.sources.kindergarten as kindergarten_module
import app.institutions.sources.neis as neis_module
import app.institutions.sources.neis_classification as neis_classification_module
import app.institutions.sources.standard_school as standard_school_module
import app.institutions.sync as sync_module
import app.providers.kakao_local as kakao_module
import httpx
import pytest
from app.institutions.models import Institution, InstitutionSite, InstitutionStatus
from app.institutions.snapshot import (
    SnapshotIntegrityError,
    VerifiedSnapshot,
    verify_snapshot,
)
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceFetchResult,
    SourceInstitutionRecord,
    SourceInstitutionSiteRecord,
    SourceProvenance,
    get_json_with_retry,
    normalized_records_sha256,
    observation_date_counts,
    source_as_of_for,
)
from app.institutions.sources.kindergarten import (
    KindergartenSource,
    parse_kindergarten_region_codes,
    parse_kindergarten_rows,
)
from app.institutions.sources.neis import NeisSource, parse_neis_rows
from app.institutions.sources.neis_classification import (
    PINNED_POLICY_SHA256,
    NeisUnclassifiedPolicy,
    load_neis_unclassified_policy,
    validate_unclassified_school_counts,
)
from app.institutions.sources.school_count_profile import (
    PINNED_POPULATION_PROFILE_SHA256,
    SchoolCountPopulationProfile,
    SchoolPopulationRow,
    load_school_count_population_profile,
)
from app.institutions.sources.sen import SenCsvSource, parse_sen_csv
from app.institutions.sources.sen_counts import (
    ReportedSchoolTotal,
    ReviewedSchoolCounts,
    SchoolCountEvidence,
    load_reviewed_school_counts,
)
from app.institutions.sources.standard_school import (
    StandardSchoolLocationSource,
    enrich_neis_coordinates,
    parse_standard_school_locations,
)
from app.institutions.store import InstitutionStore
from app.institutions.sync import (
    SnapshotBuildResult,
    SnapshotQualityError,
    build_candidate_snapshot,
    build_sync_preflight_audit,
    emit_sync_preflight_audit,
    enrichment_records_sha256,
    geocode_missing_records,
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


def test_school_count_population_profile_loads_exact_reviewed_contract() -> None:
    profile = load_school_count_population_profile(
        SOURCE_RESOURCES / "school-count-population-profile.csv",
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert profile.sha256 == PINNED_POPULATION_PROFILE_SHA256
    assert profile.status == "TEMPORARY_PRELIMINARY_VARIANCE"
    assert profile.approved_variances == (
        ("ELEMENTARY_SCHOOL", 1),
        ("HIGH_SCHOOL", 0),
        ("KINDERGARTEN", -18),
        ("MIDDLE_SCHOOL", 0),
        ("MISC_SCHOOL", 4),
        ("SPECIAL_SCHOOL", 0),
    )
    assert profile.source_totals() == {
        "KINDERGARTEN_INFO": 706,
        "NEIS": 1_415,
    }
    assert profile.role_counts("NEIS") == {
        "BENCHMARK": 1_373,
        "NONSELECTABLE": 1,
        "QUARANTINED": 18,
        "SUPPLEMENTARY": 23,
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("각종학교(고)", "각종학교(고) "),
        ("NEIS,고등학교,319", "NEIS,고등학교,320"),
        ("NONSELECTABLE", "SUPPLEMENTARY"),
        ("MISC_SCHOOL,BENCHMARK,MISC_SCHOOL", "HIGH_SCHOOL,BENCHMARK,MISC_SCHOOL"),
        ("KINDERGARTEN,BENCHMARK,KINDERGARTEN", "KINDERGARTEN,BENCHMARK,MISC_SCHOOL"),
        ("kindergarten_timing=20261", "kindergarten_timing=20262"),
        ("kindergarten_source_as_of=2026-04-01", "kindergarten_source_as_of=2026-04-02"),
        (
            "benchmark_raw_sha256=6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70",
            "benchmark_raw_sha256=" + "0" * 64,
        ),
        (
            "unclassified_policy_sha256=2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1",
            "unclassified_policy_sha256=" + "0" * 64,
        ),
        (
            "NEIS,각종학교(고),13,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL\nNEIS,각종학교(중),7,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL",
            "NEIS,각종학교(중),7,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL\nNEIS,각종학교(고),13,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL",
        ),
        (
            "NEIS,특수학교,32,SPECIAL_SCHOOL,BENCHMARK,SPECIAL_SCHOOL\n",
            "NEIS,특수학교,32,SPECIAL_SCHOOL,BENCHMARK,SPECIAL_SCHOOL\nNEIS,특수학교,32,SPECIAL_SCHOOL,BENCHMARK,SPECIAL_SCHOOL\n",
        ),
        ("# reviewer_role=data-steward", "# reviewer_role=data-steward\n# unreviewed=1"),
        (
            "source,source_category,observed_count,normalized_type,reconciliation_role,benchmark_type",
            "source,source_category,observed_count,normalized_type,reconciliation_role,benchmark_type,extra",
        ),
    ],
)
def test_school_count_population_profile_rejects_resource_trust_boundary_mutations(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    path = tmp_path / "profile.csv"
    content = (SOURCE_RESOURCES / "school-count-population-profile.csv").read_text(
        encoding="utf-8"
    )
    path.write_text(content.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(SourceDataError):
        load_school_count_population_profile(
            path,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )


def test_school_count_population_profile_rejects_malformed_utf8_resource(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.csv"
    path.write_bytes(b"# normalized_sha256=" + b"0" * 64 + b"\n\xff")

    with pytest.raises(SourceDataError):
        load_school_count_population_profile(
            path,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )


def test_school_count_population_profile_rejects_symlinked_resource(
    tmp_path: Path,
) -> None:
    link = tmp_path / "profile-link.csv"
    link.symlink_to(SOURCE_RESOURCES / "school-count-population-profile.csv")

    with pytest.raises(SourceDataError, match="symlink|regular"):
        load_school_count_population_profile(
            link,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )


def test_school_count_population_profile_rejects_oversized_resource_before_decode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.csv"
    path.write_bytes(b"x" * 16_385)

    with pytest.raises(SourceDataError, match="size limit"):
        load_school_count_population_profile(
            path,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )


def test_school_count_population_profile_rejects_subclass_spoofs_and_contract_drift() -> None:
    profile = load_school_count_population_profile(
        SOURCE_RESOURCES / "school-count-population-profile.csv",
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    class SpoofedString(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    class SpoofedTuple(tuple):
        def __eq__(self, other: object) -> bool:
            return True

    with pytest.raises(ValueError):
        SchoolPopulationRow(
            source=SpoofedString("NEIS"),
            source_category="고등학교",
            observed_count=319,
            normalized_type="HIGH_SCHOOL",
            reconciliation_role="BENCHMARK",
            benchmark_type="HIGH_SCHOOL",
        )
    with pytest.raises(ValueError):
        SchoolCountPopulationProfile(
            sha256=SpoofedString(PINNED_POPULATION_PROFILE_SHA256),
            status=profile.status,
            reviewed_as_of=profile.reviewed_as_of,
            reviewer_role=profile.reviewer_role,
            neis_region_code=profile.neis_region_code,
            neis_fetched_row_count=profile.neis_fetched_row_count,
            neis_normalized_row_count=profile.neis_normalized_row_count,
            kindergarten_timing=profile.kindergarten_timing,
            kindergarten_source_as_of=profile.kindergarten_source_as_of,
            kindergarten_fetched_row_count=profile.kindergarten_fetched_row_count,
            benchmark_source_url=profile.benchmark_source_url,
            benchmark_source_as_of=profile.benchmark_source_as_of,
            benchmark_raw_sha256=profile.benchmark_raw_sha256,
            unclassified_policy_sha256=profile.unclassified_policy_sha256,
            approved_variances=SpoofedTuple(profile.approved_variances),
            rows=profile.rows,
        )
    with pytest.raises(ValueError):
        SchoolCountPopulationProfile(
            sha256=profile.sha256,
            status="APPROVED",
            reviewed_as_of=profile.reviewed_as_of,
            reviewer_role=profile.reviewer_role,
            neis_region_code=profile.neis_region_code,
            neis_fetched_row_count=profile.neis_fetched_row_count,
            neis_normalized_row_count=profile.neis_normalized_row_count,
            kindergarten_timing=profile.kindergarten_timing,
            kindergarten_source_as_of=profile.kindergarten_source_as_of,
            kindergarten_fetched_row_count=profile.kindergarten_fetched_row_count,
            benchmark_source_url=profile.benchmark_source_url,
            benchmark_source_as_of=profile.benchmark_source_as_of,
            benchmark_raw_sha256=profile.benchmark_raw_sha256,
            unclassified_policy_sha256=profile.unclassified_policy_sha256,
            approved_variances=profile.approved_variances,
            rows=profile.rows,
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
        if (
            "apps/travel-map/app/" in frame.f_code.co_filename
            or "apps/travel-map/scripts/" in frame.f_code.co_filename
        ):
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


def test_load_neis_unclassified_policy_accepts_exact_reviewed_resource() -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )

    assert policy.counts == (
        ("평생학교(고)-2년6학기", 7),
        ("평생학교(고)-3년6학기", 4),
        ("평생학교(중)-2년6학기", 5),
        ("평생학교(초)-3년6학기", 2),
    )
    assert policy.sha256 == (
        "2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1"
    )
    assert policy.sha256 == PINNED_POLICY_SHA256
    assert policy.reviewed_as_of == "2026-08-13"
    assert policy.reviewer_role == "data-steward"


@pytest.mark.parametrize(
    ("counts", "sha256"),
    [
        (
            (("unreviewed-kind", 18),),
            PINNED_POLICY_SHA256,
        ),
        (
            (
                ("평생학교(고)-2년6학기", 8),
                ("평생학교(고)-3년6학기", 4),
                ("평생학교(중)-2년6학기", 5),
                ("평생학교(초)-3년6학기", 1),
            ),
            PINNED_POLICY_SHA256,
        ),
        (
            (
                ("평생학교(고)-2년6학기", 7),
                ("평생학교(고)-3년6학기", 4),
                ("평생학교(중)-2년6학기", 5),
                ("평생학교(초)-3년6학기", 2),
            ),
            "0" * 64,
        ),
    ],
)
def test_neis_unclassified_policy_rejects_caller_supplied_contract_drift(
    counts: tuple[tuple[str, int], ...],
    sha256: str,
) -> None:
    with pytest.raises(SourceDataError, match="reviewed"):
        NeisUnclassifiedPolicy(
            counts=counts,
            sha256=sha256,
            reviewed_as_of="2026-08-13",
            reviewer_role="data-steward",
        )


def test_neis_unclassified_policy_rejects_tuple_subclass_whitelist_spoof() -> None:
    class SpoofedReviewedPair(tuple):
        def __eq__(self, other: object) -> bool:
            return type(other) is tuple and len(other) == 2

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(("unreviewed-kind", 18))

    counts = (
        SpoofedReviewedPair(("unreviewed-kind", 18)),
        ("평생학교(고)-3년6학기", 4),
        ("평생학교(중)-2년6학기", 5),
        ("평생학교(초)-3년6학기", 2),
    )

    with pytest.raises(SourceDataError, match="reviewed"):
        NeisUnclassifiedPolicy(
            counts=counts,
            sha256=PINNED_POLICY_SHA256,
            reviewed_as_of="2026-08-13",
            reviewer_role="data-steward",
        )


@pytest.mark.parametrize(
    "field",
    ["sha256", "reviewed_as_of", "reviewer_role"],
)
def test_neis_unclassified_policy_rejects_spoofed_string_metadata(
    field: str,
) -> None:
    class SpoofedReviewedString(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    values = {
        "sha256": PINNED_POLICY_SHA256,
        "reviewed_as_of": "2026-08-13",
        "reviewer_role": "data-steward",
    }
    values[field] = SpoofedReviewedString("unreviewed")

    with pytest.raises(SourceDataError, match="reviewed"):
        NeisUnclassifiedPolicy(
            counts=REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts,
            sha256=values["sha256"],
            reviewed_as_of=values["reviewed_as_of"],
            reviewer_role=values["reviewer_role"],
        )


def test_neis_unclassified_policy_rejects_oversized_file_before_content_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "oversized-policy.csv"
    path.write_bytes(b"x" * (16 * 1024 + 1))

    def unexpected_content_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized policy content was opened")

    monkeypatch.setattr(Path, "open", unexpected_content_open)

    with pytest.raises(SourceDataError, match="size limit"):
        load_neis_unclassified_policy(path)


def test_neis_unclassified_policy_uses_no_follow_descriptor_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    actual_open = os.open
    calls: list[int] = []

    def recording_open(path: object, flags: int, *args: object) -> int:
        calls.append(flags)
        return actual_open(path, flags, *args)

    monkeypatch.setattr(neis_classification_module.os, "open", recording_open)

    load_neis_unclassified_policy(resource)

    assert calls
    assert all(flags & os.O_NOFOLLOW for flags in calls)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("schemaVersion=1", "schemaVersion=2"),
        (
            "school_kind,expected_count,reason_code",
            "school_kind,expected_count,reason_code,extra_column",
        ),
        (
            "평생학교(고)-3년6학기,4,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(고)-2년6학기,7,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        (
            "평생학교(중)-2년6학기,5,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(초)-3년6학기,2,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        (
            "평생학교(고)-2년6학기,7,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(고)-2년6학기,True,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        ("OFFICIAL_CLASSIFICATION_PENDING", "DIFFERENT_REASON"),
    ],
)
def test_neis_unclassified_policy_rejects_tampered_reviewed_resource(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source = SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    path = tmp_path / "policy.csv"
    body = source.read_text(encoding="utf-8").replace(old, new, 1)
    path.write_text(body, encoding="utf-8")

    with pytest.raises(SourceDataError, match="reviewed|SHA-256|policy"):
        load_neis_unclassified_policy(path)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "school_kind,expected_count,reason_code",
            "school_kind,expected_count,reason_code,extra_column",
        ),
        (
            "school_kind,expected_count,reason_code",
            "school_kind,expected_count",
        ),
        (
            "평생학교(고)-3년6학기,4,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(고)-2년6학기,4,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        (
            (
                "평생학교(고)-2년6학기,7,OFFICIAL_CLASSIFICATION_PENDING\n"
                "평생학교(고)-3년6학기,4,OFFICIAL_CLASSIFICATION_PENDING"
            ),
            (
                "평생학교(고)-3년6학기,4,OFFICIAL_CLASSIFICATION_PENDING\n"
                "평생학교(고)-2년6학기,7,OFFICIAL_CLASSIFICATION_PENDING"
            ),
        ),
        (
            "평생학교(초)-3년6학기,2,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(초)-3년6학기,0,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        (
            "평생학교(초)-3년6학기,2,OFFICIAL_CLASSIFICATION_PENDING",
            "평생학교(초)-3년6학기,True,OFFICIAL_CLASSIFICATION_PENDING",
        ),
        ("OFFICIAL_CLASSIFICATION_PENDING", "DIFFERENT_REASON"),
    ],
)
def test_neis_unclassified_policy_rejects_malformed_rehashed_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old: str,
    new: str,
) -> None:
    path = tmp_path / "policy.csv"
    body = (SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv").read_text(
        encoding="utf-8"
    ).replace(old, new, 1)
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        neis_classification_module,
        "PINNED_POLICY_SHA256",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )

    with pytest.raises(SourceDataError, match="columns|labels|counts|rows"):
        load_neis_unclassified_policy(path)


def test_neis_unclassified_policy_rejects_symlinked_resource(tmp_path: Path) -> None:
    link = tmp_path / "policy-link.csv"
    link.symlink_to(SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv")

    with pytest.raises(SourceDataError, match="symlink|regular"):
        load_neis_unclassified_policy(link)


def test_neis_quarantines_only_the_exact_reviewed_lifelong_school_labels() -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )
    payload = neis_payload_rows(
        *(label for label, count in policy.counts for _ in range(count))
    )

    records = parse_neis_rows(payload, unclassified_policy=policy)

    assert Counter(row.institution_type for row in records) == {
        "UNCLASSIFIED_SCHOOL": 18
    }
    assert {row.source_kind_label for row in records} == policy.labels
    assert validate_unclassified_school_counts(records, policy) == dict(policy.counts)


@pytest.mark.asyncio
async def test_neis_binds_quarantine_histogram_and_policy_hash_to_provenance() -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )
    payload = neis_payload_rows(
        *(label for label, count in policy.counts for _ in range(count))
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            unclassified_policy=policy,
        ).fetch()

    assert result.provenance.unclassified_school_kind_counts == policy.counts
    assert result.provenance.unclassified_school_policy_sha256 == policy.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_types",
    [
        ("평생학교(초)-3년6학기",) * 3
        + ("평생학교(중)-2년6학기",) * 5
        + ("평생학교(고)-2년6학기",) * 7
        + ("평생학교(고)-3년6학기",) * 4,
        ("평생학교(초)-3년6학기",) * 2
        + ("평생학교(중)-2년6학기",) * 5
        + ("평생학교(고)-2년6학기",) * 8
        + ("평생학교(고)-3년6학기",) * 4,
    ],
)
async def test_neis_rejects_quarantine_count_drift(
    source_types: tuple[str, ...],
) -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )
    payload = neis_payload_rows(*source_types)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceDataError, match="counts"):
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=policy,
                page_size=len(source_types),
            ).fetch()


@pytest.mark.asyncio
async def test_neis_rejects_school_kind_whitespace_before_candidate_or_pointer_mutation(
    tmp_path: Path,
) -> None:
    source_types = list(reviewed_neis_source_types())
    source_types[source_types.index("평생학교(고)-2년6학기")] += " "
    payload = neis_payload_rows(*source_types)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    pointer = snapshot_root / "current.json"
    original_pointer = b'{"snapshotId":"approved-before-fetch"}\n'
    pointer.write_bytes(original_pointer)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceDataError, match="school kind|unsupported"):
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

    assert not (snapshot_root / ".whitespace.candidate").exists()
    assert pointer.read_bytes() == original_pointer


def test_neis_rejects_unknown_lifelong_school_label_before_candidate_creation() -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )
    payload = neis_payload_rows("평생학교(고)-4년8학기")

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload, unclassified_policy=policy)


def test_neis_policy_label_is_rejected_without_a_supplied_policy() -> None:
    payload = neis_payload_rows("평생학교(초)-3년6학기")

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload)


# Production break caught: collapsing two raw vintages on one API page to one date.
def test_neis_preserves_mixed_load_dates_within_one_page() -> None:
    payload = load_json("neis-school-info.json")
    rows = payload["schoolInfo"][1]["row"]  # type: ignore[index]
    rows[0]["LOAD_DTM"] = "20260423"
    rows[1]["LOAD_DTM"] = "20260607"

    assert [row.source_as_of for row in parse_neis_rows(payload)] == [
        "2026-04-23",
        "2026-06-07",
    ]


# Production break caught: treating a filtered-out row as if its vintage cannot
# invalidate the raw source page.
def test_neis_validates_load_date_on_nonselectable_raw_row() -> None:
    payload = neis_payload(source_type="공동실습소")
    row = payload["schoolInfo"][1]["row"][0]  # type: ignore[index]
    row["LOAD_DTM"] = "invalid"

    with pytest.raises(SourceDataError, match="unsupported"):
        parse_neis_rows(payload)


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
    assert result.provenance.fetched_row_count == 42
    assert all(record.foundation_type == "PUBLIC" for record in result.records)
    assert all(record.source_region_code == "SEOUL" for record in result.records)
    assert all(not hasattr(record, "telephone") for record in result.records)
    gangseo = next(
        record
        for record in result.records
        if record.institution_id == "sen:gangseo-library"
    )
    assert gangseo.official_name == "강서도서관"
    assert gangseo.site_name == "본관"
    assert gangseo.road_address == "서울특별시 강서구 등촌로51나길 29"
    assert gangseo.additional_sites == (
        SourceInstitutionSiteRecord(
            site_code="gayang",
            site_name="가양관",
            road_address="서울특별시 강서구 양천로55길 46",
            district="강서구",
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        ),
    )
    assert not hasattr(gangseo.additional_sites[0], "telephone")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (",gayang,\\uac00\\uc591\\uad00,false", ",main,\\uac00\\uc591\\uad00,false"),
        (",gayang,\\uac00\\uc591\\uad00,false", ",gayang,\\uac00\\uc591\\uad00,true"),
    ],
)
def test_sen_multisite_parser_rejects_duplicate_or_second_default(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source = SOURCE_RESOURCES / "sen-institutions.csv"
    text = source.read_text(encoding="utf-8")
    assert old in text
    changed = text.replace(old, new, 1)
    data_lines = [
        line
        for line in changed.splitlines()
        if line.strip() and not line.startswith("# ")
    ]
    digest = hashlib.sha256(
        ("\n".join(data_lines) + "\n").encode("utf-8")
    ).hexdigest()
    changed = changed.replace(
        "c2b7e84c476175586b9f3764f54ee008fc35cb7831b4a8a0186ded9b608aac50",
        digest,
        1,
    )
    path = tmp_path / "invalid-sites.csv"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(SourceDataError, match="defaults|identifiers"):
        parse_sen_csv(path)


def test_unresolved_sen_main_and_branch_are_both_persisted_for_review(
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
        snapshot_id="unresolved-sen-multisite",
        coverage=TEST_COVERAGE,
        source_provenance={result.provenance.source: result.provenance},
    )

    assert candidate.issues == (
        "coordinate validation success rate is below 98 percent",
    )
    institutions = [
        json.loads(line)
        for line in (candidate.candidate_path / "institutions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    sites = [
        json.loads(line)
        for line in (candidate.candidate_path / "sites.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    gangseo = next(
        item
        for item in institutions
        if item["institutionId"] == "sen:gangseo-library"
    )
    gangseo_sites = [
        item
        for item in sites
        if item["institutionId"] == "sen:gangseo-library"
    ]
    assert gangseo["status"] == "REVIEW_REQUIRED"
    assert {item["siteId"] for item in gangseo_sites} == {
        "sen:gangseo-library:main",
        "sen:gangseo-library:gayang",
    }
    assert {item["status"] for item in gangseo_sites} == {"REVIEW_REQUIRED"}
    assert all(item["latitude"] is None for item in gangseo_sites)


def test_reviewed_sen_multisite_survives_snapshot_and_store(
    tmp_path: Path,
) -> None:
    sen_result = SenCsvSource(
        SOURCE_RESOURCES / "sen-institutions.csv",
        expected_type_counts={
            "HEADQUARTERS": 1,
            "DISTRICT_OFFICE": 11,
            "DIRECT_AGENCY": 8,
            "LIFELONG_LEARNING_CENTER": 4,
            "LIBRARY": 17,
        },
    ).load()
    geocoded_sen = tuple(
        replace(
            record,
            latitude=37.56,
            longitude=126.97,
            coordinate_quality="GEOCODED",
            additional_sites=tuple(
                replace(
                    site,
                    latitude=37.57,
                    longitude=126.84,
                    coordinate_quality="GEOCODED",
                )
                for site in record.additional_sites
            ),
        )
        for record in sen_result.records
    )
    neis_records = tuple(
        source_record(institution_id=f"neis:B10:7011{index:03d}")
        for index in range(9)
    ) + quarantined_neis_records("neis:B10:reviewed-sen-quarantine")
    records = geocoded_sen + neis_records
    geocoded_count = sum(
        record.coordinate_quality == "GEOCODED"
        for record in records
    ) + sum(
        site.coordinate_quality == "GEOCODED"
        for record in records
        for site in record.additional_sites
    )
    kakao = EnrichmentProvenance(
        source="KAKAO_LOCAL_GEOCODING",
        endpoint="https://dapi.kakao.com/v2/local/search/address.json",
        license_name="KAKAO_LOCAL_API_TERMS",
        attribution="Kakao Local API",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of="2026-08-10",
        raw_sha256="c" * 64,
        normalized_sha256=sync_module._geocoded_records_sha256(records),
        request_region_code="SEOUL_ADDRESS_BATCH",
        request_timing=None,
        page_count=geocoded_count,
        fetched_row_count=geocoded_count,
        matched_row_count=geocoded_count,
        matched_normalized_sha256=enrichment_records_sha256(
            records,
            "GEOCODED",
        ),
    )
    neis_provenance = source_provenance_for(neis_records)["NEIS"]
    candidate = build_candidate_snapshot(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="reviewed-sen-multisite",
        coverage=TEST_COVERAGE,
        source_provenance={
            sen_result.provenance.source: sen_result.provenance,
            neis_provenance.source: neis_provenance,
        },
        enrichment_provenance=(kakao,),
    )

    assert candidate.issues == ()
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    store = InstitutionStore.load(tmp_path)

    matches = store.search("강서도서관")
    assert {item.site_id for item in matches} == {
        "sen:gangseo-library:main",
        "sen:gangseo-library:gayang",
    }
    assert sum(item.site_name == "본관" for item in matches) == 1
    assert store.require_site("sen:gangseo-library:gayang").site_name == "가양관"


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


def test_reviewed_school_count_resource_is_official_and_pinned() -> None:
    benchmark = load_reviewed_school_counts(
        SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    )
    assert benchmark.counts == {
        "KINDERGARTEN": 724,
        "ELEMENTARY_SCHOOL": 609,
        "MIDDLE_SCHOOL": 390,
        "HIGH_SCHOOL": 319,
        "SPECIAL_SCHOOL": 32,
        "MISC_SCHOOL": 18,
    }
    assert benchmark.category_evidence["KINDERGARTEN"].source_as_of == (
        "2026-03-10"
    )
    assert (
        benchmark.category_evidence["KINDERGARTEN"].status
        == "PRELIMINARY_2026"
    )
    assert benchmark.category_evidence["ELEMENTARY_SCHOOL"].source_as_of == (
        "2026-03-10"
    )
    assert (
        benchmark.category_evidence["ELEMENTARY_SCHOOL"].status
        == "PRELIMINARY_2026"
    )
    assert benchmark.category_composition["MISC_SCHOOL"] == (
        "각종학교17+고등기술학교1"
    )
    assert benchmark.reported_totals == (
        ReportedSchoolTotal(
            expected_count=2_092,
            population=(
                "KINDERGARTEN+ELEMENTARY_SCHOOL+MIDDLE_SCHOOL+"
                "HIGH_SCHOOL+SPECIAL_SCHOOL+MISC_SCHOOL"
            ),
            used_for_gate=False,
            evidence=SchoolCountEvidence(
                source_url=(
                    "https://enews.sen.go.kr/uploads/img_smart//"
                    "2026-06-08/20260608075519432.png"
                ),
                source_as_of="2026-03-10",
                source_sha256=(
                    "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a170"
                    "5db508afeee15e70"
                ),
                status="PRELIMINARY_2026",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("KINDERGARTEN,724,", "KINDERGARTEN,725,"),
        (
            "https://enews.sen.go.kr/uploads/img_smart//",
            "https://attacker.invalid/",
        ),
        ("PRELIMINARY_2026", "FINAL_2026"),
        (
            "36158d45a3b8c7e8a083e6d78f63fee706618f69eb49d8624877aef07e3a9332",
            "f" * 64,
        ),
    ],
)
def test_reviewed_school_count_resource_rejects_mutation(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    source = SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    text = source.read_text(encoding="utf-8")
    assert old in text
    tampered = tmp_path / "sen-counts.csv"
    tampered.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(SourceDataError, match="reviewed|provenance"):
        load_reviewed_school_counts(tampered)


@pytest.mark.parametrize(
    ("institution_type", "expected", "passing_actual", "failing_actual"),
    [
        ("ELEMENTARY_SCHOOL", 609, 603, 602),
        ("MIDDLE_SCHOOL", 390, 387, 386),
        ("HIGH_SCHOOL", 318, 315, 314),
    ],
)
def test_school_reconciliation_checks_one_percent_per_category(
    institution_type: str,
    expected: int,
    passing_actual: int,
    failing_actual: int,
) -> None:
    benchmark = reviewed_counts_fixture({institution_type: expected})
    passing = reconcile_selectable_school_counts(
        with_neis_quarantine(
            records_for_type_counts({institution_type: passing_actual})
        ),
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    failing = reconcile_selectable_school_counts(
        with_neis_quarantine(
            records_for_type_counts({institution_type: failing_actual})
        ),
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert passing["categories"][institution_type]["passed"] is True
    assert passing["passed"] is True
    assert failing["categories"][institution_type]["passed"] is False
    assert failing["passed"] is False


def test_school_reconciliation_cannot_hide_swapped_category_losses() -> None:
    benchmark = reviewed_counts_fixture(
        {"ELEMENTARY_SCHOOL": 609, "MIDDLE_SCHOOL": 390}
    )
    audit = reconcile_selectable_school_counts(
        with_neis_quarantine(
            records_for_type_counts(
                {"ELEMENTARY_SCHOOL": 599, "MIDDLE_SCHOOL": 400}
            )
        ),
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert audit["categories"]["ELEMENTARY_SCHOOL"]["passed"] is False
    assert audit["categories"]["MIDDLE_SCHOOL"]["passed"] is False
    assert audit["passed"] is False


def test_school_reconciliation_passes_reviewed_real_count_fixture() -> None:
    benchmark = load_reviewed_school_counts(
        SOURCE_RESOURCES / "sen-annual-school-counts.csv"
    )
    audit = reconcile_selectable_school_counts(
        with_neis_quarantine(records_for_type_counts(benchmark.counts)),
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert audit["passed"] is True
    assert audit["reportedTotals"] == [
        {
            "expectedCount": 2_092,
            "actualCount": 2_092,
            "population": (
                "KINDERGARTEN+ELEMENTARY_SCHOOL+MIDDLE_SCHOOL+"
                "HIGH_SCHOOL+SPECIAL_SCHOOL+MISC_SCHOOL"
            ),
            "usedForGate": False,
            "passed": None,
            "sourceUrl": (
                "https://enews.sen.go.kr/uploads/img_smart//"
                "2026-06-08/20260608075519432.png"
            ),
            "sourceAsOf": "2026-03-10",
            "sourceSha256": (
                "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a170"
                "5db508afeee15e70"
            ),
            "evidenceStatus": "PRELIMINARY_2026",
        }
    ]
    assert (
        audit["categories"]["KINDERGARTEN"]["sourceAsOf"]
        == "2026-03-10"
    )
    assert (
        audit["categories"]["ELEMENTARY_SCHOOL"]["sourceAsOf"]
        == "2026-03-10"
    )
    assert audit["categories"]["MISC_SCHOOL"]["composition"] == (
        "각종학교17+고등기술학교1"
    )
    assert all(
        category["deltaCount"] == 0
        for category in audit["categories"].values()
    )


def test_school_reconciliation_rejects_actual_source_contamination() -> None:
    benchmark = reviewed_counts_fixture({"KINDERGARTEN": 1})
    contaminated = (
        replace(
            source_record(),
            institution_type="KINDERGARTEN",
            source="NEIS",
            institution_id="neis:B10:7010001",
        ),
    )

    audit = reconcile_selectable_school_counts(
        with_neis_quarantine(contaminated),
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert audit["categories"]["KINDERGARTEN"]["sourceValidationPassed"] is False
    assert audit["categories"]["KINDERGARTEN"]["passed"] is False
    assert audit["passed"] is False


# Production break caught: treating a valid mixed-vintage NEIS population as
# contaminated merely because its raw rows were loaded on multiple dates.
def test_school_reconciliation_allows_multi_vintage_neis_but_requires_neis_only() -> None:
    records = with_neis_quarantine(mixed_neis_records())
    benchmark = reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 4})

    audit = reconcile_selectable_school_counts(
        records,
        benchmark=benchmark,
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    category = audit["categories"]["ELEMENTARY_SCHOOL"]

    assert category["actualSourceAsOf"] == [
        "2026-04-23",
        "2026-05-17",
        "2026-06-07",
    ]
    assert category["actualSourceObservationDateCounts"] == {
        "2026-04-23": 2,
        "2026-05-17": 1,
        "2026-06-07": 1,
    }
    assert category["sourceValidationPassed"] is True


# Production break caught: treating reviewed lifelong-school labels as selectable
# categories, or letting their valid coordinates bypass the pending-classification
# quarantine.
def test_unclassified_reconciliation_keeps_official_counts_and_forces_status(
    tmp_path: Path,
) -> None:
    policy = REVIEWED_NEIS_UNCLASSIFIED_POLICY
    official_records = records_for_type_counts({"ELEMENTARY_SCHOOL": 1})
    unclassified_records = tuple(
        replace(
            source_record(institution_id=f"neis:B10:lifelong-{index:02d}"),
            official_name=f"검토 평생학교 {index}",
            institution_type="UNCLASSIFIED_SCHOOL",
            source_kind_label=label,
            additional_sites=(
                SourceInstitutionSiteRecord(
                    site_code="branch",
                    site_name="분교장",
                    road_address="서울특별시 중구 검증로 2",
                    district="중구",
                    latitude=37.561,
                    longitude=126.971,
                    coordinate_quality="MANUALLY_VERIFIED",
                ),
            ),
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
    records = (*official_records, *unclassified_records)

    reconciliation = reconcile_selectable_school_counts(
        records,
        benchmark=reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 1}),
        unclassified_policy=policy,
    )
    candidate = build_test_candidate(
        records=with_neis_quarantine(records),
        previous=None,
        output_root=tmp_path,
        snapshot_id="unclassified-status",
    )
    audit = build_sync_preflight_audit(
        records,
        source_provenance=source_provenance_for(records),
        reconciliation=reconciliation,
    )
    institutions = [
        json.loads(line)
        for line in (candidate.candidate_path / "institutions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    sites = [
        json.loads(line)
        for line in (candidate.candidate_path / "sites.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert reconciliation["categories"]["ELEMENTARY_SCHOOL"]["actualCount"] == 1
    assert reconciliation["unclassifiedSchoolKindCounts"] == dict(policy.counts)
    assert reconciliation["unclassifiedSchoolPolicySha256"] == policy.sha256
    assert reconciliation["unclassifiedPolicyPassed"] is True
    assert audit["statusCounts"] == {
        "PRECHECK_READY_INSTITUTION": 1,
        "PRECHECK_REVIEW_REQUIRED_INSTITUTION": 18,
    }
    assert all(
        institution["status"] == InstitutionStatus.REVIEW_REQUIRED
        and institution["statusSource"] == "OFFICIAL_CLASSIFICATION_PENDING"
        for institution in institutions
        if institution["institutionType"] == "UNCLASSIFIED_SCHOOL"
    )
    assert all(
        site["status"] == InstitutionStatus.REVIEW_REQUIRED
        for site in sites
        if site["institutionId"].startswith("neis:B10:lifelong-")
    )


# Production break caught: treating an absent quarantine population as a valid
# NEIS reconciliation or allowing candidate creation with empty policy fields.
def test_neis_requires_the_complete_unclassified_quarantine_at_creation(
    tmp_path: Path,
) -> None:
    records = (source_record(),)
    reconciliation = reconcile_selectable_school_counts(
        records,
        benchmark=reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 1}),
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )

    assert reconciliation["unclassifiedPolicyPassed"] is False
    assert reconciliation["passed"] is False
    with pytest.raises(SnapshotQualityError, match="unclassified"):
        build_test_candidate(
            records=records,
            previous=None,
            output_root=tmp_path,
            snapshot_id="missing-unclassified-at-creation",
            include_neis_quarantine=False,
        )


# Production break caught: a reattested candidate with every quarantined NEIS
# row removed can otherwise be reviewed or approved as an ordinary school feed.
@pytest.mark.parametrize("operation", ["review", "approve"])
def test_zero_quarantine_neis_candidate_cannot_review_or_approve(
    tmp_path: Path,
    operation: str,
) -> None:
    records = (
        source_record(institution_id="neis:B10:ordinary"),
        *quarantined_neis_records("neis:B10:review-zero"),
    )
    baseline = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"zero-quarantine-baseline-{operation}",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    original_pointer = (tmp_path / "current.json").read_bytes()
    candidate = build_test_candidate(
        records=records,
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id=f"zero-quarantine-candidate-{operation}",
    )
    remove_unclassified_rows(
        candidate.candidate_path,
        resign_root=tmp_path,
        candidate=candidate,
    )

    with pytest.raises(SnapshotQualityError, match="unclassified"):
        if operation == "review":
            sync_module.build_candidate_review_packet(
                snapshot_id=candidate.snapshot_id,
                snapshot_root=tmp_path,
                coverage=TEST_COVERAGE,
            )
        else:
            sync_module.approve_candidate_snapshot(
                snapshot_id=candidate.snapshot_id,
                review_digest="a" * 64,
                reviewer_role="data-steward",
                snapshot_root=tmp_path,
                coverage=TEST_COVERAGE,
            )
    assert (tmp_path / "current.json").read_bytes() == original_pointer


# Production break caught: a verified snapshot can otherwise lose all 18
# quarantined institutions and replace the NEIS aggregate with empty values.
def test_verified_snapshot_rejects_zero_quarantine_neis_source(tmp_path: Path) -> None:
    records = (
        source_record(institution_id="neis:B10:ordinary"),
        *quarantined_neis_records("neis:B10:verified-zero"),
    )
    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="verified-zero-quarantine",
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    remove_unclassified_rows(tmp_path / candidate.snapshot_id)

    with pytest.raises(SnapshotIntegrityError, match="unclassified"):
        verify_snapshot(tmp_path)


# Production break caught: dropping the reviewed raw-label aggregate from the
# candidate source provenance or from the digest-bound review packet.
def test_unclassified_provenance_is_manifested_and_bound_to_review_digest(
    tmp_path: Path,
) -> None:
    policy = REVIEWED_NEIS_UNCLASSIFIED_POLICY
    records = tuple(
        replace(
            source_record(institution_id=f"neis:B10:review-{index:02d}"),
            institution_type="UNCLASSIFIED_SCHOOL",
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
    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id="unclassified-provenance",
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    source = manifest["sources"][0]
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert source["unclassifiedSchoolKindCounts"] == dict(policy.counts)
    assert source["unclassifiedSchoolPolicySha256"] == policy.sha256
    assert packet["unclassifiedSchoolKindCounts"] == dict(policy.counts)
    assert packet["unclassifiedSchoolPolicySha256"] == policy.sha256
    assert "평생학교(고)-2년6학기" not in (
        candidate.candidate_path / "institutions.jsonl"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("unclassifiedSchoolKindCounts", {"평생학교(고)-2년6학기": 8}),
        ("unclassifiedSchoolPolicySha256", "f" * 64),
    ],
)
def test_unclassified_provenance_tampering_fails_before_pointer_mutation(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="unclassified-tamper-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    original_pointer = (tmp_path / "current.json").read_bytes()
    policy = REVIEWED_NEIS_UNCLASSIFIED_POLICY
    records = (
        source_record(institution_id="neis:B10:ordinary"),
        *tuple(
            replace(
                source_record(institution_id=f"neis:B10:tamper-{index:02d}"),
                institution_type="UNCLASSIFIED_SCHOOL",
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
        ),
    )
    candidate = build_test_candidate(
        records=records,
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id=f"unclassified-tamper-{field_name}",
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0][field_name] = value
    sync_module._write_json(manifest_path, manifest)
    resign_candidate(candidate, tmp_path)

    with pytest.raises(SnapshotQualityError, match="unclassified"):
        sync_module.build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert (tmp_path / "current.json").read_bytes() == original_pointer


@pytest.mark.parametrize("tamper", ["missing", "extra", "unsorted"])
def test_unclassified_manifest_fields_are_strict_before_review(
    tmp_path: Path,
    tamper: str,
) -> None:
    policy = REVIEWED_NEIS_UNCLASSIFIED_POLICY
    records = tuple(
        replace(
            source_record(institution_id=f"neis:B10:fields-{index:02d}"),
            institution_type="UNCLASSIFIED_SCHOOL",
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
    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"unclassified-fields-{tamper}",
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest["sources"][0]
    if tamper == "missing":
        source.pop("unclassifiedSchoolPolicySha256")
    elif tamper == "extra":
        source["unclassifiedSchoolKindsRaw"] = ["must-not-persist"]
    else:
        source["unclassifiedSchoolKindCounts"] = dict(
            reversed(list(source["unclassifiedSchoolKindCounts"].items()))
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError, match="manifest fields|schema"):
        sync_module.build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize("target", ["institution", "site"])
def test_unclassified_active_record_tampering_fails_before_pointer_mutation(
    tmp_path: Path,
    target: str,
) -> None:
    policy = REVIEWED_NEIS_UNCLASSIFIED_POLICY
    records = (
        source_record(institution_id="neis:B10:ordinary"),
        *tuple(
            replace(
                source_record(institution_id=f"neis:B10:active-{index:02d}"),
                institution_type="UNCLASSIFIED_SCHOOL",
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
        ),
    )
    candidate = build_test_candidate(
        records=records,
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"unclassified-active-{target}",
    )
    pointer = tmp_path / "current.json"
    original_pointer = pointer.read_bytes() if pointer.exists() else None
    filename = "institutions.jsonl" if target == "institution" else "sites.jsonl"
    rows_path = candidate.candidate_path / filename
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
    ]
    row = next(
        item
        for item in rows
        if item.get("institutionType") == "UNCLASSIFIED_SCHOOL"
        or item.get("institutionId", "").startswith("neis:B10:active-")
    )
    row["status"] = "ACTIVE"
    rows[rows.index(row)] = row
    row_bytes = (
        "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in rows)
        + "\n"
    ).encode("utf-8")
    rows_path.write_bytes(row_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256" if target == "institution" else "sitesSha256"] = (
        hashlib.sha256(row_bytes).hexdigest()
    )
    if target == "institution":
        manifest["quarantinedCount"] -= 1
        manifest["countsByStatus"]["REVIEW_REQUIRED"] -= 1
        manifest["countsByStatus"]["ACTIVE"] += 1
    sync_module._write_json(manifest_path, manifest)
    resign_candidate(candidate, tmp_path)

    with pytest.raises(SnapshotQualityError, match="unclassified"):
        sync_module.build_candidate_review_packet(
            snapshot_id=candidate.snapshot_id,
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert (pointer.read_bytes() if pointer.exists() else None) == original_pointer


# Production break caught: collapsing a production-shaped mixed NEIS candidate's
# row dates or publishing a source entry that omits their measured distribution.
def test_mixed_vintage_neis_candidate_keeps_row_dates_and_manifest_histogram(
    tmp_path: Path,
) -> None:
    dates = (
        ["2026-04-23"] * 1_413
        + ["2026-05-17"]
        + ["2026-06-07"]
    )
    records = tuple(
        replace(
            source_record(institution_id=f"neis:B10:{7010000 + index}"),
            source_as_of=source_date,
        )
        for index, source_date in enumerate(dates)
    )
    provenance = replace(
        source_provenance_for(with_neis_quarantine(records))["NEIS"],
        page_count=2,
    )

    candidate = build_test_candidate(
        records=with_neis_quarantine(records),
        previous=None,
        output_root=tmp_path,
        snapshot_id="mixed-vintage-neis",
        source_provenance={"NEIS": provenance},
    )
    manifest = json.loads(
        (candidate.candidate_path / "manifest.json").read_text(encoding="utf-8")
    )
    neis = next(item for item in manifest["sources"] if item["source"] == "NEIS")

    assert neis["sourceAsOf"] is None
    assert neis["sourceObservationDateCounts"] == {
        "2026-04-23": 1_413,
        "2026-05-17": 1,
        "2026-06-07": 1,
        "2026-08-10": 18,
    }
    assert (
        neis["normalizedObservationDateCounts"]
        == neis["sourceObservationDateCounts"]
    )
    assert neis["preservedObservationDateCounts"] == {}
    persisted_dates = Counter(
        json.loads(line)["sourceAsOf"]
        for line in (candidate.candidate_path / "institutions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert persisted_dates == Counter(
        {
            "2026-04-23": 1_413,
            "2026-05-17": 1,
            "2026-06-07": 1,
            "2026-08-10": 18,
        }
    )


# Production break caught: a human-review artifact that changes between reads or
# exposes institution rows, source payload material, or the transaction secret.
def test_review_packet_is_deterministic_and_only_contains_safe_aggregates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_module.secrets,
        "token_bytes",
        lambda size: b"test-secret".ljust(size, b"!")[:size],
    )
    dates = (
        ["2026-04-23"] * 1_413
        + ["2026-05-17"]
        + ["2026-06-07"]
    )
    records = tuple(
        replace(
            source_record(institution_id=f"neis:B10:{7010000 + index}"),
            source_as_of=source_date,
        )
        for index, source_date in enumerate(dates)
    )
    provenance = replace(
        source_provenance_for(with_neis_quarantine(records))["NEIS"],
        page_count=2,
    )
    build_test_candidate(
        records=with_neis_quarantine(records),
        previous=None,
        output_root=tmp_path,
        snapshot_id="mixed-vintage-review",
        source_provenance={"NEIS": provenance},
    )

    first = sync_module.build_candidate_review_packet(
        snapshot_id="mixed-vintage-review",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    second = sync_module.build_candidate_review_packet(
        snapshot_id="mixed-vintage-review",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    assert first == second
    assert set(first) == {
        "status",
        "snapshotId",
        "createdAt",
        "snapshotAsOf",
        "previousSnapshotId",
        "sourceCounts",
            "sourceObservationDateCounts",
            "normalizedObservationDateCounts",
            "preservedObservationDateCounts",
            "unclassifiedSchoolKindCounts",
            "unclassifiedSchoolPolicySha256",
            "institutionTypeCounts",
        "foundationCounts",
        "districtCounts",
        "statusCounts",
        "coordinateQualityCounts",
        "quarantinedInstitutionIds",
        "quarantinedSiteIds",
        "diff",
        "institutionsSha256",
        "sitesSha256",
        "candidateManifestSha256",
        "sourceProvenanceSha256",
        "enrichmentProvenanceSha256",
        "reviewDigest",
    }
    assert first["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert first["normalizedObservationDateCounts"] == {
        "NEIS": {
            "2026-04-23": 1_413,
            "2026-05-17": 1,
            "2026-06-07": 1,
            "2026-08-10": 18,
        }
    }
    assert isinstance(first["reviewDigest"], str)
    assert re.fullmatch(r"[0-9a-f]{64}", first["reviewDigest"])
    digest_body = {key: value for key, value in first.items() if key != "reviewDigest"}
    assert first["reviewDigest"] == hashlib.sha256(
        json.dumps(
            digest_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert len(first["districtCounts"]) == 25
    assert not (tmp_path / ".promotion.lock").exists()
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "officialName",
        "roadAddress",
        "latitude",
        "longitude",
        "test-secret",
        "signature",
        "endpoint",
        "attribution",
        "rawSha256",
        "fetchedAt",
    ):
        assert forbidden not in serialized


# Production break caught: accepting a visually plausible but noncanonical review
# digest and mutating the selected snapshot without exact human authorization.
def test_approval_requires_exact_digest_role_and_unchanged_candidate(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="approval-contract",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="review digest"):
        sync_module.approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest="A" * 64,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    assert isinstance(packet["reviewDigest"], str)
    assert re.fullmatch(r"[0-9a-f]{64}", packet["reviewDigest"])
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize(
    "review_digest",
    ("a" * 63, "a" * 65, "g" * 64, "A" * 64, "a" * 63 + "\n"),
)
def test_approval_rejects_every_noncanonical_review_digest_without_mutation(
    tmp_path: Path,
    review_digest: str,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="invalid-review-digest",
    )

    with pytest.raises(SnapshotQualityError, match="review digest"):
        sync_module.approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=review_digest,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    assert not (tmp_path / "current.json").exists()
    assert not (tmp_path / ".promotion.lock").exists()


def test_approval_rejects_wrong_role_without_mutation(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="invalid-review-role",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )

    with pytest.raises(SnapshotQualityError, match="reviewer role"):
        sync_module.approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=packet["reviewDigest"],
            reviewer_role="operator",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    assert not (tmp_path / "current.json").exists()
    assert not (tmp_path / ".promotion.lock").exists()


def test_approval_rejects_candidate_changed_after_review_without_pointer_change(
    tmp_path: Path,
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="changed-after-review",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["createdAt"] = "2026-08-13T12:00:00Z"
    sync_module._write_json(manifest_path, manifest)
    transaction_path = (
        tmp_path / ".sync-transactions" / f"{candidate.snapshot_id}.json"
    )
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    transaction["manifestSha256"] = sync_module._manifest_section_sha256(manifest)
    transaction.pop("signature")
    sync_module._write_signed_transaction(
        tmp_path,
        transaction,
        replace_existing=True,
    )

    with pytest.raises(SnapshotQualityError, match="review digest"):
        sync_module.approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=packet["reviewDigest"],
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    assert (tmp_path / "current.json").read_bytes() == pointer_before


def test_same_digest_published_retry_is_idempotent(tmp_path: Path) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="same-digest-retry",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    review_digest = packet["reviewDigest"]
    assert isinstance(review_digest, str)

    assert sync_module.approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=review_digest,
        reviewer_role="data-steward",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    ) == review_digest
    pointer_before = (tmp_path / "current.json").read_bytes()

    assert sync_module.approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=review_digest,
        reviewer_role="data-steward",
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    ) == review_digest
    assert (tmp_path / "current.json").read_bytes() == pointer_before


# Production break caught: changing and reattesting a candidate after its digest
# comparison can otherwise publish data the reviewer did not approve.
def test_approval_rechecks_digest_after_post_comparison_candidate_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="digest-race-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="digest-race-candidate",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    review_digest = packet["reviewDigest"]
    assert isinstance(review_digest, str)
    real_compare_digest = sync_module.hmac.compare_digest
    changed = False

    def change_after_review_comparison(left: object, right: object) -> bool:
        nonlocal changed
        result = real_compare_digest(left, right)
        if not changed and left == review_digest and right == review_digest:
            manifest_path = candidate.candidate_path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["createdAt"] = "2099-01-01T00:00:00Z"
            sync_module._write_json(manifest_path, manifest)
            transaction_path = (
                tmp_path / ".sync-transactions" / f"{candidate.snapshot_id}.json"
            )
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
            transaction["manifestSha256"] = sync_module._manifest_section_sha256(
                manifest
            )
            transaction.pop("signature")
            sync_module._write_signed_transaction(
                tmp_path,
                transaction,
                replace_existing=True,
            )
            changed = True
        return result

    monkeypatch.setattr(
        sync_module.hmac,
        "compare_digest",
        change_after_review_comparison,
    )

    with pytest.raises(SnapshotQualityError, match="review digest"):
        sync_module.approve_candidate_snapshot(
            snapshot_id=candidate.snapshot_id,
            review_digest=review_digest,
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )

    rebuilt = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    assert changed is True
    assert rebuilt["reviewDigest"] != review_digest
    assert (tmp_path / "current.json").read_bytes() == pointer_before
    assert verify_snapshot(tmp_path).manifest.snapshot_id == baseline.snapshot_id


def test_automatic_promotion_symbol_is_not_public(
    tmp_path: Path,
) -> None:
    build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="automatic-promotion-disabled",
    )

    assert not hasattr(sync_module, "promote_snapshot")
    assert not (tmp_path / "current.json").exists()


def test_failed_reconciliation_emits_privacy_safe_audit_before_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = with_neis_quarantine(
        records_for_type_counts({"ELEMENTARY_SCHOOL": 602})
    )
    reconciliation = reconcile_selectable_school_counts(
        records,
        benchmark=reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 609}),
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    provenance = source_provenance_for(records)["NEIS"]
    audit = build_sync_preflight_audit(
        records,
        source_provenance={provenance.source: provenance},
        reconciliation=reconciliation,
    )

    with pytest.raises(
        SnapshotQualityError,
        match="official school count reconciliation failed",
    ):
        emit_sync_preflight_audit(audit)

    output = capsys.readouterr()
    parsed = json.loads(output.out)
    assert parsed["auditStage"] == "PRE_PROMOTION_RECONCILIATION"
    assert parsed["passed"] is False
    assert parsed["reconciliation"]["passed"] is False
    assert parsed["typeCounts"] == {
        "ELEMENTARY_SCHOOL": 602,
        "UNCLASSIFIED_SCHOOL": 18,
    }
    assert parsed["sourceCounts"]["NEIS"]["normalized"] == 620
    assert len(parsed["districtCounts"]) == 25
    assert "statusCounts" in parsed
    assert "quarantinedInstitutionIds" in parsed
    assert "quarantinedSiteIds" in parsed
    assert output.err == ""


@pytest.mark.asyncio
async def test_cli_reconciliation_failure_precedes_kakao_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location("sync_institutions_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    neis_records = records_for_type_counts({"ELEMENTARY_SCHOOL": 602})
    neis_provenance = source_provenance_for(neis_records)["NEIS"]
    empty_kindergarten_provenance = SourceProvenance(
        source="KINDERGARTEN_INFO",
        endpoint="https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do",
        license_name="PUBLIC_DATA_PORTAL_TERMS",
        attribution="Ministry of Education Kindergarten Info",
        fetched_at="2026-08-10T09:00:00Z",
        source_as_of=None,
        source_observation_date_counts=(),
        normalized_observation_date_counts=(),
        raw_sha256="b" * 64,
        page_count=25,
        row_count=0,
        fetched_row_count=0,
        request_region_code="11",
        request_timing="20261",
        normalized_sha256=normalized_records_sha256(()),
    )

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeNeisSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SourceFetchResult:
            return SourceFetchResult(neis_records, neis_provenance)

    class FakeKindergartenSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SourceFetchResult:
            return SourceFetchResult((), empty_kindergarten_provenance)

    class FakeStandardSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SimpleNamespace:
            return SimpleNamespace(
                locations=(),
                provenance=standard_enrichment_provenance(matched_row_count=0),
            )

    class ForbiddenKakaoClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("Kakao must not be created before reconciliation")

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "NeisSource", FakeNeisSource)
    monkeypatch.setattr(module, "KindergartenSource", FakeKindergartenSource)
    monkeypatch.setattr(module, "StandardSchoolLocationSource", FakeStandardSource)
    monkeypatch.setattr(module, "KakaoLocalClient", ForbiddenKakaoClient)
    monkeypatch.setattr(
        module,
        "load_reviewed_school_counts",
        lambda _path: reviewed_counts_fixture({"ELEMENTARY_SCHOOL": 609}),
    )
    monkeypatch.setattr(
        module,
        "reconcile_selectable_school_counts",
        lambda *_args, **_kwargs: {"passed": False},
    )
    snapshot_root = tmp_path / "snapshots"
    args = argparse.Namespace(
        sen_csv=SOURCE_RESOURCES / "sen-institutions.csv",
        region_codes=SOURCE_RESOURCES / "kindergarten-region-codes.csv",
        school_counts=SOURCE_RESOURCES / "sen-annual-school-counts.csv",
        neis_unclassified_policy=(
            SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
        ),
        snapshot_root=snapshot_root,
        geodata_root=Path("apps/travel-map/resources/geodata"),
        timing="20261",
        snapshot_id="must-not-build",
    )

    with pytest.raises(
        SnapshotQualityError,
        match="official school count reconciliation failed",
    ):
        await module._run_with_keys(
            args,
            {
                "NEIS_API_KEY": "neis-test",
                "KINDERGARTEN_API_KEY": "kindergarten-test",
                "KAKAO_REST_API_KEY": "kakao-test",
            },
            [],
        )

    audit = json.loads(capsys.readouterr().out)
    assert audit["reconciliation"]["passed"] is False
    assert audit["passed"] is False
    assert not snapshot_root.exists()


def test_sync_cli_defaults_to_the_reviewed_neis_unclassified_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location("sync_institutions_cli_args", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(script_path)])

    args = module.parse_args()

    assert args.neis_unclassified_policy == (
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )


@pytest.mark.asyncio
async def test_sync_cli_stops_at_candidate_review_without_pointer_or_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = Path("apps/travel-map/scripts/sync-institutions.py")
    spec = importlib.util.spec_from_file_location(
        "sync_institutions_candidate_cli",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not hasattr(module, "promote_snapshot")

    neis_records = (source_record(),)
    neis_provenance = source_provenance_for(neis_records)["NEIS"]
    loaded_policy = False

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            assert loaded_policy, "policy must load before network clients"

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeNeisSource:
        def __init__(
            self,
            *,
            unclassified_policy: NeisUnclassifiedPolicy,
            **_kwargs: object,
        ) -> None:
            assert unclassified_policy is REVIEWED_NEIS_UNCLASSIFIED_POLICY

        async def fetch(self) -> SourceFetchResult:
            return SourceFetchResult(neis_records, neis_provenance)

    class FakeKindergartenSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SimpleNamespace:
            return SimpleNamespace(
                records=(),
                provenance=SimpleNamespace(source="KINDERGARTEN_INFO"),
            )

    class FakeStandardSource:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def fetch(self) -> SimpleNamespace:
            return SimpleNamespace(
                locations=(),
                provenance=standard_enrichment_provenance(matched_row_count=0),
            )

    class FakeSenSource:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def load(self) -> SimpleNamespace:
            return SimpleNamespace(
                records=(),
                provenance=SimpleNamespace(source="SEN_REVIEWED_CSV"),
            )

    class FakeKakaoClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def clear_credentials(self) -> None:
            pass

        def provenance(self) -> EnrichmentProvenance:
            return EnrichmentProvenance(
                source="KAKAO_LOCAL_GEOCODING",
                endpoint="https://dapi.kakao.com/v2/local/search/address.json",
                license_name="KAKAO_LOCAL_API_TERMS",
                attribution="Kakao Local API",
                fetched_at="2026-08-13T09:00:00Z",
                source_as_of="2026-08-13",
                raw_sha256="c" * 64,
                normalized_sha256=hashlib.sha256(b"").hexdigest(),
                request_region_code="SEOUL_ADDRESS_BATCH",
                request_timing=None,
                page_count=0,
                fetched_row_count=0,
                matched_row_count=0,
                matched_normalized_sha256=hashlib.sha256(b"").hexdigest(),
            )

    snapshot_root = tmp_path / "snapshots"
    snapshot_id = "candidate-only-cli"

    def fake_build_candidate_snapshot(**_kwargs: object) -> SnapshotBuildResult:
        snapshot_root.mkdir()
        candidate_path = snapshot_root / f".{snapshot_id}.candidate"
        candidate_path.mkdir()
        return SnapshotBuildResult(
            snapshot_id=snapshot_id,
            candidate_path=candidate_path,
            approved=False,
            issues=(),
        )

    def forbidden_promotion(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("synchronizer must not auto-promote a candidate")

    async def identity_records(
        records: tuple[SourceInstitutionRecord, ...],
        _client: object,
    ) -> tuple[SourceInstitutionRecord, ...]:
        return records

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(module, "NeisSource", FakeNeisSource)
    monkeypatch.setattr(module, "KindergartenSource", FakeKindergartenSource)
    monkeypatch.setattr(module, "StandardSchoolLocationSource", FakeStandardSource)
    monkeypatch.setattr(module, "SenCsvSource", FakeSenSource)
    monkeypatch.setattr(module, "KakaoLocalClient", FakeKakaoClient)
    monkeypatch.setattr(module, "geocode_missing_records", identity_records)
    def load_policy(path: Path) -> NeisUnclassifiedPolicy:
        nonlocal loaded_policy
        assert path == (
            SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
        )
        loaded_policy = True
        return REVIEWED_NEIS_UNCLASSIFIED_POLICY

    monkeypatch.setattr(
        module,
        "load_neis_unclassified_policy",
        load_policy,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "load_reviewed_school_counts",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        module,
        "build_candidate_snapshot",
        fake_build_candidate_snapshot,
    )
    monkeypatch.setattr(module, "promote_snapshot", forbidden_promotion, raising=False)
    def reconcile(
        *_args: object,
        unclassified_policy: NeisUnclassifiedPolicy,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert unclassified_policy is REVIEWED_NEIS_UNCLASSIFIED_POLICY
        return {
            "passed": True,
            "unclassifiedSchoolKindCounts": dict(unclassified_policy.counts),
        }

    monkeypatch.setattr(module, "reconcile_selectable_school_counts", reconcile)
    monkeypatch.setattr(
        module,
        "build_sync_preflight_audit",
        lambda *_args, reconciliation, **_kwargs: {
            "passed": True,
            "reconciliation": reconciliation,
        },
    )
    monkeypatch.setattr(
        module,
        "emit_sync_preflight_audit",
        lambda audit: print(json.dumps(audit, sort_keys=True)),
    )
    args = argparse.Namespace(
        sen_csv=tmp_path / "sen.csv",
        region_codes=tmp_path / "regions.csv",
        school_counts=tmp_path / "counts.csv",
        neis_unclassified_policy=(
            SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
        ),
        snapshot_root=snapshot_root,
        geodata_root=Path("apps/travel-map/resources/geodata"),
        timing="20261",
        snapshot_id=snapshot_id,
    )

    await module._run_with_keys(
        args,
        {
            "NEIS_API_KEY": "neis-test",
            "KINDERGARTEN_API_KEY": "kindergarten-test",
            "KAKAO_REST_API_KEY": "kakao-test",
        },
        [],
    )

    lines = capsys.readouterr().out.splitlines()
    preflight = json.loads(lines[0])
    assert preflight["reconciliation"]["unclassifiedSchoolKindCounts"] == dict(
        REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts
    )
    assert lines[-1] == (
        '{"snapshotId":"candidate-only-cli",'
        '"status":"CANDIDATE_REVIEW_REQUIRED"}'
    )
    assert (snapshot_root / ".candidate-only-cli.candidate").is_dir()
    assert not (snapshot_root / "current.json").exists()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2",
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
    source_types = (
        "\ucd08\ub4f1\ud559\uad50",
        "\ucd08\ub4f1\ud559\uad50",
        *reviewed_neis_source_types(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["pIndex"])
        return httpx.Response(
            200,
            json=neis_page_payload(
                source_types,
                page=page,
                page_size=10,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        source = NeisSource(
            api_key="test-key",
            client=client,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            page_size=10,
        )
        result = await source.fetch()

    assert len(result.records) == 20
    assert result.provenance.page_count == 2
    assert [request.url.params["pIndex"] for request in requests] == ["1", "2"]
    assert all(request.url.params["ATPT_OFCDC_SC_CODE"] == "B10" for request in requests)

    with pytest.raises(SourceDataError, match="NEIS_API_KEY"):
        NeisSource(
            api_key="",
            client=httpx.AsyncClient(),
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )


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
            await NeisSource(
                api_key=secret,
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

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
            await NeisSource(
                api_key=secret,
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

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
            await NeisSource(
                api_key=secret,
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

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
        return httpx.Response(
            200,
            json=neis_payload_rows(
                "\ucd08\ub4f1\ud559\uad50",
                *reviewed_neis_source_types(),
            ),
        )

    def kindergarten_handler(request: httpx.Request) -> httpx.Response:
        payload = kindergarten_payload()
        payload["sggList"] = request.url.params["sggCode"]
        row = payload["kinderInfo"][0]  # type: ignore[index]
        row["kinderCode"] = f"K{request.url.params['sggCode']}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(neis_handler)
    ) as client:
        neis = NeisSource(
            api_key=neis_secret,
            client=client,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
        )
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
        payload = neis_payload_rows(
            "\ucd08\ub4f1\ud559\uad50",
            *reviewed_neis_source_types(),
            "\uacf5\ub3d9\uc2e4\uc2b5\uc18c",
        )
        sections = payload["schoolInfo"]
        assert type(sections) is list
        for row in sections[1]["row"][:-1]:
            row["LOAD_DTM"] = "20260423"
        sections[1]["row"][-1]["LOAD_DTM"] = "20260607"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            page_size=20,
        ).fetch()

    assert len(result.records) == 19
    assert result.provenance.page_count == 1
    assert result.provenance.fetched_row_count == 20
    assert result.provenance.row_count == 19
    assert result.provenance.source_as_of is None
    assert result.provenance.source_observation_date_counts == (
        ("2026-04-23", 19),
        ("2026-06-07", 1),
    )
    assert result.provenance.normalized_observation_date_counts == (
        ("2026-04-23", 19),
    )


@pytest.mark.asyncio
async def test_neis_fetch_records_raw_and_normalized_mixed_vintage_histograms() -> None:
    source_types = (*reviewed_neis_source_types(), *("\ucd08\ub4f1\ud559\uad50",) * 4)

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_page_payload(source_types, page=page, page_size=11)
        sections = payload["schoolInfo"]
        assert type(sections) is list
        rows = sections[1]["row"]
        for index, row in enumerate(rows):
            source_index = (page - 1) * 11 + index
            row["LOAD_DTM"] = (
                "20260517"
                if source_index == 1
                else "20260607" if source_index == 21 else "20260423"
            )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            page_size=11,
        ).fetch()

    assert result.provenance.source_as_of is None
    assert result.provenance.source_observation_date_counts == (
        ("2026-04-23", 20),
        ("2026-05-17", 1),
        ("2026-06-07", 1),
    )
    assert result.provenance.normalized_observation_date_counts == (
        ("2026-04-23", 20),
        ("2026-05-17", 1),
        ("2026-06-07", 1),
    )


@pytest.mark.asyncio
async def test_neis_source_preserves_different_date_on_excluded_only_page() -> None:
    source_types = (
        "\ucd08\ub4f1\ud559\uad50",
        *reviewed_neis_source_types(),
        "\uacf5\ub3d9\uc2e4\uc2b5\uc18c",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pIndex"])
        payload = neis_page_payload(source_types, page=page, page_size=10)
        section = payload["schoolInfo"]
        rows = section[1]["row"]  # type: ignore[index]
        for index, row in enumerate(rows):
            row["LOAD_DTM"] = (
                "20260809"
                if (page - 1) * 10 + index == len(source_types) - 1
                else "20260810"
            )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await NeisSource(
            api_key="test-key",
            client=client,
            unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            page_size=10,
        ).fetch()

    assert result.provenance.source_as_of is None
    assert result.provenance.source_observation_date_counts == (
        ("2026-08-09", 1),
        ("2026-08-10", 19),
    )
    assert result.provenance.normalized_observation_date_counts == (
        ("2026-08-10", 19),
    )


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
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

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
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
                page_size=1,
            ).fetch()

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
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()


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
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
            ).fetch()

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
            await NeisSource(
                api_key="test-key",
                client=client,
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
                page_size=1,
            ).fetch()


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
                unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
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

    official_institutions = tuple(
        institution
        for institution in verified.institutions
        if institution.institution_type != "UNCLASSIFIED_SCHOOL"
    )
    official_sites = tuple(
        site
        for site in verified.sites
        if site.institution_id == "neis:B10:7010001"
    )

    assert len(official_institutions) == 1
    assert {site.site_id for site in official_sites} == {
        "neis:B10:7010001:main",
        "neis:B10:7010001:gayang",
    }
    assert sum(site.is_default for site in official_sites) == 1


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

    assert {
        item.institution_id
        for item in verified.institutions
        if item.institution_type != "UNCLASSIFIED_SCHOOL"
    } == {
        "neis:B10:7010001",
        "kinder:K12345678",
    }
    assert (
        "kinder:K12345678",
        "neis:B10:7010001",
    ) in {match.institution_ids for match in verified.manifest.possible_matches}


def test_candidate_rejects_mixed_row_dates_with_single_date_provenance_before_writing(
    tmp_path: Path,
) -> None:
    first = source_record(institution_id="neis:B10:7010001")
    second = SourceInstitutionRecord(
        **{
            **source_record(institution_id="neis:B10:7010002").__dict__,
            "source_as_of": "2026-08-09",
        }
    )

    records = with_neis_quarantine((first, second))
    provenance = replace(
        source_provenance_for(records)["NEIS"],
        source_as_of="2026-08-10",
        source_observation_date_counts=(("2026-08-10", 20),),
        normalized_observation_date_counts=(("2026-08-10", 20),),
    )

    with pytest.raises(SnapshotQualityError, match="observation dates"):
        build_test_candidate(
            records=records,
            previous=None,
            output_root=tmp_path,
            snapshot_id="mixed-source-dates",
            coverage=TEST_COVERAGE,
            source_provenance={"NEIS": provenance},
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
        if row["institutionId"].rsplit(":", 1)[-1].isdigit()
        and int(row["institutionId"].rsplit(":", 1)[-1]) >= 90
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
    first_packet = sync_module.build_candidate_review_packet(
        snapshot_id=first.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    second_packet = sync_module.build_candidate_review_packet(
        snapshot_id=second.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    real_packet_builder = sync_module.build_candidate_review_packet
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def controlled_packet_builder(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["snapshot_id"] == first.snapshot_id:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return real_packet_builder(*args, **kwargs)

    monkeypatch.setattr(
        sync_module,
        "build_candidate_review_packet",
        controlled_packet_builder,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            sync_module.approve_candidate_snapshot,
            snapshot_id=first.snapshot_id,
            review_digest=first_packet["reviewDigest"],
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
        assert first_entered.wait(timeout=2)
        second_future = executor.submit(
            sync_module.approve_candidate_snapshot,
            snapshot_id=second.snapshot_id,
            review_digest=second_packet["reviewDigest"],
            reviewer_role="data-steward",
            snapshot_root=tmp_path,
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
    assert manifest["quarantinedCount"] == 19
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

    assert manifest["quarantinedCount"] == 19
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

    official_rows = [
        json.loads(row)
        for row in rows
        if json.loads(row)["institutionType"] != "UNCLASSIFIED_SCHOOL"
    ]
    assert len(official_rows) == 2
    assert (
        "kinder:verified-kindergarten",
        "neis:B10:7010001",
    ) in {
        tuple(match["institutionIds"])
        for match in manifest["possibleMatches"]
    }


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
    _, site_bytes = replace_jsonl_record(
        sites_path,
        field="siteId",
        value="neis:B10:7010001:main",
        updates={
            "latitude": 35.1796,
            "longitude": 129.0756,
            "routingAnchorLatitude": 35.1796,
            "routingAnchorLongitude": 129.0756,
        },
    )
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

    with pytest.raises(SnapshotQualityError, match="transaction|candidate"):
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


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        (
            "sourceObservationDateCounts",
            {"2026-04-23": 3, "2026-05-17": 1, "2026-06-07": 1},
        ),
        (
            "normalizedObservationDateCounts",
            {"2026-04-23": 1, "2026-05-17": 1, "2026-06-07": 1},
        ),
        ("preservedObservationDateCounts", {"2026-04-23": 1}),
    ],
)
def test_promotion_rejects_tampered_observation_date_count_before_pointer_change(
    tmp_path: Path,
    field_name: str,
    value: dict[str, int],
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="histogram-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    records = mixed_neis_records()
    candidate = build_test_candidate(
        records=records,
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id=f"tampered-{field_name}",
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0][field_name] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)

    assert (tmp_path / "current.json").read_bytes() == pointer_before


# Production break caught: accepting a persisted row date that differs from the
# signed normalized histogram during the final promotion recheck.
def test_promotion_rejects_tampered_observation_date_before_pointer_change(
    tmp_path: Path,
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="date-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    candidate = build_test_candidate(
        records=mixed_neis_records(),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="tampered-row-date",
    )
    institutions_path = candidate.candidate_path / "institutions.jsonl"
    lines = institutions_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["sourceAsOf"] = "2026-04-24"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    institution_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    institutions_path.write_bytes(institution_bytes)
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="observation dates"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)

    assert (tmp_path / "current.json").read_bytes() == pointer_before


# Production break caught: accepting equivalent histogram keys in a noncanonical
# order after raw JSON tampering.
def test_promotion_rejects_unsorted_observation_date_keys_before_pointer_change(
    tmp_path: Path,
) -> None:
    baseline = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="order-baseline",
    )
    promote_snapshot(baseline, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    candidate = build_test_candidate(
        records=mixed_neis_records(),
        previous=verify_snapshot(tmp_path),
        output_root=tmp_path,
        snapshot_id="tampered-key-order",
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    canonical = (
        '"sourceObservationDateCounts":{"2026-04-23":2,'
        '"2026-05-17":1,"2026-06-07":1,"2026-08-10":18}'
    )
    unsorted = (
        '"sourceObservationDateCounts":{"2026-06-07":1,'
        '"2026-04-23":2,"2026-05-17":1,"2026-08-10":18}'
    )
    assert canonical in manifest_text
    manifest_path.write_text(
        manifest_text.replace(canonical, unsorted, 1),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)

    assert (tmp_path / "current.json").read_bytes() == pointer_before


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
    _, site_bytes = replace_jsonl_record(
        sites_path,
        field="siteId",
        value="neis:B10:7010001:main",
        updates={
            "roadAddress": "서울특별시 송파구 변조로 10",
            "district": "송파구",
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        },
    )
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


def test_public_result_cannot_authorize_replaced_raw_provenance(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="forged-public-attestations",
        coverage=TEST_COVERAGE,
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["rawSha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    forged = replace(candidate, issues=())

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
        promote_snapshot(forged, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_builder_writes_private_root_transaction_without_source_pii(
    tmp_path: Path,
) -> None:
    record = source_record()
    candidate = build_test_candidate(
        records=(record,),
        previous=None,
        output_root=tmp_path,
        snapshot_id="durable-transaction",
        coverage=TEST_COVERAGE,
    )
    key_path = tmp_path / ".sync-attestation.key"
    receipt_path = tmp_path / ".sync-transactions" / "durable-transaction.json"

    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert receipt_path.is_file()
    assert candidate.candidate_path not in receipt_path.parents
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert record.official_name not in receipt_text
    assert record.road_address not in receipt_text


def test_builder_fsyncs_existing_root_before_durable_transaction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="transaction-bootstrap",
        coverage=TEST_COVERAGE,
    )
    events: list[tuple[str, Path]] = []
    real_fsync_directory = sync_module._fsync_directory
    real_replace = os.replace

    def record_fsync(path: Path) -> None:
        events.append(("fsync", Path(path)))
        real_fsync_directory(path)

    def record_replace(source: str | Path, destination: str | Path) -> None:
        destination_path = Path(destination)
        if destination_path.parent.name == ".sync-transactions":
            events.append(("receipt-replace", destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(sync_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    second = build_test_candidate(
        records=(source_record(institution_id="neis:B10:7010002"),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="transaction-after-bootstrap",
        coverage=TEST_COVERAGE,
    )

    receipt_index = next(
        index for index, event in enumerate(events) if event[0] == "receipt-replace"
    )
    candidate_fsync_index = events.index(("fsync", second.candidate_path))
    root_fsync_indices = [
        index
        for index, event in enumerate(events)
        if event == ("fsync", tmp_path.resolve())
    ]
    assert candidate_fsync_index < root_fsync_indices[-1] < receipt_index


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_promotion_rejects_missing_or_tampered_build_transaction(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id=f"{mutation}-transaction",
        coverage=TEST_COVERAGE,
    )
    receipt_path = (
        tmp_path / ".sync-transactions" / f"{candidate.snapshot_id}.json"
    )
    if mutation == "missing":
        receipt_path.unlink()
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["manifestSha256"] = "f" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="transaction"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_build_transaction_cannot_be_copied_between_output_roots(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=first_root,
        snapshot_id="copied-transaction",
        coverage=TEST_COVERAGE,
    )
    second = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=second_root,
        snapshot_id="copied-transaction",
        coverage=TEST_COVERAGE,
    )
    first_receipt = (
        first_root / ".sync-transactions" / "copied-transaction.json"
    )
    second_receipt = (
        second_root / ".sync-transactions" / "copied-transaction.json"
    )
    second_receipt.write_bytes(first_receipt.read_bytes())

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
        promote_snapshot(second, second_root, coverage=TEST_COVERAGE)
    assert not (second_root / "current.json").exists()
    assert first.candidate_path.is_dir()


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
    _, site_bytes = replace_jsonl_record(
        sites_path,
        field="siteId",
        value="neis:B10:7010001:main",
        updates={
            "latitude": 37.51,
            "longitude": 127.10,
            "routingAnchorLatitude": 37.51,
            "routingAnchorLongitude": 127.10,
        },
    )
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
    _, institution_bytes = replace_jsonl_record(
        institutions_path,
        field="institutionId",
        value="neis:B10:7010001",
        updates={"lastSeenSnapshot": "other-snapshot"},
    )
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(institution_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="transaction attestation"):
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
    records = with_neis_quarantine((official_record,))
    provenance = replace(
        source_provenance_for(records)["NEIS"],
        raw_sha256="b" * 64,
        page_count=2,
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
        records=records,
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
    assert manifest["sources"][0]["fetchedRowCount"] == 19
    assert manifest["sources"][0]["normalizedRowCount"] == 19
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
        ("source_observation_date_counts", ()),
        ("source_observation_date_counts", (("2026-08-10", 0),)),
        (
            "source_observation_date_counts",
            (("2026-08-10", 1), ("2026-08-09", 1)),
        ),
        (
            "source_observation_date_counts",
            (("2026-08-10", 1), ("2026-08-10", 1)),
        ),
        ("normalized_observation_date_counts", (("2026-08-09", 1),)),
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


def test_pointer_failure_restart_uses_durable_transaction_not_result_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="restart-from-transaction",
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
    snapshot_id = candidate.snapshot_id
    candidate_path = candidate.candidate_path
    del candidate
    restarted = SnapshotBuildResult(
        snapshot_id=snapshot_id,
        candidate_path=candidate_path,
        approved=False,
        issues=(),
    )

    promote_snapshot(restarted, tmp_path, coverage=TEST_COVERAGE)
    assert verify_snapshot(tmp_path).manifest.snapshot_id == snapshot_id


def test_restart_after_pointer_fsync_before_published_phase_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="pointer-written-before-phase",
        coverage=TEST_COVERAGE,
    )
    real_advance = sync_module._advance_build_transaction

    def fail_published_phase(
        root: Path,
        transaction: Mapping[str, object],
        *,
        phase: str,
        approved_manifest: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if phase == "PUBLISHED":
            raise OSError("simulated crash before published receipt")
        return real_advance(
            root,
            transaction,
            phase=phase,
            approved_manifest=approved_manifest,
        )

    monkeypatch.setattr(
        sync_module,
        "_advance_build_transaction",
        fail_published_phase,
    )
    with pytest.raises(OSError, match="before published receipt"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    pointer_before = (tmp_path / "current.json").read_bytes()
    receipt_path = (
        tmp_path
        / ".sync-transactions"
        / "pointer-written-before-phase.json"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["phase"] == (
        "POINTER_PREPARED"
    )
    monkeypatch.setattr(
        sync_module,
        "_advance_build_transaction",
        real_advance,
    )
    restarted = SnapshotBuildResult(
        snapshot_id=candidate.snapshot_id,
        candidate_path=candidate.candidate_path,
        approved=False,
        issues=(),
    )

    promote_snapshot(restarted, tmp_path, coverage=TEST_COVERAGE)

    assert (tmp_path / "current.json").read_bytes() == pointer_before
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["phase"] == (
        "PUBLISHED"
    )
    assert verify_snapshot(tmp_path).manifest.snapshot_id == candidate.snapshot_id


def test_published_transaction_cannot_authorize_a_different_current_pointer(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="published-pointer-binding",
        coverage=TEST_COVERAGE,
    )
    promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    (tmp_path / "current.json").write_text(
        json.dumps({"snapshotId": "different-snapshot"}),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotQualityError, match="pointer|current snapshot"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)

    assert json.loads((tmp_path / "current.json").read_text(encoding="utf-8")) == {
        "snapshotId": "different-snapshot"
    }


def test_pointer_failure_rejects_changed_attested_approval_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="changed-approved-at",
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
    manifest["approvedAt"] = "2099-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approval phase"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


def test_forged_approved_final_without_attested_phase_is_rejected(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="forged-approved-final",
        coverage=TEST_COVERAGE,
    )
    final_path = tmp_path / candidate.snapshot_id
    os.replace(candidate.candidate_path, final_path)
    manifest_path = final_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "approved": True,
            "approvedAt": manifest["createdAt"],
            "approvedByRole": "data-steward",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotQualityError, match="approval phase"):
        promote_snapshot(candidate, tmp_path, coverage=TEST_COVERAGE)
    assert not (tmp_path / "current.json").exists()


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


def _run_snapshot_admin_script(
    script_name: str,
    *arguments: str,
    secret: str = "administrator-key-fixture-must-not-appear",
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "apps/travel-map",
        "NEIS_API_KEY": secret,
        "KINDERGARTEN_API_KEY": secret,
        "KAKAO_REST_API_KEY": secret,
    }
    return subprocess.run(
        [
            sys.executable,
            f"apps/travel-map/scripts/{script_name}",
            *arguments,
        ],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


# Production break caught: review accidentally initializes a credential/network
# dependency or emits anything other than the one deterministic review packet.
def test_review_cli_uses_no_credentials_and_prints_one_compact_packet(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="review-cli-contract",
    )

    completed = _run_snapshot_admin_script(
        "review-institution-snapshot.py",
        "--snapshot-id",
        candidate.snapshot_id,
        "--snapshot-root",
        str(tmp_path),
        "--geodata-root",
        "apps/travel-map/resources/geodata",
    )

    assert completed.returncode == 0
    packet = json.loads(completed.stdout)
    assert packet["snapshotId"] == candidate.snapshot_id
    assert packet["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert re.fullmatch(r"[0-9a-f]{64}", packet["reviewDigest"])
    assert completed.stdout == (
        json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert completed.stderr == ""
    assert "administrator-key-fixture-must-not-appear" not in (
        completed.stdout + completed.stderr
    )


@pytest.mark.parametrize(
    "script_name",
    (
        "review-institution-snapshot.py",
        "approve-institution-snapshot.py",
    ),
)
def test_review_and_approval_clis_redact_rejected_argument_values(
    script_name: str,
) -> None:
    required_arguments = ["--snapshot-id", "credential-argument-check"]
    if script_name.startswith("approve-"):
        required_arguments.extend(
            [
                "--review-digest",
                "a" * 64,
                "--reviewer-role",
                "data-steward",
            ]
        )
    completed = _run_snapshot_admin_script(
        script_name,
        *required_arguments,
        "--env-file",
        "administrator-key-fixture-must-not-appear",
    )

    assert completed.returncode == 2
    assert "invalid command arguments" in completed.stderr
    assert "administrator-key-fixture-must-not-appear" not in (
        completed.stdout + completed.stderr
    )


@pytest.mark.parametrize(
    "script_name",
    (
        "review-institution-snapshot.py",
        "approve-institution-snapshot.py",
    ),
)
@pytest.mark.parametrize(
    "rejected_arguments",
    (
        ("--env-file", "rejected-unknown-option-secret-must-not-appear"),
        ("rejected-positional-secret-must-not-appear",),
    ),
)
def test_snapshot_admin_cli_parse_errors_do_not_retain_rejected_values_in_app_or_script_frames(
    script_name: str,
    rejected_arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = rejected_arguments[-1]
    required_arguments = ["--snapshot-id", "redacted-argument-check"]
    if script_name.startswith("approve-"):
        required_arguments.extend(
            [
                "--review-digest",
                "a" * 64,
                "--reviewer-role",
                "data-steward",
            ]
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            f"apps/travel-map/scripts/{script_name}",
            *required_arguments,
            *rejected_arguments,
        ],
    )

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            f"apps/travel-map/scripts/{script_name}",
            run_name="__main__",
        )

    assert raised.value.code == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert_secret_absent_from_app_traceback(
        raised.value,
        raised.value.__traceback__,
        secret,
    )


# Production break caught: approval prints the wrong status/digest or publishes
# without routing the reviewed digest through approve_candidate_snapshot().
def test_approval_cli_prints_exact_safe_success_record(
    tmp_path: Path,
) -> None:
    candidate = build_test_candidate(
        records=(source_record(),),
        previous=None,
        output_root=tmp_path,
        snapshot_id="approval-cli-contract",
    )
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=tmp_path,
        coverage=TEST_COVERAGE,
    )
    review_digest = packet["reviewDigest"]
    assert isinstance(review_digest, str)

    completed = _run_snapshot_admin_script(
        "approve-institution-snapshot.py",
        "--snapshot-id",
        candidate.snapshot_id,
        "--review-digest",
        review_digest,
        "--reviewer-role",
        "data-steward",
        "--snapshot-root",
        str(tmp_path),
        "--geodata-root",
        "apps/travel-map/resources/geodata",
    )

    expected = {
        "reviewDigest": review_digest,
        "snapshotId": candidate.snapshot_id,
        "status": "SNAPSHOT_APPROVED",
    }
    assert completed.returncode == 0
    assert completed.stdout == (
        json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert completed.stderr == ""
    assert "administrator-key-fixture-must-not-appear" not in (
        completed.stdout + completed.stderr
    )
    assert json.loads((tmp_path / "current.json").read_text(encoding="utf-8")) == {
        "snapshotId": candidate.snapshot_id
    }


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


def neis_payload_rows(*source_types: str) -> dict[str, object]:
    payload = neis_payload(source_type="초등학교")
    section = payload["schoolInfo"]
    assert type(section) is list
    template = section[1]["row"][0]
    assert type(template) is dict
    section[0]["head"][0]["list_total_count"] = len(source_types)
    section[1]["row"] = [
        {
            **template,
            "SD_SCHUL_CODE": f"{7010000 + index}",
            "SCHUL_KND_SC_NM": source_type,
        }
        for index, source_type in enumerate(source_types, start=1)
    ]
    return payload


def reviewed_neis_source_types() -> tuple[str, ...]:
    return tuple(
        label
        for label, count in REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts
        for _ in range(count)
    )


def neis_page_payload(
    source_types: tuple[str, ...],
    *,
    page: int,
    page_size: int,
) -> dict[str, object]:
    offset = (page - 1) * page_size
    payload = neis_payload_rows(*source_types[offset : offset + page_size])
    section = payload["schoolInfo"]
    assert type(section) is list
    section[0]["head"][0]["list_total_count"] = len(source_types)
    rows = section[1]["row"]
    for index, row in enumerate(rows, start=offset + 1):
        row["SD_SCHUL_CODE"] = f"{7010000 + index}"
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
    include_neis_quarantine: bool = True,
) -> SnapshotBuildResult:
    if (
        include_neis_quarantine
        and any(record.source == "NEIS" for record in records)
        and not any(
            record.institution_type == "UNCLASSIFIED_SCHOOL" for record in records
        )
    ):
        records = (*records, *quarantined_neis_records("neis:B10:test-quarantine"))
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


def promote_snapshot(
    candidate: SnapshotBuildResult,
    output_root: Path,
    *,
    coverage: CoverageService,
) -> str:
    packet = sync_module.build_candidate_review_packet(
        snapshot_id=candidate.snapshot_id,
        snapshot_root=output_root,
        coverage=coverage,
    )
    review_digest = packet["reviewDigest"]
    assert isinstance(review_digest, str)
    return sync_module.approve_candidate_snapshot(
        snapshot_id=candidate.snapshot_id,
        review_digest=review_digest,
        reviewer_role="data-steward",
        snapshot_root=output_root,
        coverage=coverage,
    )


def resign_candidate(candidate: SnapshotBuildResult, snapshot_root: Path) -> None:
    manifest_path = candidate.candidate_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transaction_path = (
        snapshot_root / ".sync-transactions" / f"{candidate.snapshot_id}.json"
    )
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    unapproved = dict(manifest)
    unapproved["approved"] = False
    unapproved["approvedAt"] = None
    unapproved["approvedByRole"] = None
    transaction["manifestSha256"] = sync_module._manifest_section_sha256(unapproved)
    transaction["sourcesSha256"] = sync_module._manifest_section_sha256(
        manifest["sources"]
    )
    transaction["enrichmentsSha256"] = sync_module._manifest_section_sha256(
        manifest["enrichments"]
    )
    transaction["institutionsSha256"] = manifest["institutionsSha256"]
    transaction["sitesSha256"] = manifest["sitesSha256"]
    transaction["previousSnapshotId"] = manifest["diff"]["previousSnapshotId"]
    transaction.pop("signature")
    sync_module._write_signed_transaction(
        snapshot_root,
        transaction,
        replace_existing=True,
    )


def quarantined_neis_records(prefix: str) -> tuple[SourceInstitutionRecord, ...]:
    return tuple(
        replace(
            source_record(institution_id=f"{prefix}-{index:02d}"),
            institution_type="UNCLASSIFIED_SCHOOL",
            source_kind_label=label,
        )
        for index, label in enumerate(
            (
                label
                for label, count in REVIEWED_NEIS_UNCLASSIFIED_POLICY.counts
                for _ in range(count)
            ),
            start=1,
        )
    )


def with_neis_quarantine(
    records: tuple[SourceInstitutionRecord, ...],
) -> tuple[SourceInstitutionRecord, ...]:
    if not any(record.source == "NEIS" for record in records) or any(
        record.institution_type == "UNCLASSIFIED_SCHOOL" for record in records
    ):
        return records
    return (*records, *quarantined_neis_records("neis:B10:reconciliation-quarantine"))


def replace_jsonl_record(
    path: Path,
    *,
    field: str,
    value: str,
    updates: Mapping[str, object],
) -> tuple[dict[str, object], bytes]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    record = next(row for row in rows if row[field] == value)
    record.update(updates)
    contents = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    result = contents.encode()
    path.write_bytes(result)
    return record, result


def remove_unclassified_rows(
    snapshot_path: Path,
    *,
    resign_root: Path | None = None,
    candidate: SnapshotBuildResult | None = None,
) -> None:
    institution_path = snapshot_path / "institutions.jsonl"
    site_path = snapshot_path / "sites.jsonl"
    institutions = [
        Institution.model_validate_json(line)
        for line in institution_path.read_text(encoding="utf-8").splitlines()
    ]
    quarantined_ids = {
        institution.institution_id
        for institution in institutions
        if institution.institution_type == "UNCLASSIFIED_SCHOOL"
    }
    institutions = [
        institution
        for institution in institutions
        if institution.institution_id not in quarantined_ids
    ]
    sites = [
        InstitutionSite.model_validate_json(line)
        for line in site_path.read_text(encoding="utf-8").splitlines()
    ]
    sites = [site for site in sites if site.institution_id not in quarantined_ids]
    institution_path.write_bytes(sync_module._jsonl_bytes(institutions))
    site_path.write_bytes(sync_module._jsonl_bytes(sites))
    sites_by_parent = {institution.institution_id: [] for institution in institutions}
    for site in sites:
        sites_by_parent[site.institution_id].append(site)
    manifest_path = snapshot_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["institutionsSha256"] = hashlib.sha256(
        institution_path.read_bytes()
    ).hexdigest()
    manifest["sitesSha256"] = hashlib.sha256(site_path.read_bytes()).hexdigest()
    manifest["institutionCount"] = len(institutions)
    manifest["siteCount"] = len(sites)
    manifest["quarantinedCount"] = sum(
        institution.status is InstitutionStatus.REVIEW_REQUIRED
        for institution in institutions
    )
    manifest["countsByType"] = dict(
        Counter(institution.institution_type for institution in institutions)
    )
    manifest["countsByFoundation"] = dict(
        Counter(institution.foundation_type for institution in institutions)
    )
    manifest["countsByStatus"] = dict(
        Counter(institution.status.value for institution in institutions)
    )
    manifest["coordinateQualityCounts"] = dict(
        Counter(site.coordinate_quality for site in sites)
    )
    source = manifest["sources"][0]
    source["normalizedObservationDateCounts"] = dict(
        observation_date_counts(institution.source_as_of for institution in institutions)
    )
    source["normalizedRowCount"] = len(institutions)
    source["preservedRowCount"] = 0
    source["rowCount"] = len(institutions)
    source["sourceNormalizedSha256"] = (
        sync_module._normalized_persisted_source_sha256(
            institutions,
            sites_by_parent,
            before_enrichment=True,
        )
    )
    source["normalizedSha256"] = sync_module._normalized_persisted_source_sha256(
        institutions,
        sites_by_parent,
    )
    source["unclassifiedSchoolKindCounts"] = {}
    source["unclassifiedSchoolPolicySha256"] = None
    sync_module._write_json(manifest_path, manifest)
    if resign_root is not None and candidate is not None:
        resign_candidate(candidate, resign_root)


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
            source_as_of=source_as_of_for(
                observation_date_counts(
                    record.source_as_of for record in source_records
                )
            ),
            source_observation_date_counts=(
                (
                    source_records[0].source_as_of,
                    len(source_records) + 1,
                ),
            )
            if source == "SEN_REVIEWED_CSV"
            else observation_date_counts(
                record.source_as_of for record in source_records
            ),
            normalized_observation_date_counts=observation_date_counts(
                record.source_as_of for record in source_records
            ),
            raw_sha256=(
                "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2"
                if source == "SEN_REVIEWED_CSV"
                else "a" * 64
            ),
            page_count=(25 if source == "KINDERGARTEN_INFO" else 1),
            row_count=len(source_records),
            fetched_row_count=(
                len(source_records) + 1
                if source == "SEN_REVIEWED_CSV"
                else len(source_records)
            ),
            request_region_code=regions[source],
            request_timing=(
                "20261" if source == "KINDERGARTEN_INFO" else None
            ),
            normalized_sha256=normalized_records_sha256(
                [_before_enrichment(record) for record in source_records]
            ),
            unclassified_school_kind_counts=(
                tuple(
                    sorted(
                        Counter(
                            record.source_kind_label
                            for record in source_records
                            if record.source_kind_label is not None
                        ).items()
                    )
                )
                if source == "NEIS"
                else ()
            ),
            unclassified_school_policy_sha256=(
                PINNED_POLICY_SHA256
                if source == "NEIS"
                and any(
                    record.source_kind_label is not None
                    for record in source_records
                )
                else None
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


def reviewed_counts_fixture(counts: Mapping[str, int]) -> ReviewedSchoolCounts:
    evidence = SchoolCountEvidence(
        source_url=(
            "https://enews.sen.go.kr/uploads/img_smart//"
            "2026-06-08/20260608075519432.png"
        ),
        source_as_of="2026-03-10",
        source_sha256=(
            "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70"
        ),
        status="PRELIMINARY_2026",
    )
    return ReviewedSchoolCounts(
        normalized_sha256="a" * 64,
        license_name="KOGL_TYPE_1_ATTRIBUTION",
        attribution="Source: Seoul Metropolitan Office of Education",
        counts=dict(counts),
        category_evidence={name: evidence for name in counts},
        category_composition={name: name for name in counts},
        reported_totals=(
            ReportedSchoolTotal(
                expected_count=sum(counts.values()),
                population="+".join(counts),
                used_for_gate=False,
                evidence=evidence,
            ),
        ),
    )


def records_for_type_counts(
    counts: Mapping[str, int],
) -> tuple[SourceInstitutionRecord, ...]:
    records: list[SourceInstitutionRecord] = []
    sequence = 1
    for institution_type, count in counts.items():
        for _ in range(count):
            source = (
                "KINDERGARTEN_INFO"
                if institution_type == "KINDERGARTEN"
                else "NEIS"
            )
            institution_id = (
                f"kindergarten:{sequence:07d}"
                if source == "KINDERGARTEN_INFO"
                else f"neis:B10:{sequence:07d}"
            )
            records.append(
                replace(
                    source_record(
                        institution_id=institution_id
                    ),
                    institution_type=institution_type,
                    source=source,
                )
            )
            sequence += 1
    return tuple(records)


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


def mixed_neis_records() -> tuple[SourceInstitutionRecord, ...]:
    dates = ("2026-04-23", "2026-04-23", "2026-05-17", "2026-06-07")
    return tuple(
        replace(
            source_record(institution_id=f"neis:B10:{7010001 + index}"),
            source_as_of=source_date,
        )
        for index, source_date in enumerate(dates)
    )
