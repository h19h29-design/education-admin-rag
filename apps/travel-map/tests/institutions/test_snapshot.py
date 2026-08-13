import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from app.institutions.models import (
    Institution,
    InstitutionSite,
    SchoolCountReconciliation,
    SnapshotDiff,
)
from app.institutions.snapshot import SnapshotIntegrityError, verify_snapshot
from pydantic import ValidationError

SNAPSHOT_ROOT = Path("apps/travel-map/tests/fixtures/institutions/snapshot")
REVIEWED_SCHOOL_COUNT_RECONCILIATION = {
    "profileStatus": "TEMPORARY_PRELIMINARY_VARIANCE",
    "profileSha256": (
        "e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06"
    ),
    "benchmarkSha256": (
        "36158d45a3b8c7e8a083e6d78f63fee706618f69eb49d8624877aef07e3a9332"
    ),
    "sources": {
        "KINDERGARTEN_INFO": {
            "fetchedCount": 706,
            "normalizedCount": 706,
            "roleCounts": {"BENCHMARK": 706},
        },
        "NEIS": {
            "fetchedCount": 1415,
            "normalizedCount": 1414,
            "roleCounts": {
                "BENCHMARK": 1373,
                "NONSELECTABLE": 1,
                "QUARANTINED": 18,
                "SUPPLEMENTARY": 23,
            },
        },
    },
    "categories": {
        "ELEMENTARY_SCHOOL": {
            "expectedCount": 609,
            "actualCount": 610,
            "deltaCount": 1,
            "status": "REVIEWED_VARIANCE",
        },
        "HIGH_SCHOOL": {
            "expectedCount": 319,
            "actualCount": 319,
            "deltaCount": 0,
            "status": "MATCHED",
        },
        "KINDERGARTEN": {
            "expectedCount": 724,
            "actualCount": 706,
            "deltaCount": -18,
            "status": "REVIEWED_VARIANCE",
        },
        "MIDDLE_SCHOOL": {
            "expectedCount": 390,
            "actualCount": 390,
            "deltaCount": 0,
            "status": "MATCHED",
        },
        "MISC_SCHOOL": {
            "expectedCount": 18,
            "actualCount": 22,
            "deltaCount": 4,
            "status": "REVIEWED_VARIANCE",
        },
        "SPECIAL_SCHOOL": {
            "expectedCount": 32,
            "actualCount": 32,
            "deltaCount": 0,
            "status": "MATCHED",
        },
    },
    "passed": True,
}


def test_school_count_reconciliation_accepts_only_reviewed_camel_case_shape() -> None:
    parsed = SchoolCountReconciliation.model_validate(
        REVIEWED_SCHOOL_COUNT_RECONCILIATION
    )

    assert parsed.profile_sha256 == REVIEWED_SCHOOL_COUNT_RECONCILIATION[
        "profileSha256"
    ]


# Production break caught: an internal snake_case spelling bypassing the exact
# signed JSON contract even though no producer emits that spelling.
def test_school_count_reconciliation_rejects_snake_case_field() -> None:
    payload = deepcopy(REVIEWED_SCHOOL_COUNT_RECONCILIATION)
    payload["profile_status"] = payload.pop("profileStatus")

    with pytest.raises(ValidationError):
        SchoolCountReconciliation.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("passed",), False),
        (("profileSha256",), "f" * 64),
        (("benchmarkSha256",), "f" * 64),
        (("sources", "NEIS", "fetchedCount"), True),
        (("sources", "NEIS", "normalizedCount"), 1415),
        (("sources", "NEIS", "roleCounts", "QUARANTINED"), 17),
        (("categories", "ELEMENTARY_SCHOOL", "actualCount"), 609),
        (("categories", "ELEMENTARY_SCHOOL", "deltaCount"), 0),
        (("categories", "ELEMENTARY_SCHOOL", "status"), "MATCHED"),
    ],
)
def test_school_count_reconciliation_rejects_unreviewed_values(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = deepcopy(REVIEWED_SCHOOL_COUNT_RECONCILIATION)
    selected: dict[str, Any] = payload
    for name in path[:-1]:
        selected = selected[name]
    selected[path[-1]] = value

    with pytest.raises(ValidationError):
        SchoolCountReconciliation.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("sources",),
        ("sources", "NEIS", "roleCounts"),
        ("categories",),
    ],
)
def test_school_count_reconciliation_rejects_unsorted_mapping_keys(
    path: tuple[str, ...],
) -> None:
    payload = deepcopy(REVIEWED_SCHOOL_COUNT_RECONCILIATION)
    selected: dict[str, Any] = payload
    for name in path:
        selected = selected[name]
    reversed_items = reversed(tuple(selected.items()))
    replacement = dict(reversed_items)
    parent: dict[str, Any] = payload
    for name in path[:-1]:
        parent = parent[name]
    parent[path[-1]] = replacement

    with pytest.raises(ValidationError):
        SchoolCountReconciliation.model_validate(payload)


