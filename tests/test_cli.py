import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import src.cli as cli_module
from src.cli import app
from src.evaluation.release_report import ReleaseEvaluationReport
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    ReviewValidationError,
    SegmentManifest,
    VerifiedCanonicalReviewRegistry,
)
from src.release import IndexReleaseEvidence, ReleaseVerificationEvidence
from src.retrieval.dense import DenseBuildResult
from src.retrieval.lexical import build_lexical_index
from tests.ingestion.test_parse_metadata import (
    _native_quarantine_records,
    _ocr_quarantine_record,
    _write_jsonl,
    _write_manifest,
)
from tests.retrieval.test_lexical import _write_canonical_database

CONTENT_A = "a" * 64
CONTENT_B = "b" * 64
CASE_1 = "senqa-2025-contract-contract-general-1"
CASE_2 = "senqa-2025-contract-contract-general-2"


def _registry() -> VerifiedCanonicalReviewRegistry:
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=case_id,
                content_sha256=content_hash,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="critical-fields-unverified",
                    ),
                ),
            )
            for case_id, content_hash in (
                (CASE_1, CONTENT_A),
                (CASE_2, CONTENT_B),
            )
        ]
    )
    rendered = registry.to_bytes()
    return CanonicalReviewRegistry.from_bytes(
        rendered, expected_sha256=hashlib.sha256(rendered).hexdigest()
    )


def test_cli_reports_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "education-admin-rag 0.1.0"


def test_evaluate_release_cli_passes_exact_evidence_paths_and_reports_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tuple(
        tmp_path / name
        for name in (
            "canonical.sqlite3",
            "dev.jsonl",
            "blind.jsonl",
            "labels.jsonl",
            "ingestion.jsonl",
            "substring.jsonl",
            "lexical.jsonl",
            "dense.jsonl",
            "hybrid.jsonl",
        )
    )
    for path in inputs:
        path.write_bytes(b"evidence")
    output = tmp_path / "evaluation-report.json"
    captured: dict[str, object] = {}

    def fake_create(**kwargs: object) -> ReleaseEvaluationReport:
        captured.update(kwargs)
        return ReleaseEvaluationReport.model_construct(
            gold_items=200,
            blind_items=60,
            ingestion_gate=True,
            retrieval_gate=True,
        )

    monkeypatch.setattr(
        cli_module,
        "create_release_evaluation_report",
        fake_create,
    )
    option_names = (
        "canonical-db",
        "dev-gold",
        "blind-gold",
        "blind-labels",
        "ingestion-observations",
        "substring-observations",
        "lexical-observations",
        "dense-observations",
        "hybrid-observations",
    )
    arguments = [
        "evaluate-release-evidence",
        "--release-id",
        "corpus-20250808123456-deadbeef",
    ]
    for option, path in zip(option_names, inputs, strict=True):
        arguments.extend((f"--{option}", str(path)))
    arguments.extend(("--output", str(output)))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "gold_items=200 blind_items=60 ingestion_gate=1 retrieval_gate=1 failed=0"
    )
    assert captured["canonical_database"] == inputs[0]
    assert captured["retrieval_paths"] == {
        system: path
        for system, path in zip(
            ("substring", "lexical", "dense", "hybrid"), inputs[5:], strict=True
        )
    }


