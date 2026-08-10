import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from app.institutions.snapshot import SnapshotIntegrityError, verify_snapshot

SNAPSHOT_ROOT = Path("apps/travel-map/tests/fixtures/institutions/snapshot")


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

    with pytest.raises(SnapshotIntegrityError, match="source TEST_NEIS rowCount"):
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