# Production break caught: treating the explicitly identified synthetic fixture
# exception as an ordinary production snapshot schema omission.
def test_snapshot_accepts_only_identified_test_fixture_without_reconciliation() -> None:
    snapshot = verify_snapshot(SNAPSHOT_ROOT)

    assert snapshot.manifest.school_count_reconciliation is None
    assert snapshot.manifest.approved_by_role == "TEST_FIXTURE_REVIEWER"
    assert snapshot.manifest.sources[0].source_category_counts == {}
    assert snapshot.manifest.sources[0].source_population_role_counts == {}
    assert snapshot.manifest.sources[0].source_population_profile_sha256 is None


# Production break caught: accepting source provenance that omits the required
# privacy-safe unclassified-school aggregate fields for a non-NEIS source.
def test_snapshot_requires_empty_unclassified_provenance_for_other_sources(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["unclassifiedSchoolKindCounts"] = {}
    source["unclassifiedSchoolPolicySha256"] = None
    write_manifest(fixture, manifest)

    verified = verify_snapshot(fixture)

    assert verified.manifest.sources[0].unclassified_school_kind_counts == {}
    assert verified.manifest.sources[0].unclassified_school_policy_sha256 is None


@pytest.mark.parametrize("target", ["institution", "site"])
def test_snapshot_rejects_active_unclassified_records(
    tmp_path: Path,
    target: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    institution_index = 0 if target == "institution" else 8
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=institution_index,
        field_name="institutionType",
        value="UNCLASSIFIED_SCHOOL",
    )
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=institution_index,
        field_name="source",
        value="NEIS",
    )
    if target == "site":
        change_jsonl_record(
            fixture,
            "institutions.jsonl",
            record_index=institution_index,
            field_name="statusSource",
            value="OFFICIAL_CLASSIFICATION_PENDING",
        )

    with pytest.raises(SnapshotIntegrityError, match="unclassified"):
        verify_snapshot(fixture)