def test_assemble_release_evidence_cli_uses_measured_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tuple(
        tmp_path / name
        for name in (
            "manifest.json",
            "canonical.sqlite3",
            "lexical.sqlite3",
            "index-attestation.json",
            "evaluation-report.json",
        )
    )
    for path in inputs:
        path.write_bytes(b"evidence")
    output = tmp_path / "release-evidence.json"
    captured: dict[str, object] = {}

    def fake_assemble(**kwargs: object) -> ReleaseVerificationEvidence:
        captured.update(kwargs)
        return ReleaseVerificationEvidence.model_construct(
            canonical_bundle_sha256="a" * 64
        )

    monkeypatch.setattr(
        cli_module,
        "assemble_release_verification_evidence",
        fake_assemble,
    )
    result = CliRunner().invoke(
        app,
        [
            "assemble-release-evidence",
            "--release-id",
            "corpus-20250808123456-deadbeef",
            "--canonical-manifest",
            str(inputs[0]),
            "--canonical-db",
            str(inputs[1]),
            "--lexical-index",
            str(inputs[2]),
            "--index-evidence",
            str(inputs[3]),
            "--evaluation-report",
            str(inputs[4]),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == f"bundle_sha256={'a' * 64} failed=0"
    assert captured["canonical_manifest"] == inputs[0]
    assert captured["output"] == output


def test_build_dense_index_cli_builds_candidate_and_writes_index_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    lexical = tmp_path / "lexical.sqlite3"
    lock = tmp_path / "models.lock.json"
    model_root = tmp_path / "model"
    output = tmp_path / "index-attestation.json"
    for path in (canonical, lexical, lock):
        path.write_bytes(b"input")
    model_root.mkdir()
    monkeypatch.setenv("SEN_QA_EMBEDDING_MODEL_ROOT", str(model_root))
    monkeypatch.setenv("SEN_QA_EMBEDDING_LOCK_SHA256", "a" * 64)
    captured: dict[str, object] = {}

    class FakeEncoder:
        @classmethod
        def from_lock(cls, *args: object, **kwargs: object) -> object:
            captured["encoder"] = (args, kwargs)
            return object()

    class FakeClient:
        def close(self) -> None:
            captured["closed"] = True

    dense = DenseBuildResult(
        collection_name="corpus-20250808123456-deadbeef-bge-m3",
        release_id="corpus-20250808123456-deadbeef",
        embedding_version="bge-m3-pinned",
        point_count=3,
        sampled_vector_sha256="b" * 64,
    )
    monkeypatch.setattr(cli_module, "DenseEncoder", FakeEncoder)
    monkeypatch.setattr(cli_module, "create_qdrant_client", lambda _url: FakeClient())
    monkeypatch.setattr(cli_module, "build_dense_candidate", lambda *a, **k: dense)

    def fake_evidence(**kwargs: object) -> IndexReleaseEvidence:
        captured.update(kwargs)
        return IndexReleaseEvidence(
            schema_version="sen-qa-index-evidence/v1",
            release_id=dense.release_id,
            canonical_database_sha256="c" * 64,
            lexical_index_sha256="d" * 64,
            dense_sample_sha256=dense.sampled_vector_sha256,
            eligible_chunks=3,
            lexical_chunks=3,
            dense_points=3,
            collection_name=dense.collection_name,
        )

    monkeypatch.setattr(cli_module, "create_index_release_evidence", fake_evidence)
    result = CliRunner().invoke(
        app,
        [
            "build-dense-index",
            "--canonical-db",
            str(canonical),
            "--lexical-index",
            str(lexical),
            "--output",
            str(output),
            "--release-id",
            dense.release_id,
            "--lock",
            str(lock),
            "--qdrant-url",
            "http://qdrant:6333",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "dense_points=3 failed=0"
    assert captured["dense_result"] == dense
    assert captured["output"] == output
    assert captured["closed"] is True


def test_module_entrypoint_reports_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "education-admin-rag 0.1.0"


def test_start_release_cli_creates_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the operator entrypoint omitting or expanding the trusted envelope."""
    source_root = tmp_path / "source"
    artifact_root = tmp_path / "artifacts"
    private_eval_root = tmp_path / "private-eval"
    for root in (source_root, artifact_root, private_eval_root):
        root.mkdir()
    env_file = artifact_root / "active-release.env"
    monkeypatch.setattr(
        cli_module,
        "_release_clock_provider",
        lambda: datetime(2025, 8, 8, 12, 34, 56, tzinfo=UTC),
    )
    monkeypatch.setattr(
        cli_module,
        "_release_git_sha_provider",
        lambda: "deadbeef" + "1" * 32,
    )

    result = CliRunner().invoke(
        app,
        [
            "start-release",
            "--source-root",
            str(source_root),
            "--artifact-root",
            str(artifact_root),
            "--private-eval-root",
            str(private_eval_root),
            "--env-file",
            str(env_file),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "release_id=corpus-20250808123456-deadbeef failed=0"
    )
    assert env_file.exists()


def test_start_release_cli_sanitizes_git_and_storage_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches source paths or subprocess diagnostics leaking on setup failure."""
    sentinel = "PRIVATE_RELEASE_SETUP_SENTINEL"
    source_root = tmp_path / sentinel
    artifact_root = tmp_path / "artifacts"
    private_eval_root = tmp_path / "private-eval"
    for root in (source_root, artifact_root, private_eval_root):
        root.mkdir()
    monkeypatch.setattr(
        cli_module,
        "_release_git_sha_provider",
        lambda: (_ for _ in ()).throw(OSError(sentinel)),
    )

    result = CliRunner().invoke(
        app,
        [
            "start-release",
            "--source-root",
            str(source_root),
            "--artifact-root",
            str(artifact_root),
            "--private-eval-root",
            str(private_eval_root),
            "--env-file",
            str(artifact_root / "active-release.env"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=release_setup_failed"
    assert sentinel not in result.stdout + result.stderr
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None


def test_backup_manifest_cli_creates_then_verifies_exact_bundle(
    tmp_path: Path,
) -> None:
    """Catches shell backup orchestration using divergent hash implementations."""
    from src.release import BACKUP_PAYLOAD_PATHS

    root = tmp_path / "backup"
    for index, relative in enumerate(BACKUP_PAYLOAD_PATHS, start=1):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"payload-{index}".encode("ascii"))

    created = CliRunner().invoke(
        app,
        [
            "backup-manifest",
            "--root",
            str(root),
            "--release-id",
            "corpus-20250808123456-deadbeef",
        ],
    )
    verified = CliRunner().invoke(app, ["verify-backup", "--root", str(root)])

    assert created.exit_code == verified.exit_code == 0
    assert re.fullmatch(r"bundle_sha256=[0-9a-f]{64} failed=0\n", created.stdout)
    assert verified.stdout == created.stdout


def test_storage_policy_env_cli_emits_only_shell_quoted_policy_fields(
    tmp_path: Path,
) -> None:
    """Catches shell probes using ambient identities or unvalidated root paths."""
    policy = tmp_path / "storage-policy.toml"
    policy.write_text(
        "\n".join(
            (
                'schema_version = "sen-qa-storage-policy/v1"',
                "ingestion_uid = 21001",
                "search_uid = 21002",
                "evaluator_uid = 21003",
                "reviewer_gid = 22001",
                f'source_root = "{tmp_path}/source"',
                f'artifact_root = "{tmp_path}/artifacts"',
                f'private_eval_root = "{tmp_path}/private-eval"',
            )
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["storage-policy-env", "--policy", str(policy)])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "SEN_QA_POLICY_INGESTION_UID=21001",
        "SEN_QA_POLICY_SEARCH_UID=21002",
        "SEN_QA_POLICY_EVALUATOR_UID=21003",
        "SEN_QA_POLICY_REVIEWER_GID=22001",
        f"SEN_QA_POLICY_SOURCE_ROOT={tmp_path}/source",
        f"SEN_QA_POLICY_ARTIFACT_ROOT={tmp_path}/artifacts",
        f"SEN_QA_POLICY_PRIVATE_EVAL_ROOT={tmp_path}/private-eval",
    ]


def test_inspect_lexical_plan_reports_only_safe_plan_metadata(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    index = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    build_lexical_index(canonical, index)

    result = CliRunner().invoke(
        app,
        [
            "inspect-lexical-plan",
            "--db",
            str(index),
            "--query",
            "학교회계 제12조 PRIVATE_QUERY_SENTINEL",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith(
        "uses_fts=1 full_table_scan=0 restricted_candidates=0 plan_steps="
    )
    assert result.stdout.rstrip().endswith("failed=0")
    assert "PRIVATE_QUERY_SENTINEL" not in result.stdout


def test_build_lexical_index_cli_publishes_candidate_without_alias_mutation(
    tmp_path: Path,
) -> None:
    """Catches the release wrapper omitting the real atomic lexical builder."""
    canonical = tmp_path / "canonical.sqlite3"
    index = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)

    result = CliRunner().invoke(
        app,
        [
            "build-lexical-index",
            "--canonical-db",
            str(canonical),
            "--output",
            str(index),
        ],
    )

    assert result.exit_code == 0
    assert re.fullmatch(
        r"indexed_chunks=2 skipped_chunks=2 config_sha256=[0-9a-f]{64} failed=0\n",
        result.stdout,
    )
    assert index.is_file()


def test_inspect_lexical_plan_sanitizes_invalid_index_error(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_text("PRIVATE_INDEX_SENTINEL", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["inspect-lexical-plan", "--db", str(invalid), "--query", "학교회계"],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=index_invalid"
    assert "PRIVATE_INDEX_SENTINEL" not in result.stdout


def _queued_database(tmp_path: Path) -> Path:
    database = tmp_path / "review.sqlite3"
    with ReviewStore(database, canonical_registry=_registry()) as store:
        store.enqueue(
            CASE_1,
            content_sha256=CONTENT_A,
            reason="quality_gate",
            actor_id="quality-gate",
        )
    return database


def _review_args(database: Path) -> list[str]:
    return [
        "--db",
        str(database),
        "--case-id",
        CASE_1,
        "--content-sha256",
        CONTENT_A,
    ]


def test_review_cli_rejects_spoofed_reviewer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )

    result = CliRunner().invoke(
        app,
        [
            "review",
            "verify-fields",
            *_review_args(database),
            "--reason",
            "fields_checked",
            "--reviewer-id",
            "uid:502:attacker",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout.strip() == "updated=0 failed=1 error_code=actor_mismatch"
    with ReviewStore(database) as store:
        assert store.get(CASE_1).critical_field_review == "pending"


def test_review_cli_accepts_current_pwd_username_but_records_uid_bound_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    result = CliRunner().invoke(
        app,
        [
            "review",
            "verify-fields",
            *_review_args(database),
            "--reason",
            "fields_checked",
            "--reviewer-id",
            "reviewer-a",
        ],
    )

    assert result.exit_code == 0
    with ReviewStore(database) as store:
        assert store.events(CASE_1)[-1].actor_id == "uid:501:reviewer-a"


def test_review_cli_fails_closed_when_os_actor_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)

    def unresolved_actor() -> str:
        raise ReviewValidationError("unresolvable")

    monkeypatch.setattr(cli_module, "_review_actor_provider", unresolved_actor)
    result = CliRunner().invoke(
        app,
        [
            "review",
            "verify-fields",
            *_review_args(database),
            "--reason",
            "fields_checked",
        ],
    )

    assert result.exit_code == 2
    assert (
        result.stdout.strip() == "updated=0 failed=1 error_code=actor_resolution_failed"
    )


def test_review_cli_single_case_flow_requires_independent_actor_and_explicit_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    runner = CliRunner()

    verify = runner.invoke(
        app,
        [
            "review",
            "verify-fields",
            *_review_args(database),
            "--reason",
            "fields_checked",
        ],
    )
    search = runner.invoke(
        app,
        [
            "review",
            "approve-search",
            *_review_args(database),
            "--reason",
            "search_checked",
        ],
    )
    unchecked = runner.invoke(
        app,
        [
            "review",
            "approve-answer",
            *_review_args(database),
            "--reason",
            "answer_checked",
        ],
    )

    assert verify.exit_code == 0
    assert (
        verify.stdout.strip()
        == f"updated=1 case_id={CASE_1} status=needs_review failed=0"
    )
    assert search.exit_code == 0
    assert (
        search.stdout.strip()
        == f"updated=1 case_id={CASE_1} status=search_approved failed=0"
    )
    assert unchecked.exit_code == 2
    assert (
        unchecked.stdout.strip()
        == "updated=0 failed=1 error_code=verification_required"
    )

    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:502:reviewer-b"
    )
    answer = runner.invoke(
        app,
        [
            "review",
            "approve-answer",
            *_review_args(database),
            "--reason",
            "answer_checked",
            "--content-verified",
            "--basis-verified",
            "--privacy-verified",
        ],
    )
    assert answer.exit_code == 0
    assert (
        answer.stdout.strip() == f"updated=1 case_id={CASE_1} status=approved failed=0"
    )


def test_review_cli_fail_closed_output_does_not_echo_hash_or_candidate_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    drifted = "b" * 64

    result = CliRunner().invoke(
        app,
        [
            "review",
            "verify-fields",
            "--db",
            str(database),
            "--case-id",
            CASE_1,
            "--content-sha256",
            drifted,
            "--reason",
            "fields_checked",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "updated=0 failed=1 error_code=review_conflict"
    assert CONTENT_A not in result.stdout
    assert drifted not in result.stdout


def test_review_cli_rejects_value_bearing_reason_before_interactive_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    unsafe_reason = "010-1234-5678"
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )

    result = CliRunner().invoke(
        app,
        [
            "review",
            "run",
            "--mode",
            "critical-fields-all",
            "--db",
            str(database),
            "--manifest",
            str(manifest_path),
            "--manifest-sha256",
            hashlib.sha256(manifest).hexdigest(),
            "--reason",
            unsafe_reason,
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "updated=0 failed=1 error_code=invalid_input"
    assert unsafe_reason not in result.stdout
    assert CASE_1 not in result.stdout
    assert CONTENT_A not in result.stdout


def test_review_cli_batch_and_ready_outputs_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    with ReviewStore(database) as store:
        store.verify_critical_fields(
            CASE_1,
            reviewer_id="uid:501:reviewer-a",
            reviewed_content_sha256=CONTENT_A,
            reason="fields_checked",
        )
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    runner = CliRunner()

    approval = runner.invoke(
        app,
        [
            "review",
            "approve-search-batch",
            "--db",
            str(database),
            "--manifest",
            str(manifest_path),
            "--manifest-sha256",
            manifest_hash,
            "--reason",
            "segment_checked",
        ],
    )
    ready = runner.invoke(
        app,
        ["review", "assert-ready", "--db", str(database), "--purpose", "answer"],
    )

    assert approval.exit_code == 0
    assert approval.stdout.strip() == "updated=1 failed=0"
    assert ready.exit_code == 1
    assert ready.stdout.strip() == (
        "ready=0 total=1 eligible=0 blockers=search_approved:1 failed=1"
    )
    assert CASE_1 not in approval.stdout + ready.stdout
    assert CONTENT_A not in approval.stdout + ready.stdout


def test_review_cli_run_modes_use_hashed_manifest_and_second_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    runner = CliRunner()
    common = [
        "--db",
        str(database),
        "--manifest",
        str(manifest_path),
        "--manifest-sha256",
        manifest_hash,
        "--reason",
        "manual_run",
    ]
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )

    critical = runner.invoke(
        app,
        ["review", "run", "--mode", "critical-fields-all", *common],
        input="y\n",
    )
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:502:reviewer-b"
    )
    answer = runner.invoke(
        app,
        [
            "review",
            "run",
            "--mode",
            "answer-and-basis-all",
            *common,
            "--content-verified",
            "--basis-verified",
            "--privacy-verified",
        ],
        input="y\n",
    )

    assert critical.exit_code == 0
    assert f"case_id={CASE_1}" in critical.stdout
    assert f"content_sha256={CONTENT_A}" in critical.stdout
    assert "page_id=13 bbox=10,20,100,200" in critical.stdout
    assert "reason=manual_run" not in critical.stdout
    assert "reason_code=critical-fields-unverified count=1" in critical.stdout
    assert critical.stdout.rstrip().endswith(
        "updated=1 mode=critical-fields-all failed=0"
    )
    assert answer.exit_code == 0
    assert answer.stdout.rstrip().endswith(
        "updated=1 mode=answer-and-basis-all failed=0"
    )
    with ReviewStore(database) as store:
        assert store.get(CASE_1).review_status == "approved"
        assert [event.batch_manifest_sha256 for event in store.events(CASE_1)[-3:]] == [
            manifest_hash,
            manifest_hash,
            manifest_hash,
        ]


def test_review_cli_run_decline_leaves_case_unchanged_without_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )

    result = CliRunner().invoke(
        app,
        [
            "review",
            "run",
            "--mode",
            "critical-fields-all",
            "--db",
            str(database),
            "--manifest",
            str(manifest_path),
            "--manifest-sha256",
            hashlib.sha256(manifest).hexdigest(),
            "--reason",
            "manual_run",
        ],
        input="n\n",
    )

    assert result.exit_code == 2
    assert result.stdout.rstrip().endswith(
        "updated=0 failed=1 error_code=confirmation_required"
    )
    with ReviewStore(database) as store:
        assert store.get(CASE_1).review_status == "needs_review"
        assert [event.action for event in store.events(CASE_1)] == ["enqueue"]


@pytest.mark.parametrize("first_input", ["y\nn\n", "y\n"])
def test_review_cli_run_reports_partial_commits_and_resumes_matching_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_input: str,
) -> None:
    database = tmp_path / "review.sqlite3"
    with ReviewStore(database, canonical_registry=_registry()) as store:
        for case_id, content_hash in ((CASE_1, CONTENT_A), (CASE_2, CONTENT_B)):
            store.enqueue(
                case_id,
                content_sha256=content_hash,
                reason="quality_gate",
                actor_id="quality-gate",
            )
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[
            ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A),
            ReviewReference(case_id=CASE_2, content_sha256=CONTENT_B),
        ],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    command = [
        "review",
        "run",
        "--mode",
        "critical-fields-all",
        "--db",
        str(database),
        "--manifest",
        str(manifest_path),
        "--manifest-sha256",
        manifest_hash,
        "--reason",
        "manual_run",
    ]
    runner = CliRunner()

    partial = runner.invoke(app, command, input=first_input)

    assert partial.exit_code == 2
    assert partial.stdout.rstrip().endswith(
        "updated=1 failed=1 error_code=confirmation_required"
    )
    with ReviewStore(database) as store:
        assert store.get(CASE_1).review_status == "search_approved"
        assert store.get(CASE_2).review_status == "needs_review"

    resumed = runner.invoke(app, command, input="y\n")

    assert resumed.exit_code == 0
    assert resumed.stdout.count("confirm reviewed metadata") == 1
    assert resumed.stdout.rstrip().endswith(
        "updated=1 mode=critical-fields-all failed=0"
    )
    with ReviewStore(database) as store:
        assert store.get(CASE_1).review_status == "search_approved"
        assert store.get(CASE_2).review_status == "search_approved"
        assert [event.action for event in store.events(CASE_1)] == [
            "enqueue",
            "verify_fields",
            "approve_search",
        ]


def test_review_cli_run_reports_commits_before_a_later_state_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "review.sqlite3"
    with ReviewStore(database, canonical_registry=_registry()) as store:
        for case_id, content_hash in ((CASE_1, CONTENT_A), (CASE_2, CONTENT_B)):
            store.enqueue(
                case_id,
                content_sha256=content_hash,
                reason="quality_gate",
                actor_id="quality-gate",
            )
    manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[
            ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A),
            ReviewReference(case_id=CASE_2, content_sha256=CONTENT_B),
        ],
    ).to_bytes()
    manifest_path = tmp_path / "segment.json"
    manifest_path.write_bytes(manifest)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    prompt_count = 0

    def confirm_with_concurrent_rejection(_prompt: str, *, default: bool) -> bool:
        nonlocal prompt_count
        assert default is False
        prompt_count += 1
        if prompt_count == 2:
            with ReviewStore(database) as store:
                store.reject(
                    CASE_2,
                    reviewer_id="uid:599:concurrent-reviewer",
                    reviewed_content_sha256=CONTENT_B,
                    reason="invalid_layout",
                )
        return True

    monkeypatch.setattr(typer, "confirm", confirm_with_concurrent_rejection)

    result = CliRunner().invoke(
        app,
        [
            "review",
            "run",
            "--mode",
            "critical-fields-all",
            "--db",
            str(database),
            "--manifest",
            str(manifest_path),
            "--manifest-sha256",
            hashlib.sha256(manifest).hexdigest(),
            "--reason",
            "manual_run",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.rstrip().endswith(
        "updated=1 failed=1 error_code=review_conflict"
    )
    with ReviewStore(database) as store:
        assert store.get(CASE_1).review_status == "search_approved"
        assert store.get(CASE_2).review_status == "rejected"


def test_parse_metadata_cli_emits_one_canonical_value_free_json_line(
    tmp_path: Path,
) -> None:
    """Catches the integration command printing input paths or extractor content."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "PRIVATE-INPUT-PATH.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))

    result = CliRunner().invoke(
        app,
        [
            "parse-metadata",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--year",
            "2020",
            "--pages",
            "all",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    assert payload["metadata_schema"] == "sen-qa-parse-metadata-v1"
    assert payload["record_counts"]["quarantined"] == 2
    assert str(input_path) not in result.stdout
    assert "PRIVATE-INPUT-PATH" not in result.stdout
    assert result.stdout.strip() == json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_stage_review_corpus_cli_reports_only_counts_and_registry_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root = tmp_path / "raw-pages"
    input_root.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"{}\n")
    fingerprint = "f" * 64

    class Registry:
        fingerprint_sha256 = fingerprint

    class Batch:
        cases = (object(), object())
        quarantine_count = 3
        registry = Registry()

    batch = Batch()
    monkeypatch.setattr(
        cli_module,
        "prepare_review_corpus_from_artifacts",
        lambda *_args, **_kwargs: batch,
    )
    monkeypatch.setattr(
        cli_module,
        "write_review_package",
        lambda root, *, release_id, batch: root / "review",
    )

    result = CliRunner().invoke(
        app,
        [
            "stage-review-corpus",
            "--input-root",
            str(input_root),
            "--manifest",
            str(manifest),
            "--output-root",
            str(tmp_path),
            "--release-id",
            "corpus-20250808123456-deadbeef",
            "--ingestion-version",
            "ingestion-v1",
        ],
        env={"SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64},
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.strip() == (
        f"cases=2 quarantines=3 registry_sha256={fingerprint} failed=0"
    )


def test_review_export_ready_cli_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "review"
    package.mkdir()
    monkeypatch.setattr(
        cli_module,
        "export_review_ready",
        lambda *_args, **_kwargs: package / "review-ready.attestation.json",
    )

    result = CliRunner().invoke(
        app,
        [
            "review",
            "export-ready",
            "--package",
            str(package),
            "--release-id",
            "corpus-20250808123456-deadbeef",
            "--registry-sha256",
            "f" * 64,
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.strip() == "ready=1 failed=0"


def test_parse_metadata_cli_errors_are_fixed_and_source_value_free(
    tmp_path: Path,
) -> None:
    """Catches JSON/Pydantic details escaping through stdout, stderr, or Exit context."""
    manifest_path, _ = _write_manifest(tmp_path)
    sentinel = "PRIVATE-RAW-SENTINEL"
    input_path = tmp_path / f"{sentinel}.jsonl"
    input_path.write_text('{"status":"' + sentinel + '"}\n', encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "parse-metadata",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--year",
            "2020",
            "--pages",
            "all",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=input_invalid"
    assert result.stderr == ""
    assert sentinel not in result.stdout + result.stderr
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None
    chain: list[BaseException] = []
    current: BaseException | None = result.exception
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    disclosed = "".join(str(error) + repr(error) for error in chain)
    assert sentinel not in disclosed
    assert str(input_path) not in disclosed


def test_parse_metadata_cli_deep_json_has_one_fixed_cause_free_error(
    tmp_path: Path,
) -> None:
    """Catches JSON recursion errors escaping Click's fixed public boundary."""
    manifest_path, _ = _write_manifest(tmp_path)
    input_path = tmp_path / "deep.jsonl"
    input_path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n")

    result = CliRunner().invoke(
        app,
        [
            "parse-metadata",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--year",
            "2020",
            "--pages",
            "all",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=input_invalid"
    assert result.stderr == ""
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None


def test_parse_metadata_cli_canonical_failure_has_one_fixed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches final Pydantic/JSON rendering failures escaping the CLI boundary."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))

    def fail_canonical(metadata: object) -> bytes:
        del metadata
        raise OverflowError("PRIVATE-CANONICAL-FAILURE")

    monkeypatch.setattr(cli_module, "canonical_metadata_bytes", fail_canonical)
    result = CliRunner().invoke(
        app,
        [
            "parse-metadata",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--year",
            "2020",
            "--pages",
            "all",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=parse_failed"
    assert result.stderr == ""
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None


def test_parse_metadata_cli_requires_matching_ocr_image_digest(
    tmp_path: Path,
) -> None:
    """Catches OCR metadata running without binding the ingestion container image."""
    manifest_path, documents = _write_manifest(tmp_path)
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(documents[-1], image_digest=digest),),
    )
    command = [
        "parse-metadata",
        "--input",
        str(input_path),
        "--manifest",
        str(manifest_path),
        "--year",
        "2025",
        "--pages",
        "2",
    ]
    runner = CliRunner()

    missing = runner.invoke(
        app,
        command,
        env={"SEN_QA_INGESTION_IMAGE_DIGEST": ""},
    )
    matched = runner.invoke(
        app,
        command,
        env={"SEN_QA_INGESTION_IMAGE_DIGEST": digest},
    )

    assert missing.exit_code == 1
    assert missing.stdout.strip() == "failed=1 error_code=image_digest_invalid"
    assert matched.exit_code == 0
    assert json.loads(matched.stdout)["extraction_source"] == "ocr"


def test_review_cli_reject_is_terminal_and_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _queued_database(tmp_path)
    monkeypatch.setattr(
        cli_module, "_review_actor_provider", lambda: "uid:501:reviewer-a"
    )
    result = CliRunner().invoke(
        app,
        [
            "review",
            "reject",
            *_review_args(database),
            "--reason",
            "invalid_layout",
        ],
    )
    assert result.exit_code == 0
    assert (
        result.stdout.strip() == f"updated=1 case_id={CASE_1} status=rejected failed=0"
    )
