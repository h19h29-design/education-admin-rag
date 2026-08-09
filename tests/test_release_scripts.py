from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import src.release as release_module
from src.evaluation.release_report import (
    build_release_evaluation_report,
    canonical_release_evaluation_bytes,
)
from src.ingestion.privacy import PrivacyFinding
from src.release import (
    BACKUP_PAYLOAD_PATHS,
    IndexReleaseEvidence,
    ReleaseAttestation,
    assemble_release_verification_evidence,
    create_backup_manifest,
    create_index_release_evidence,
    create_verification_attestation,
    load_backup_tool_lock,
    materialize_backup_restore,
    prepare_backup_payload,
    promote_release,
    reconcile_release_state,
    start_release_environment,
    verify_backup_manifest,
    write_release_attestation,
)
from src.retrieval.dense import DenseBuildResult
from src.retrieval.lexical import build_lexical_index
from tests.corpus.test_storage import _batch, _build
from tests.evaluation.test_release_report import _gold, _ingestion, _retrieval

RELEASE_ID = "corpus-20250808123456-deadbeef"
BACKUP_TOOL_LOCK = Path("config/backup-tools.lock.json")


def _backup_payload(root: Path) -> None:
    for index, relative in enumerate(BACKUP_PAYLOAD_PATHS, start=1):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"payload-{index}".encode("ascii"))