# Production break caught: loading bytes that no longer match the approved manifest.
def test_snapshot_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    (fixture / "fixture-001" / "sites.jsonl").write_text(
        "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(SnapshotIntegrityError, match="sites.jsonl sha256"):
        verify_snapshot(fixture)


# Production break caught: accepting a pointer object with unreviewed fields.
def test_current_pointer_requires_exact_schema(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    write_json(
        fixture / "current.json",
        {"snapshotId": "fixture-001", "fallbackSnapshotId": "fixture-000"},
    )

    with pytest.raises(SnapshotIntegrityError, match="current.json"):
        verify_snapshot(fixture)


# Production break caught: a traversal snapshot ID being hidden by a later duplicate
# key under Python's default last-key-wins JSON behavior.
def test_current_pointer_rejects_duplicate_keys(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    (fixture / "current.json").write_text(
        '{"snapshotId":"../escape","snapshotId":"fixture-001"}\n',
        encoding="utf-8",
    )

    with pytest.raises(SnapshotIntegrityError, match="duplicate JSON key: snapshotId"):
        verify_snapshot(fixture)


# Production break caught: allowing traversal or an unbounded directory identifier.
@pytest.mark.parametrize(
    "snapshot_id",
    ["", "..", "../escape", "fixture/001", "fixture.001", "x" * 65],
)
def test_current_pointer_rejects_unsafe_snapshot_slug(
    tmp_path: Path,
    snapshot_id: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    write_json(fixture / "current.json", {"snapshotId": snapshot_id})

    with pytest.raises(
        SnapshotIntegrityError,
        match="current snapshotId must be a safe slug",
    ):
        verify_snapshot(fixture)


# Production break caught: following a JSONL symlink out of the snapshot directory.
def test_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    sites = fixture / "fixture-001" / "sites.jsonl"
    escaped_sites = tmp_path / "escaped-sites.jsonl"
    escaped_sites.write_bytes(sites.read_bytes())
    sites.unlink()
    sites.symlink_to(escaped_sites)

    with pytest.raises(
        SnapshotIntegrityError,
        match="sites.jsonl must remain inside the snapshot directory",
    ):
        verify_snapshot(fixture)


# Production break caught: loading a snapshot under an unsupported schema version.
def test_snapshot_requires_schema_version_one(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, "schemaVersion", 2)

    with pytest.raises(SnapshotIntegrityError, match="schemaVersion must be 1"):
        verify_snapshot(fixture)


# Production break caught: loading a snapshot without explicit nonblank approval.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("approved", False),
        ("approvedAt", None),
        ("approvedAt", "   "),
        ("approvedByRole", None),
        ("approvedByRole", "   "),
        ("sources", []),
    ],
)
def test_snapshot_requires_complete_approval_metadata(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, field_name, value)

    with pytest.raises(SnapshotIntegrityError, match="approved|sources"):
        verify_snapshot(fixture)


# Production break caught: accepting a manifest with extra unreviewed fields.
def test_snapshot_manifest_forbids_unknown_fields(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["downloadUrl"] = "https://example.invalid/unapproved"
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="manifest.json"):
        verify_snapshot(fixture)


# Production break caught: treating a noncanonical snake_case key as the approved
# camelCase manifest schema.
def test_snapshot_manifest_requires_canonical_field_names(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["schema_version"] = manifest.pop("schemaVersion")
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="manifest.json fields"):
        verify_snapshot(fixture)


# Production break caught: a rejected schema version being hidden by a later
# duplicate top-level manifest key.
def test_snapshot_manifest_rejects_duplicate_top_level_key(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest_path = fixture / "fixture-001" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace(
            '"schemaVersion": 1',
            '"schemaVersion": 2, "schemaVersion": 1',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotIntegrityError, match="duplicate JSON key: schemaVersion"):
        verify_snapshot(fixture)


# Production break caught: duplicate keys inside nested source/diff objects escaping
# a top-level-only duplicate check.
@pytest.mark.parametrize(
    ("original", "replacement", "duplicate_key"),
    [
        (
            '"source": "TEST_NEIS"',
            '"source": "UNAPPROVED", "source": "TEST_NEIS"',
            "source",
        ),
        (
            '"addedCount": 10',
            '"addedCount": 999, "addedCount": 10',
            "addedCount",
        ),
    ],
)
def test_snapshot_manifest_rejects_duplicate_nested_key(
    tmp_path: Path,
    original: str,
    replacement: str,
    duplicate_key: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest_path = fixture / "fixture-001" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace(original, replacement, 1),
        encoding="utf-8",
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match=f"duplicate JSON key: {duplicate_key}",
    ):
        verify_snapshot(fixture)


# Production break caught: accepting JavaScript-only numeric constants as JSON.
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_snapshot_rejects_nonstandard_json_numeric_constant(
    tmp_path: Path,
    constant: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest_path = fixture / "fixture-001" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace('"pageCount": 1', f'"pageCount": {constant}', 1),
        encoding="utf-8",
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match=f"nonstandard JSON constant: {re.escape(constant)}",
    ):
        verify_snapshot(fixture)


# Production break caught: current.json selecting bytes whose manifest names another snapshot.
def test_snapshot_id_must_match_pointer_and_manifest(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, "snapshotId", "fixture-002")

    with pytest.raises(SnapshotIntegrityError, match="manifest snapshotId"):
        verify_snapshot(fixture)


# Production break caught: trusting declared row totals rather than recounting JSONL.
@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("institutionCount", 9, "institutionCount"),
        ("siteCount", 11, "siteCount"),
    ],
)
def test_snapshot_recounts_each_jsonl_file(
    tmp_path: Path,
    field_name: str,
    value: int,
    message: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, field_name, value)

    with pytest.raises(SnapshotIntegrityError, match=message):
        verify_snapshot(fixture)


# Production break caught: trusting stale category aggregates in the manifest.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("countsByType", {"ELEMENTARY_SCHOOL": 10}),
        ("countsByFoundation", {"PUBLIC": 9, "PRIVATE": 1}),
        ("countsByStatus", {"ACTIVE": 10}),
        ("coordinateQualityCounts", {"ENTRANCE": 12}),
    ],
)
def test_snapshot_recomputes_category_aggregates(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, field_name, value)

    with pytest.raises(SnapshotIntegrityError, match=field_name):
        verify_snapshot(fixture)


# Production break caught: declaring a source row count that does not match its records.
def test_snapshot_recomputes_source_row_counts(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["sources"][0]["rowCount"] = 9
    write_manifest(fixture, manifest)

    with pytest.raises(
        SnapshotIntegrityError,
        match="source TEST_NEIS rowCount|normalizedRowCount",
    ):
        verify_snapshot(fixture)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"pageCount": 0}, "page/fetched counts"),
        ({"fetchedRowCount": 0}, "page/fetched counts"),
        (
            {"normalizedRowCount": 11},
            "normalizedRowCount must not exceed fetchedRowCount",
        ),
        (
            {"normalizedRowCount": 8},
            r"normalizedRowCount \+ preservedRowCount must equal rowCount",
        ),
    ],
)
def test_snapshot_rejects_impossible_source_count_relations(
    tmp_path: Path,
    updates: dict[str, int],
    message: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["sources"][0].update(updates)
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match=message):
        verify_snapshot(fixture)


# Production break caught: accepting noncanonical raw observation histograms that
# cannot be bound exactly to the fetched source rows.
@pytest.mark.parametrize(
    ("counts", "match"),
    [
        ({"2026-08-01": 0}, "positive"),
        ({"2026-08-01": 9}, "fetchedRowCount"),
    ],
)
def test_snapshot_rejects_invalid_source_observation_date_counts(
    tmp_path: Path,
    counts: dict[str, int],
    match: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["sourceObservationDateCounts"] = counts
    source["normalizedObservationDateCounts"] = {"2026-08-01": 9}
    source["preservedObservationDateCounts"] = {"2026-08-01": 1}
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match=match):
        verify_snapshot(fixture)


