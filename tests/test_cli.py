import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import src.cli as cli_module
from src.cli import app
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    ReviewValidationError,
    SegmentManifest,
    VerifiedCanonicalReviewRegistry,
)
from tests.ingestion.test_parse_metadata import (
    _native_quarantine_records,
    _ocr_quarantine_record,
    _write_jsonl,
    _write_manifest,
)

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


def test_module_entrypoint_reports_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "education-admin-rag 0.1.0"


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