def test_backup_manifest_is_deterministic_complete_and_tamper_evident(
    tmp_path: Path,
) -> None:
    """Catches omitted release evidence or changed restore bytes."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    _backup_payload(first)
    _backup_payload(second)

    first_manifest = create_backup_manifest(first, release_id=RELEASE_ID)
    second_manifest = create_backup_manifest(second, release_id=RELEASE_ID)

    assert first_manifest.bundle_sha256 == second_manifest.bundle_sha256
    assert tuple(entry.path for entry in first_manifest.files) == BACKUP_PAYLOAD_PATHS
    assert verify_backup_manifest(first) == first_manifest
    (first / BACKUP_PAYLOAD_PATHS[0]).write_bytes(b"changed")
    with pytest.raises(release_module.ReleaseError, match="backup_bundle_invalid"):
        verify_backup_manifest(first)


def test_backup_manifest_rejects_missing_symlink_and_unmanaged_payload(
    tmp_path: Path,
) -> None:
    """Catches partial bundles, redirected inputs, and undeclared plaintext copies."""
    root = tmp_path / "bundle"
    _backup_payload(root)
    (root / BACKUP_PAYLOAD_PATHS[0]).unlink()
    (root / BACKUP_PAYLOAD_PATHS[0]).symlink_to(root / BACKUP_PAYLOAD_PATHS[1])
    with pytest.raises(release_module.ReleaseError, match="backup_bundle_invalid"):
        create_backup_manifest(root, release_id=RELEASE_ID)

    (root / BACKUP_PAYLOAD_PATHS[0]).unlink()
    (root / BACKUP_PAYLOAD_PATHS[0]).write_bytes(b"restored")
    (root / "private-labels.jsonl").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    with pytest.raises(release_module.ReleaseError, match="backup_bundle_invalid"):
        create_backup_manifest(root, release_id=RELEASE_ID)


def test_prepare_backup_payload_uses_sqlite_backup_and_exact_public_inputs(
    tmp_path: Path,
) -> None:
    """Catches raw SQLite copying or private labels entering plaintext staging."""
    source_db = tmp_path / "canonical.sqlite3"
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE proof(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO proof VALUES(7)")
    inputs = {}
    for name in (
        "qdrant.snapshot",
        "source-manifest.json",
        "models.lock.json",
        "evaluation-report.json",
    ):
        path = tmp_path / f"input-{name}"
        path.write_bytes(name.encode("ascii"))
        inputs[name] = path
    output = tmp_path / "backup-staging"

    prepare_backup_payload(
        output,
        canonical_database=source_db,
        qdrant_snapshot=inputs["qdrant.snapshot"],
        source_manifest=inputs["source-manifest.json"],
        model_lock=inputs["models.lock.json"],
        evaluation_report=inputs["evaluation-report.json"],
    )

    with sqlite3.connect(output / "canonical/canonical.sqlite3") as connection:
        assert connection.execute("SELECT value FROM proof").fetchone() == (7,)
    assert (output / "qdrant/qdrant.snapshot").read_bytes() == b"qdrant.snapshot"
    assert not (output / "blind-labels.age").exists()
    assert not any("private" in path.name for path in output.rglob("*"))


def test_materialize_restore_rehashes_stable_canonical_and_snapshot_bytes(
    tmp_path: Path,
) -> None:
    """Catches a verified bundle path being swapped before restore copying."""
    bundle = tmp_path / "bundle"
    _backup_payload(bundle)
    manifest = create_backup_manifest(bundle, release_id=RELEASE_ID)
    output = tmp_path / "restore"

    materialize_backup_restore(bundle, output)

    expected = {entry.path: entry.sha256 for entry in manifest.files}
    assert (
        hashlib.sha256((output / "canonical.sqlite3").read_bytes()).hexdigest()
        == (expected["canonical/canonical.sqlite3"])
    )
    assert (
        hashlib.sha256((output / "qdrant.snapshot").read_bytes()).hexdigest()
        == (expected["qdrant/qdrant.snapshot"])
    )


def test_start_release_writes_only_minimal_mode_0600_environment(
    tmp_path: Path,
) -> None:
    """Catches secrets, ambient state, or permissive modes entering active-release.env."""
    source_root = tmp_path / "source"
    artifact_root = tmp_path / "artifacts"
    private_eval_root = tmp_path / "private-eval"
    for root in (source_root, artifact_root, private_eval_root):
        root.mkdir()
    env_file = artifact_root / "active-release.env"

    environment = start_release_environment(
        source_root=source_root,
        artifact_root=artifact_root,
        private_eval_root=private_eval_root,
        env_file=env_file,
        released_at=datetime(2025, 8, 8, 12, 34, 56, tzinfo=UTC),
        git_sha="deadbeef" + "1" * 32,
    )

    assert environment.release_id == RELEASE_ID
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        f"SEN_QA_RELEASE_ID={RELEASE_ID}",
        f"SEN_QA_SOURCE_ROOT={source_root}",
        f"SEN_QA_ARTIFACT_ROOT={artifact_root}",
        f"SEN_QA_PRIVATE_EVAL_ROOT={private_eval_root}",
    ]


def test_start_release_rejects_overwrite_and_overlapping_roots(tmp_path: Path) -> None:
    """Catches release state replacement or private labels sharing artifact storage."""
    source_root = tmp_path / "source"
    artifact_root = tmp_path / "artifacts"
    source_root.mkdir()
    artifact_root.mkdir()
    env_file = artifact_root / "active-release.env"
    env_file.write_text("existing=1\n", encoding="utf-8")

    with pytest.raises(
        release_module.ReleaseError, match="release_environment_invalid"
    ):
        start_release_environment(
            source_root=source_root,
            artifact_root=artifact_root,
            private_eval_root=artifact_root,
            env_file=env_file,
            released_at=datetime(2025, 8, 8, 12, 34, 56, tzinfo=UTC),
            git_sha="deadbeef" + "1" * 32,
        )

    unsafe_source = tmp_path / "source:docker-option"
    private_root = tmp_path / "private"
    unsafe_source.mkdir()
    private_root.mkdir()
    with pytest.raises(
        release_module.ReleaseError, match="release_environment_invalid"
    ):
        start_release_environment(
            source_root=unsafe_source,
            artifact_root=artifact_root,
            private_eval_root=private_root,
            env_file=artifact_root / "active-release-unsafe.env",
            released_at=datetime(2025, 8, 8, 12, 34, 56, tzinfo=UTC),
            git_sha="deadbeef" + "1" * 32,
        )


def test_attestation_publish_never_clobbers_a_concurrent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches exists-then-replace racing another release operator."""
    target = tmp_path / "verification.json"
    real_link = os.link

    def create_competitor_then_link(
        source: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        Path(destination).write_bytes(b"competing-attestation")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(release_module.os, "link", create_competitor_then_link)

    with pytest.raises(
        release_module.ReleaseError, match="release_artifact_write_failed"
    ):
        write_release_attestation(
            target,
            ReleaseAttestation(
                kind="verification",
                release_id=RELEASE_ID,
                bundle_sha256="1" * 64,
            ),
        )

    assert target.read_bytes() == b"competing-attestation"


def _verification_evidence(*, retrieval_gate: bool = True) -> dict[str, object]:
    return {
        "schema_version": "sen-qa-release-verification-evidence/v1",
        "release_id": RELEASE_ID,
        "canonical_bundle_sha256": "1" * 64,
        "canonical_content_sha256": "2" * 64,
        "lexical_index_sha256": "3" * 64,
        "dense_sample_sha256": "4" * 64,
        "eligible_chunks": 100,
        "lexical_chunks": 100,
        "dense_points": 100,
        "gold_items": 200,
        "blind_items": 60,
        "quarantined_pages": 0,
        "failed_pages": 0,
        "provenance_missing": 0,
        "privacy_findings_unresolved": 0,
        "warm_latency_p95_ms": 2500.0,
        "review_gate": True,
        "ingestion_gate": True,
        "retrieval_gate": retrieval_gate,
        "privacy_gate": True,
    }


def test_verification_attestation_requires_every_measured_release_gate(
    tmp_path: Path,
) -> None:
    """Catches a partial or sub-threshold report authorizing promotion."""
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "verification.json"
    evidence.write_text(
        json.dumps(_verification_evidence(), sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="ascii",
    )

    attestation = create_verification_attestation(
        evidence, output=output, expected_release_id=RELEASE_ID
    )

    assert attestation.kind == "verification"
    assert attestation.bundle_sha256 == "1" * 64
    failed_evidence = tmp_path / "failed-evidence.json"
    failed_evidence.write_text(
        json.dumps(
            _verification_evidence(retrieval_gate=False),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    with pytest.raises(release_module.ReleaseError, match="release_evidence_invalid"):
        create_verification_attestation(
            failed_evidence,
            output=tmp_path / "must-not-exist.json",
            expected_release_id=RELEASE_ID,
        )


def test_release_evidence_is_derived_from_canonical_index_and_evaluation_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch(release_id=RELEASE_ID)
    canonical = _build(
        tmp_path,
        batch,
        registry_name="issuance.sqlite3",
        output_name="canonical-output",
    )
    lexical_path = tmp_path / "lexical.sqlite3"
    lexical = build_lexical_index(
        canonical.bundle_path / "canonical.sqlite3", lexical_path
    )
    gold = _gold()
    retrieval = _retrieval(gold)
    evaluation = build_release_evaluation_report(
        release_id=RELEASE_ID,
        gold_items=gold,
        ingestion_observations=_ingestion(gold),
        retrieval_observations={
            system: retrieval for system in ("substring", "lexical", "dense", "hybrid")
        },
    )
    evaluation_path = tmp_path / "evaluation-report.json"
    evaluation_path.write_bytes(canonical_release_evaluation_bytes(evaluation))
    index_path = tmp_path / "index-attestation.json"
    index_evidence = IndexReleaseEvidence(
        schema_version="sen-qa-index-evidence/v1",
        release_id=RELEASE_ID,
        canonical_database_sha256=canonical.database_sha256,
        lexical_index_sha256=hashlib.sha256(lexical_path.read_bytes()).hexdigest(),
        dense_sample_sha256="4" * 64,
        eligible_chunks=lexical.indexed_chunks,
        lexical_chunks=lexical.indexed_chunks,
        dense_points=lexical.indexed_chunks,
        collection_name=f"{RELEASE_ID}-bge-m3",
    )
    index_path.write_text(
        json.dumps(
            index_evidence.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    output = tmp_path / "release-evidence.json"

    evidence = assemble_release_verification_evidence(
        canonical_manifest=canonical.bundle_path / "manifest.json",
        canonical_database=canonical.bundle_path / "canonical.sqlite3",
        lexical_index=lexical_path,
        index_evidence_path=index_path,
        evaluation_report=evaluation_path,
        output=output,
        expected_release_id=RELEASE_ID,
    )

    assert evidence.canonical_bundle_sha256 == canonical.bundle_sha256
    assert evidence.eligible_chunks == lexical.indexed_chunks
    assert evidence.quarantined_pages == 0
    assert evidence.failed_pages == 0
    assert evidence.review_gate is True
    assert evidence.privacy_gate is True
    assert json.loads(output.read_bytes())["retrieval_gate"] is True

    unresolved_output = tmp_path / "unresolved-release-evidence.json"
    monkeypatch.setattr(
        release_module,
        "scan_text",
        lambda *_args, **_kwargs: (
            PrivacyFinding(
                kind="phone",
                location_id="case-synthetic:canonical",
                count=1,
            ),
        ),
    )
    with pytest.raises(release_module.ReleaseError, match="release_evidence_invalid"):
        assemble_release_verification_evidence(
            canonical_manifest=canonical.bundle_path / "manifest.json",
            canonical_database=canonical.bundle_path / "canonical.sqlite3",
            lexical_index=lexical_path,
            index_evidence_path=index_path,
            evaluation_report=evaluation_path,
            output=unresolved_output,
            expected_release_id=RELEASE_ID,
        )
    assert not unresolved_output.exists()


def test_index_evidence_is_written_from_physical_indexes_and_dense_result(
    tmp_path: Path,
) -> None:
    canonical_bundle = _build(
        tmp_path,
        _batch(release_id=RELEASE_ID),
        registry_name="index-issuance.sqlite3",
        output_name="index-canonical-output",
    )
    canonical = canonical_bundle.bundle_path / "canonical.sqlite3"
    lexical_path = tmp_path / "lexical.sqlite3"
    output = tmp_path / "index-attestation.json"
    lexical = build_lexical_index(canonical, lexical_path)
    dense = DenseBuildResult(
        collection_name=f"{lexical.release_id}-bge-m3",
        release_id=lexical.release_id,
        embedding_version="bge-m3-pinned",
        point_count=lexical.indexed_chunks,
        sampled_vector_sha256="6" * 64,
    )

    evidence = create_index_release_evidence(
        canonical_database=canonical,
        lexical_index=lexical_path,
        dense_result=dense,
        output=output,
        expected_release_id=lexical.release_id,
    )

    assert evidence.lexical_chunks == lexical.indexed_chunks
    assert evidence.dense_points == lexical.indexed_chunks
    assert evidence.dense_sample_sha256 == "6" * 64
    assert json.loads(output.read_bytes())["collection_name"] == dense.collection_name

    rejected = tmp_path / "rejected-index-attestation.json"
    with pytest.raises(release_module.ReleaseError, match="index_evidence_invalid"):
        create_index_release_evidence(
            canonical_database=canonical,
            lexical_index=lexical_path,
            dense_result=DenseBuildResult(
                collection_name=dense.collection_name,
                release_id=dense.release_id,
                embedding_version=dense.embedding_version,
                point_count=dense.point_count + 1,
                sampled_vector_sha256=dense.sampled_vector_sha256,
            ),
            output=rejected,
            expected_release_id=lexical.release_id,
        )
    assert not rejected.exists()


def test_backup_tool_lock_pins_exact_linux_amd64_archives() -> None:
    """Catches mutable URLs or unverified host tools entering backup/restore."""
    lock = load_backup_tool_lock(BACKUP_TOOL_LOCK)

    assert lock.age.version == "1.3.1"
    assert lock.age.archive_sha256 == (
        "bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377"
    )
    assert lock.age.archive_size == 10_263_766
    assert lock.age.binary_path == "age/age"
    assert lock.minisign.version == "0.12"
    assert lock.minisign.archive_sha256 == (
        "9a599b48ba6eb7b1e80f12f36b94ceca7c00b7a5173c95c3efc88d9822957e73"
    )
    assert lock.minisign.archive_size == 271_043
    assert lock.minisign.binary_path == "minisign-linux/x86_64/minisign"


def test_backup_tool_lock_rejects_mutable_or_forged_metadata(tmp_path: Path) -> None:
    """Catches a caller replacing the pinned archive with another release asset."""
    payload = json.loads(BACKUP_TOOL_LOCK.read_text(encoding="utf-8"))
    payload["age"]["archive_url"] = "https://example.invalid/age-latest.tar.gz"
    target = tmp_path / "backup-tools.lock.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        release_module.ReleaseError, match="backup_tool_lock_invalid"
    ) as captured:
        load_backup_tool_lock(target)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class AliasBackend:
    def __init__(self, target: str) -> None:
        self.target = target
        self.operations: list[tuple[str, str]] = []

    def current_target(self, alias_name: str) -> str | None:
        assert alias_name == "education-admin-current"
        return self.target

    def compare_and_swap(
        self,
        alias_name: str,
        expected_collection: str,
        new_collection: str,
    ) -> bool:
        assert alias_name == "education-admin-current"
        if self.target != expected_collection:
            return False
        self.operations.append((expected_collection, new_collection))
        self.target = new_collection
        return True


def test_startup_reconciliation_fails_readiness_on_alias_manifest_drift(
    tmp_path: Path,
) -> None:
    """Catches search serving from a collection not named by current.json."""
    root = tmp_path / "release"
    _current_manifest(root)

    matching = reconcile_release_state(
        release_root=root,
        alias_backend=AliasBackend("corpus-old-bge-m3"),
    )
    drifted = reconcile_release_state(
        release_root=root,
        alias_backend=AliasBackend("corpus-other-bge-m3"),
    )

    assert matching.ready is True
    assert matching.error_code is None
    assert drifted.ready is False
    assert drifted.error_code == "alias_manifest_mismatch"


def _attestations(
    tmp_path: Path, *, restore_release_id: str = RELEASE_ID
) -> tuple[Path, Path]:
    verification = write_release_attestation(
        tmp_path / "verification.json",
        ReleaseAttestation(
            kind="verification",
            release_id=RELEASE_ID,
            bundle_sha256="1" * 64,
        ),
    )
    restore = write_release_attestation(
        tmp_path / "restore.json",
        ReleaseAttestation(
            kind="restore",
            release_id=restore_release_id,
            bundle_sha256="1" * 64,
        ),
    )
    return verification, restore


def _current_manifest(root: Path) -> Path:
    root.mkdir(parents=True)
    target = root / "current.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "sen-qa-current-release/v1",
                "release_id": "corpus-20250807123456-11111111",
                "collection_name": "corpus-old-bge-m3",
            }
        ),
        encoding="utf-8",
    )
    return target


def test_failed_evaluation_keeps_current_alias(tmp_path: Path) -> None:
    """Catches a sub-threshold release mutating the production alias."""
    root = tmp_path / "release"
    current = _current_manifest(root)
    verification, restore = _attestations(tmp_path)
    backend = AliasBackend("corpus-old-bge-m3")

    result = promote_release(
        release_root=root,
        release_id=RELEASE_ID,
        candidate_collection=f"{RELEASE_ID}-bge-m3",
        expected_current_collection="corpus-old-bge-m3",
        alias_backend=backend,
        verification_attestation=verification,
        restore_attestation=restore,
        all_release_gates=False,
    )

    assert result.promoted is False
    assert result.error_code == "release_gate_failed"
    assert backend.operations == []
    assert (
        json.loads(current.read_text(encoding="utf-8"))["collection_name"]
        == "corpus-old-bge-m3"
    )


def test_manifest_failure_after_alias_change_rolls_alias_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a half-promoted release leaving Qdrant and the manifest divergent."""
    root = tmp_path / "release"
    current = _current_manifest(root)
    verification, restore = _attestations(tmp_path)
    backend = AliasBackend("corpus-old-bge-m3")
    real_replace = os.replace

    def fail_final_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == current:
            raise OSError("PRIVATE_MANIFEST_REPLACE_SENTINEL")
        real_replace(source, destination)

    monkeypatch.setattr(release_module.os, "replace", fail_final_replace)

    result = promote_release(
        release_root=root,
        release_id=RELEASE_ID,
        candidate_collection=f"{RELEASE_ID}-bge-m3",
        expected_current_collection="corpus-old-bge-m3",
        alias_backend=backend,
        verification_attestation=verification,
        restore_attestation=restore,
        all_release_gates=True,
    )

    assert result.promoted is False
    assert result.error_code == "manifest_replace_failed"
    assert backend.operations == [
        ("corpus-old-bge-m3", f"{RELEASE_ID}-bge-m3"),
        (f"{RELEASE_ID}-bge-m3", "corpus-old-bge-m3"),
    ]
    assert backend.target == "corpus-old-bge-m3"
    assert (
        json.loads(current.read_text(encoding="utf-8"))["collection_name"]
        == "corpus-old-bge-m3"
    )


def test_promotion_requires_matching_verify_and_restore_attestations(
    tmp_path: Path,
) -> None:
    """Catches evidence from another release authorizing this release."""
    root = tmp_path / "release"
    _current_manifest(root)
    verification, restore = _attestations(
        tmp_path,
        restore_release_id="corpus-20250809123456-22222222",
    )
    backend = AliasBackend("corpus-old-bge-m3")

    result = promote_release(
        release_root=root,
        release_id=RELEASE_ID,
        candidate_collection=f"{RELEASE_ID}-bge-m3",
        expected_current_collection="corpus-old-bge-m3",
        alias_backend=backend,
        verification_attestation=verification,
        restore_attestation=restore,
        all_release_gates=True,
    )

    assert result.promoted is False
    assert result.error_code == "attestation_mismatch"
    assert backend.operations == []


def test_attestation_writer_revalidates_forged_model(tmp_path: Path) -> None:
    """Catches model_construct bypassing the immutable release evidence schema."""
    forged = ReleaseAttestation.model_construct(
        schema_version="sen-qa-release-attestation/v1",
        kind="verification",
        release_id="not-a-release",
        bundle_sha256="PRIVATE_FORGED_ATTESTATION_SENTINEL",
    )

    with pytest.raises(
        release_module.ReleaseError, match="attestation_invalid"
    ) as captured:
        write_release_attestation(tmp_path / "forged.json", forged)

    assert not (tmp_path / "forged.json").exists()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_promotion_rejects_duplicate_attestation_keys(tmp_path: Path) -> None:
    """Catches alternate JSON parsers authorizing different release evidence."""
    root = tmp_path / "release"
    _current_manifest(root)
    verification = tmp_path / "verification.json"
    verification.write_text(
        (
            '{"bundle_sha256":"'
            + "1" * 64
            + '","kind":"verification","release_id":"'
            + RELEASE_ID
            + '","release_id":"'
            + RELEASE_ID
            + '","schema_version":"sen-qa-release-attestation/v1"}\n'
        ),
        encoding="utf-8",
    )
    restore = write_release_attestation(
        tmp_path / "restore.json",
        ReleaseAttestation(
            kind="restore",
            release_id=RELEASE_ID,
            bundle_sha256="1" * 64,
        ),
    )
    backend = AliasBackend("corpus-old-bge-m3")

    result = promote_release(
        release_root=root,
        release_id=RELEASE_ID,
        candidate_collection=f"{RELEASE_ID}-bge-m3",
        expected_current_collection="corpus-old-bge-m3",
        alias_backend=backend,
        verification_attestation=verification,
        restore_attestation=restore,
        all_release_gates=True,
    )

    assert result.promoted is False
    assert result.error_code == "attestation_mismatch"
    assert backend.operations == []