# Production break caught: accepting an ordered-map histogram whose raw JSON key
# order is noncanonical even though the date/count pairs are otherwise valid.
def test_snapshot_rejects_unsorted_source_observation_date_count_keys(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["sourceAsOf"] = None
    source["sourceObservationDateCounts"] = {
        "2026-07-31": 1,
        "2026-08-01": 9,
    }
    write_manifest(fixture, manifest)
    manifest_path = fixture / "fixture-001" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    canonical = (
        '"sourceObservationDateCounts":{"2026-07-31":1,"2026-08-01":9}'
    )
    unsorted = (
        '"sourceObservationDateCounts":{"2026-08-01":9,"2026-07-31":1}'
    )
    assert canonical in manifest_text
    manifest_path.write_text(
        manifest_text.replace(canonical, unsorted, 1),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotIntegrityError, match="sorted"):
        verify_snapshot(fixture)


# Production break caught: keeping a single sourceAsOf label for a mixed raw fetch.
def test_snapshot_rejects_mixed_dates_with_non_null_source_as_of(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["sourceObservationDateCounts"] = {
        "2026-07-31": 1,
        "2026-08-01": 9,
    }
    source["normalizedObservationDateCounts"] = {"2026-08-01": 10}
    source["preservedObservationDateCounts"] = {}
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="sourceAsOf"):
        verify_snapshot(fixture)


# Production break caught: trusting a declared normalized histogram after the
# persisted institution row dates have changed.
def test_snapshot_rejects_row_dates_that_do_not_match_normalized_histogram(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["sourceObservationDateCounts"] = {"2026-08-01": 10}
    source["normalizedObservationDateCounts"] = {"2026-08-01": 9}
    source["preservedObservationDateCounts"] = {"2026-08-01": 1}
    write_manifest(fixture, manifest)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="sourceAsOf",
        value="2026-07-31",
    )

    with pytest.raises(SnapshotIntegrityError, match="observation date counts"):
        verify_snapshot(fixture)


@pytest.mark.parametrize(
    ("count", "matches", "message"),
    [
        (1, [], "possibleMatchCount"),
        (
            1,
            [
                {
                    "institutionIds": [
                        "test-neis:B10:SEMWATER-ES",
                        "test-neis:B10:UNKNOWN",
                    ],
                    "reason": "EXACT_NORMALIZED_NAME_AND_ADDRESS",
                }
            ],
            "unknown institutionId",
        ),
    ],
)
def test_snapshot_rechecks_possible_match_identities(
    tmp_path: Path,
    count: int,
    matches: list[dict[str, object]],
    message: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["possibleMatchCount"] = count
    manifest["possibleMatches"] = matches
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match=message):
        verify_snapshot(fixture)


# Production break caught: allowing two records to claim one institution identity.
def test_snapshot_rejects_duplicate_institution_ids(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=1,
        field_name="institutionId",
        value="test-neis:B10:SEMWATER-KG",
    )

    with pytest.raises(SnapshotIntegrityError, match="duplicate institutionId"):
        verify_snapshot(fixture)


# Production break caught: allowing two physical rows to claim one site identity.
def test_snapshot_rejects_duplicate_site_ids(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "sites.jsonl",
        record_index=1,
        field_name="siteId",
        value="test-neis:B10:SEMWATER-KG:main",
    )

    with pytest.raises(SnapshotIntegrityError, match="duplicate siteId"):
        verify_snapshot(fixture)


# Production break caught: a bad institution/site identity being hidden by a later
# duplicate field within the same approved JSONL row.
@pytest.mark.parametrize(
    ("filename", "original", "replacement", "duplicate_key"),
    [
        (
            "institutions.jsonl",
            '"institutionId":"test-neis:B10:SEMWATER-KG"',
            (
                '"institutionId":"unsafe","institutionId":'
                '"test-neis:B10:SEMWATER-KG"'
            ),
            "institutionId",
        ),
        (
            "sites.jsonl",
            '"siteId":"test-neis:B10:SEMWATER-KG:main"',
            '"siteId":"unsafe","siteId":"test-neis:B10:SEMWATER-KG:main"',
            "siteId",
        ),
    ],
)
def test_snapshot_rejects_duplicate_jsonl_key(
    tmp_path: Path,
    filename: str,
    original: str,
    replacement: str,
    duplicate_key: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    replace_jsonl_fragment(
        fixture,
        filename,
        record_index=0,
        original=original,
        replacement=replacement,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match=f"duplicate JSON key: {duplicate_key}",
    ):
        verify_snapshot(fixture)


# Production break caught: accepting snake_case aliases or a structurally ambiguous
# institution/site row instead of the one canonical JSONL schema.
@pytest.mark.parametrize(
    ("filename", "original", "replacement"),
    [
        ("institutions.jsonl", '"institutionId"', '"institution_id"'),
        (
            "institutions.jsonl",
            '"officialName":"샘물초등학교병설유치원"',
            '"officialName":"샘물초등학교병설유치원","unexpected":"field"',
        ),
        (
            "institutions.jsonl",
            '"officialName":"샘물초등학교병설유치원",',
            "",
        ),
        ("sites.jsonl", '"siteId"', '"site_id"'),
        (
            "sites.jsonl",
            '"siteName":"본원"',
            '"siteName":"본원","unexpected":"field"',
        ),
        ("sites.jsonl", '"siteName":"본원",', ""),
    ],
)
def test_snapshot_requires_exact_canonical_jsonl_fields(
    tmp_path: Path,
    filename: str,
    original: str,
    replacement: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    replace_jsonl_fragment(
        fixture,
        filename,
        record_index=0,
        original=original,
        replacement=replacement,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match=f"{filename} line 1 fields must exactly match schema version 1",
    ):
        verify_snapshot(fixture)


# Production break caught: loading a site whose parent institution was not approved.
def test_snapshot_rejects_unknown_site_institution_reference(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "sites.jsonl",
        record_index=0,
        field_name="institutionId",
        value="test-neis:B10:DOES-NOT-EXIST",
    )

    with pytest.raises(SnapshotIntegrityError, match="unknown institutionId"):
        verify_snapshot(fixture)


# Production break caught: silently skipping a blank JSONL row.
def test_snapshot_rejects_blank_jsonl_record(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    institutions_path = fixture / "fixture-001" / "institutions.jsonl"
    lines = institutions_path.read_text(encoding="utf-8").splitlines()
    institutions_path.write_text(
        "\n".join([lines[0], "", *lines[1:]]) + "\n",
        encoding="utf-8",
    )
    refresh_manifest_hash(fixture, "institutions.jsonl")

    with pytest.raises(
        SnapshotIntegrityError,
        match="institutions.jsonl line 2 is blank",
    ):
        verify_snapshot(fixture)


# Production break caught: leaking an unhandled JSON decoder error for a bad row.
def test_snapshot_rejects_malformed_jsonl_record(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    sites_path = fixture / "fixture-001" / "sites.jsonl"
    lines = sites_path.read_text(encoding="utf-8").splitlines()
    sites_path.write_text(
        "\n".join(["not-json", *lines[1:]]) + "\n",
        encoding="utf-8",
    )
    refresh_manifest_hash(fixture, "sites.jsonl")

    with pytest.raises(
        SnapshotIntegrityError,
        match="sites.jsonl line 1 is malformed JSON",
    ):
        verify_snapshot(fixture)


# Production break caught: loading an impossible physical or routing-anchor coordinate.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("latitude", 91.0),
        ("longitude", -181.0),
        ("routingAnchorLatitude", -91.0),
        ("routingAnchorLongitude", 181.0),
    ],
)
def test_snapshot_rejects_out_of_bounds_site_coordinates(
    tmp_path: Path,
    field_name: str,
    value: float,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "sites.jsonl",
        record_index=0,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(SnapshotIntegrityError, match="sites.jsonl line 1"):
        verify_snapshot(fixture)


# Production break caught: accepting malformed or noncanonical dates in approved
# institution/site records.
@pytest.mark.parametrize(
    ("filename", "field_name", "value"),
    [
        ("institutions.jsonl", "effectiveFrom", "20260801"),
        ("institutions.jsonl", "effectiveTo", "2026-02-30"),
        ("institutions.jsonl", "sourceAsOf", "2026/08/01"),
        ("sites.jsonl", "effectiveFrom", "2026-W31-5"),
        ("sites.jsonl", "effectiveTo", "2026-08-01T00:00:00Z"),
    ],
)
def test_snapshot_rejects_non_iso_record_dates(
    tmp_path: Path,
    filename: str,
    field_name: str,
    value: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        filename,
        record_index=0,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(SnapshotIntegrityError, match=f"{filename} line 1"):
        verify_snapshot(fixture)


# Production break caught: accepting malformed manifest/source dates.
@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("snapshotAsOf", "20260801"),
        ("sourceAsOf", "2026-02-30"),
    ],
)
def test_snapshot_rejects_non_iso_manifest_dates(
    tmp_path: Path,
    target: str,
    value: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    if target == "sourceAsOf":
        manifest["sources"][0][target] = value
    else:
        manifest[target] = value
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="manifest.json is invalid"):
        verify_snapshot(fixture)


# Production break caught: accepting timestamps that lack an explicit timezone or
# do not follow the approved RFC3339-like shape.
@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("createdAt", "2026-08-01T00:00:00"),
        ("approvedAt", "2026-08-02 00:00:00Z"),
        ("fetchedAt", "2026-08-01T00:00:00"),
    ],
)
def test_snapshot_rejects_naive_or_malformed_timestamps(
    tmp_path: Path,
    target: str,
    value: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    if target == "fetchedAt":
        manifest["sources"][0][target] = value
    else:
        manifest[target] = value
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="manifest.json is invalid"):
        verify_snapshot(fixture)


# Production break caught: accepting an institution/site interval whose end precedes
# its beginning.
@pytest.mark.parametrize("filename", ["institutions.jsonl", "sites.jsonl"])
def test_snapshot_rejects_reversed_effective_interval(
    tmp_path: Path,
    filename: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        filename,
        record_index=0,
        field_name="effectiveTo",
        value="2025-12-31",
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="effectiveTo must not precede effectiveFrom",
    ):
        verify_snapshot(fixture)


# Production break caught: approving a snapshot before it was created.
def test_snapshot_rejects_creation_after_approval(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, "createdAt", "2026-08-03T00:00:00Z")

    with pytest.raises(
        SnapshotIntegrityError,
        match="createdAt must not be later than approvedAt",
    ):
        verify_snapshot(fixture)


# Production break caught: claiming a data date later than snapshot creation.
def test_snapshot_rejects_as_of_date_after_creation(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, "snapshotAsOf", "2026-08-02")

    with pytest.raises(
        SnapshotIntegrityError,
        match="snapshotAsOf must not be later than createdAt date",
    ):
        verify_snapshot(fixture)


# Production break caught: claiming source observations from after the fetch date.
def test_snapshot_rejects_source_as_of_after_fetch(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["sources"][0]["sourceAsOf"] = "2026-08-02"
    manifest["sources"][0]["sourceObservationDateCounts"] = {"2026-08-02": 10}
    manifest["sources"][0]["normalizedObservationDateCounts"] = {"2026-08-02": 9}
    manifest["sources"][0]["preservedObservationDateCounts"] = {"2026-08-02": 1}
    write_manifest(fixture, manifest)

    with pytest.raises(
        SnapshotIntegrityError,
        match="sourceAsOf must not be later than fetchedAt date",
    ):
        verify_snapshot(fixture)


# Production break caught: bypassing fetch chronology by setting sourceAsOf to
# null while one raw mixed-vintage histogram key is later than fetchedAt.
def test_snapshot_rejects_mixed_source_observation_date_after_fetch(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    source = manifest["sources"][0]
    source["sourceAsOf"] = None
    source["sourceObservationDateCounts"] = {
        "2026-08-01": 9,
        "2026-08-02": 1,
    }
    write_manifest(fixture, manifest)

    with pytest.raises(
        SnapshotIntegrityError,
        match="observation dates must not be later than fetchedAt date",
    ):
        verify_snapshot(fixture)


# Production break caught: accepting a source fetched after the manifest was
# created, even when the source's local calendar date still matches sourceAsOf.
def test_snapshot_rejects_source_fetched_after_manifest_creation(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["sources"][0]["fetchedAt"] = "2026-08-01T00:00:01-01:00"
    write_manifest(fixture, manifest)

    with pytest.raises(
        SnapshotIntegrityError,
        match="source TEST_NEIS fetchedAt must not be later than manifest createdAt",
    ):
        verify_snapshot(fixture)


# Same chronology guard: a later local wall-clock time can still represent an
# earlier instant once its UTC offset is applied.
def test_snapshot_compares_source_fetch_and_creation_as_instants(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["sources"][0]["fetchedAt"] = "2026-08-01T08:59:59+09:00"
    write_manifest(fixture, manifest)

    verify_snapshot(fixture)


# Production break caught: accepting a source vintage newer than the approved
# snapshot's as-of date.
def test_snapshot_rejects_source_as_of_after_manifest_snapshot_as_of(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    update_manifest(fixture, "snapshotAsOf", "2026-07-31")

    with pytest.raises(
        SnapshotIntegrityError,
        match="source TEST_NEIS sourceAsOf must not be later than manifest snapshotAsOf",
    ):
        verify_snapshot(fixture)


# Production break caught: bypassing snapshot chronology with null sourceAsOf
# even though mixed observation histograms are later than snapshotAsOf.
def test_snapshot_rejects_mixed_observation_date_after_snapshot_as_of(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["snapshotAsOf"] = "2026-07-31"
    source = manifest["sources"][0]
    source["sourceAsOf"] = None
    source["sourceObservationDateCounts"] = {
        "2026-07-31": 1,
        "2026-08-01": 9,
    }
    write_manifest(fixture, manifest)

    with pytest.raises(
        SnapshotIntegrityError,
        match="observation date must not be later than manifest snapshotAsOf",
    ):
        verify_snapshot(fixture)


# Production break caught: mixing institution rows from a different source vintage
# while preserving the same source name and row count.
def test_snapshot_requires_institution_and_manifest_source_as_of_match(
    tmp_path: Path,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="sourceAsOf",
        value="2026-07-31",
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="normalized observation date counts",
    ):
        verify_snapshot(fixture)


# Production break caught: publishing an ACTIVE institution/site that is future-dated
# or expired on the snapshot's as-of date.
@pytest.mark.parametrize(
    ("filename", "entity", "field_name", "value"),
    [
        ("institutions.jsonl", "institution", "effectiveFrom", "2026-08-02"),
        ("institutions.jsonl", "institution", "effectiveTo", "2026-07-31"),
        ("sites.jsonl", "site", "effectiveFrom", "2026-08-02"),
        ("sites.jsonl", "site", "effectiveTo", "2026-07-31"),
    ],
)
def test_snapshot_rejects_active_record_not_effective_on_snapshot_date(
    tmp_path: Path,
    filename: str,
    entity: str,
    field_name: str,
    value: str,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        filename,
        record_index=0,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match=f"ACTIVE {entity} .* is not effective on snapshotAsOf",
    ):
        verify_snapshot(fixture)


# Production break caught: accepting a blank required model field after hash approval.
def test_snapshot_rejects_blank_model_data(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "sites.jsonl",
        record_index=0,
        field_name="siteName",
        value="   ",
    )

    with pytest.raises(SnapshotIntegrityError, match="sites.jsonl line 1"):
        verify_snapshot(fixture)


# Production break caught: models accepting namespaceless, path-like, whitespace, or
# Unicode identifiers before snapshot graph validation.
@pytest.mark.parametrize(
    "institution_id",
    ["SEMWATER-KG", "neis:has space", "neis:B10/bad", "학교:B10:1"],
)
def test_institution_model_rejects_unsafe_namespaced_id(
    institution_id: str,
) -> None:
    payload = fixture_jsonl_record("institutions.jsonl", 0)
    payload["institutionId"] = institution_id

    with pytest.raises(ValidationError, match="safe namespaced institution ID"):
        Institution.model_validate_json(json.dumps(payload, ensure_ascii=False))


# Production break caught: unsafe IDs entering lineage metadata even when the primary
# institution ID is valid.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("mergedInto", "../escape"),
        ("supersedes", ["학교:B10:OLD"]),
    ],
)
def test_institution_model_rejects_unsafe_lineage_id(
    field_name: str,
    value: object,
) -> None:
    payload = fixture_jsonl_record("institutions.jsonl", 0)
    payload[field_name] = value

    with pytest.raises(ValidationError, match="safe namespaced institution ID"):
        Institution.model_validate_json(json.dumps(payload, ensure_ascii=False))


# Production break caught: accepting an unsafe namespaced site identity directly.
@pytest.mark.parametrize(
    "site_id",
    ["main", "neis:bad site", "neis:B10/escape:main", "기관:B10:main"],
)
def test_site_model_rejects_unsafe_namespaced_id(site_id: str) -> None:
    payload = fixture_jsonl_record("sites.jsonl", 0)
    payload["siteId"] = site_id

    with pytest.raises(ValidationError, match="safe namespaced site ID"):
        InstitutionSite.model_validate_json(json.dumps(payload, ensure_ascii=False))


# Production break caught: accepting unsafe snapshot pointers inside model metadata.
@pytest.mark.parametrize("snapshot_id", ["../old", "fixture.001", "x" * 65])
def test_lineage_models_reject_unsafe_snapshot_slug(snapshot_id: str) -> None:
    institution_payload = fixture_jsonl_record("institutions.jsonl", 0)
    institution_payload["lastSeenSnapshot"] = snapshot_id
    diff_payload = {
        "previousSnapshotId": snapshot_id,
        "addedCount": 0,
        "changedCount": 0,
        "missingCount": 0,
        "closedCandidateCount": 0,
    }

    with pytest.raises(ValidationError, match="safe snapshot slug"):
        Institution.model_validate_json(
            json.dumps(institution_payload, ensure_ascii=False)
        )
    with pytest.raises(ValidationError, match="safe snapshot slug"):
        SnapshotDiff.model_validate_json(json.dumps(diff_payload))


# Production break caught: loading a namespaceless institution ID from an otherwise
# hash-consistent snapshot.
def test_snapshot_rejects_namespaceless_institution_id(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="institutionId",
        value="SEMWATER-KG",
    )

    with pytest.raises(SnapshotIntegrityError, match="safe namespaced institution ID"):
        verify_snapshot(fixture)


# Production break caught: accepting a valid-looking site ID that does not belong to
# the row's parent institution.
def test_snapshot_rejects_site_id_with_wrong_parent_prefix(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "sites.jsonl",
        record_index=0,
        field_name="siteId",
        value="neis:B10:OTHER:main",
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="siteId must begin with parent institutionId and a safe suffix",
    ):
        verify_snapshot(fixture)


# Production break caught: accepting an unsafe last-seen snapshot pointer from JSONL.
def test_snapshot_rejects_unsafe_last_seen_snapshot_slug(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="lastSeenSnapshot",
        value="../fixture-001",
    )

    with pytest.raises(SnapshotIntegrityError, match="safe snapshot slug"):
        verify_snapshot(fixture)


# Production break caught: accepting an unsafe previous snapshot pointer in a
# canonical manifest.
def test_snapshot_rejects_unsafe_previous_snapshot_slug(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    manifest = read_manifest(fixture)
    manifest["diff"]["previousSnapshotId"] = "../fixture-000"
    write_manifest(fixture, manifest)

    with pytest.raises(SnapshotIntegrityError, match="safe snapshot slug"):
        verify_snapshot(fixture)


# Production break caught: retaining a lineage edge to an institution absent from the
# approved snapshot.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("mergedInto", "test-neis:B10:DOES-NOT-EXIST"),
        ("supersedes", ["test-neis:B10:DOES-NOT-EXIST"]),
    ],
)
def test_snapshot_rejects_dangling_lineage_reference(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(SnapshotIntegrityError, match="unknown lineage target"):
        verify_snapshot(fixture)


# Production break caught: allowing an institution to point to itself in either
# lineage field.
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("mergedInto", "test-neis:B10:SEMWATER-KG"),
        ("supersedes", ["test-neis:B10:SEMWATER-KG"]),
    ],
)
def test_snapshot_rejects_self_lineage_reference(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(SnapshotIntegrityError, match="self lineage reference"):
        verify_snapshot(fixture)


# Production break caught: allowing the same lineage target to appear more than once.
def test_snapshot_rejects_duplicate_lineage_reference(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="supersedes",
        value=["test-neis:B10:SEMWATER-ES", "test-neis:B10:SEMWATER-ES"],
    )

    with pytest.raises(SnapshotIntegrityError, match="duplicate lineage reference"):
        verify_snapshot(fixture)


# Production break caught: treating reciprocal predecessor/successor declarations as
# opposing graph edges instead of the same chronological transition.
def test_snapshot_accepts_reciprocal_lineage_declarations(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="mergedInto",
        value="test-neis:B10:SEMWATER-ES",
    )
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=1,
        field_name="supersedes",
        value=["test-neis:B10:SEMWATER-KG"],
    )

    verified = verify_snapshot(fixture)

    assert verified.institutions[0].merged_into == "test-neis:B10:SEMWATER-ES"
    assert verified.institutions[1].supersedes == (
        "test-neis:B10:SEMWATER-KG",
    )


# Production break caught: overlooking a genuine directed cycle when mergedInto and
# supersedes declarations need different chronological orientations.
def test_snapshot_rejects_three_node_lineage_cycle(tmp_path: Path) -> None:
    fixture = copy_fixture_snapshot(tmp_path)
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="mergedInto",
        value="test-neis:B10:SEMWATER-ES",
    )
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=0,
        field_name="supersedes",
        value=["test-neis:B10:HANBIT-GANGNAM"],
    )
    change_jsonl_record(
        fixture,
        "institutions.jsonl",
        record_index=2,
        field_name="supersedes",
        value=["test-neis:B10:SEMWATER-ES"],
    )

    with pytest.raises(SnapshotIntegrityError, match="lineage cycle"):
        verify_snapshot(fixture)


# Production break caught: verifier returning unchecked dictionaries to the store.
def test_verified_snapshot_contains_validated_models() -> None:
    verified = verify_snapshot(SNAPSHOT_ROOT)

    assert verified.manifest.snapshot_id == "fixture-001"
    assert len(verified.institutions) == 10
    assert len(verified.sites) == 12
    assert verified.institutions[0].institution_id == "test-neis:B10:SEMWATER-KG"


def copy_fixture_snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT_ROOT, destination)
    return destination


