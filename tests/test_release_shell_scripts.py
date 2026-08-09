from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = (
    "build-corpus.sh",
    "build-indexes.sh",
    "evaluate-release.sh",
    "verify-release.sh",
    "backup-release.sh",
    "restore-release.sh",
    "promote-release.sh",
    "verify-storage-permissions.sh",
)
RELEASE_ID = "corpus-20250808123456-deadbeef"


def _release_environment(tmp_path: Path) -> dict[str, str]:
    environment = {"PATH": os.environ["PATH"]}
    for name in ("source", "artifacts", "private-eval"):
        (tmp_path / name).mkdir()
    environment.update(
        {
            "SEN_QA_RELEASE_ID": RELEASE_ID,
            "SEN_QA_SOURCE_ROOT": str(tmp_path / "source"),
            "SEN_QA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "SEN_QA_PRIVATE_EVAL_ROOT": str(tmp_path / "private-eval"),
        }
    )
    return environment


@pytest.mark.parametrize("script", SCRIPTS)
def test_release_script_fails_closed_without_active_release(script: str) -> None:
    """Catches ambient defaults accidentally targeting production storage."""
    environment = {"PATH": os.environ["PATH"]}
    completed = subprocess.run(
        ["bash", f"scripts/{script}"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "failed=1 error_code=release_environment_missing\n"


def test_release_scripts_have_valid_shell_syntax() -> None:
    """Catches an operator discovering syntax drift during the release window."""
    completed = subprocess.run(
        ["bash", "-n", *(f"scripts/{script}" for script in SCRIPTS)],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr


def test_backup_rejects_same_storage_tree_before_reading_keys(tmp_path: Path) -> None:
    """Catches a same-NAS artifacts directory being mislabeled external backup."""
    environment = _release_environment(tmp_path)
    environment["SEN_QA_BACKUP_IMAGE"] = "sen-qa-backup:v1@sha256:" + "a" * 64
    target = tmp_path / "artifacts" / "backup"
    target.mkdir()

    completed = subprocess.run(
        ["bash", "scripts/backup-release.sh", str(target)],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 2
    assert completed.stderr == "failed=1 error_code=backup_target_not_external\n"


def test_index_build_stops_before_container_without_review_checkpoint(
    tmp_path: Path,
) -> None:
    """Catches indexing machine-extracted cases before the human checkpoint."""
    environment = _release_environment(tmp_path)
    environment["SEN_QA_INDEXER_IMAGE"] = "sen-qa-index:v1@sha256:" + "b" * 64
    canonical = (
        tmp_path
        / "artifacts"
        / "releases"
        / RELEASE_ID
        / "canonical"
        / "canonical.sqlite3"
    )
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"SQLite format 3\0")

    completed = subprocess.run(
        ["bash", "scripts/build-indexes.sh"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 1
    assert completed.stderr == "failed=1 error_code=review_checkpoint_missing\n"


def test_storage_probe_rejects_policy_for_different_roots(tmp_path: Path) -> None:
    """Catches a valid policy file being replayed against another release tree."""
    environment = _release_environment(tmp_path)
    environment["SEN_QA_PERMISSION_PROBE_IMAGE"] = "sen-qa-backup:v1@sha256:" + "c" * 64
    policy = tmp_path / "policy.toml"
    policy.write_text(
        """schema_version = "sen-qa-storage-policy/v1"
ingestion_uid = 21001
search_uid = 21002
evaluator_uid = 21003
reviewer_gid = 22001
source_root = "/different/source"
artifact_root = "/different/artifacts"
private_eval_root = "/different/private"
""",
        encoding="utf-8",
    )
    environment["SEN_QA_STORAGE_POLICY"] = str(policy)

    completed = subprocess.run(
        ["bash", "scripts/verify-storage-permissions.sh"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stderr == "failed=1 error_code=storage_policy_root_mismatch\n"


def test_active_release_rejects_nested_roots_before_any_job(tmp_path: Path) -> None:
    """Catches a hand-edited active environment bypassing start-release isolation."""
    environment = _release_environment(tmp_path)
    nested = tmp_path / "artifacts" / "private"
    nested.mkdir()
    environment["SEN_QA_PRIVATE_EVAL_ROOT"] = str(nested)

    completed = subprocess.run(
        ["bash", "scripts/evaluate-release.sh"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 2
    assert completed.stderr == "failed=1 error_code=release_environment_invalid\n"


def test_runbooks_preserve_predeployment_blockers_and_no_fake_keys() -> None:
    """Catches documentation silently turning a missing authority into a default."""
    index_runbook = Path("docs/runbooks/index-release.md").read_text(encoding="utf-8")
    backup_runbook = Path("docs/runbooks/backup-restore.md").read_text(encoding="utf-8")
    recipients = Path("config/backup-recipients.txt.example").read_text(
        encoding="utf-8"
    )

    assert "stage=review_pending" in index_runbook
    assert "index-attestation.json" in index_runbook
    assert "qdrant_alias_broker_required" in index_runbook
    assert "restore_evaluation_driver_required" in backup_runbook
    assert "age1" not in recipients
    assert "AGE-SECRET-KEY" not in recipients


def test_evaluation_script_runs_gold_bound_aggregate_evaluator() -> None:
    """Catches replacement of measured evaluation with a generic TODO blocker."""
    script = Path("scripts/evaluate-release.sh").read_text(encoding="utf-8")

    assert "evaluate-release-evidence" in script
    assert "evaluation_observations_missing" in script
    assert "retrieval_evaluation_driver_required" not in script


def test_verification_script_derives_evidence_before_signing() -> None:
    """Catches signing a caller-authored all-green evidence file."""
    script = Path("scripts/verify-release.sh").read_text(encoding="utf-8")

    assert "assemble-release-evidence" in script
    assert script.index("assemble-release-evidence") < script.index(
        "create-verification-attestation"
    )
    assert "release_evidence_exists" in script


def test_index_script_builds_dense_candidate_and_index_attestation() -> None:
    script = Path("scripts/build-indexes.sh").read_text(encoding="utf-8")

    assert "build-dense-index" in script
    assert "index-attestation.json" in script
    assert "dense_index_driver_required" not in script


def test_corpus_script_stages_review_registry_before_stopping() -> None:
    script = Path("scripts/build-corpus.sh").read_text(encoding="utf-8")

    assert "stage-review-corpus" in script
    assert "--input-root /sen-qa/artifacts/raw-pages" in script
    assert "stage=review_pending failed=0" in script
    assert "candidate_review_bridge_required" not in script