def fixture_jsonl_record(filename: str, record_index: int) -> dict[str, Any]:
    path = SNAPSHOT_ROOT / "fixture-001" / filename
    return json.loads(path.read_text(encoding="utf-8").splitlines()[record_index])


def read_manifest(snapshot_root: Path) -> dict[str, Any]:
    return json.loads(
        (snapshot_root / "fixture-001" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


def write_manifest(snapshot_root: Path, manifest: dict[str, Any]) -> None:
    write_json(snapshot_root / "fixture-001" / "manifest.json", manifest)


def update_manifest(snapshot_root: Path, field_name: str, value: object) -> None:
    manifest = read_manifest(snapshot_root)
    manifest[field_name] = value
    write_manifest(snapshot_root, manifest)


def change_jsonl_record(
    snapshot_root: Path,
    filename: str,
    *,
    record_index: int,
    field_name: str,
    value: object,
) -> None:
    path = snapshot_root / "fixture-001" / filename
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[record_index][field_name] = value
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    refresh_manifest_hash(snapshot_root, filename)


def replace_jsonl_fragment(
    snapshot_root: Path,
    filename: str,
    *,
    record_index: int,
    original: str,
    replacement: str,
) -> None:
    path = snapshot_root / "fixture-001" / filename
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[record_index].count(original) == 1
    lines[record_index] = lines[record_index].replace(original, replacement, 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    refresh_manifest_hash(snapshot_root, filename)


def refresh_manifest_hash(snapshot_root: Path, filename: str) -> None:
    manifest = read_manifest(snapshot_root)
    manifest_field = {
        "institutions.jsonl": "institutionsSha256",
        "sites.jsonl": "sitesSha256",
    }[filename]
    manifest[manifest_field] = hashlib.sha256(
        (snapshot_root / "fixture-001" / filename).read_bytes()
    ).hexdigest()
    write_manifest(snapshot_root, manifest)


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
