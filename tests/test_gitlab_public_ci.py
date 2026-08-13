from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".gitlab-ci.yml"
ENTRYPOINT = ROOT / "scripts" / "ci-public-gates.sh"
RUNBOOK = ROOT / "docs" / "runbooks" / "public-gitlab.md"
REPORT = ROOT / "docs" / "reports" / "public-gitlab-bootstrap.md"


def test_gitlab_pipeline_is_full_history_public_safe() -> None:
    text = CI.read_text(encoding="utf-8")

    assert 'GIT_DEPTH: "0"' in text
    assert 'AUTO_DEVOPS_DISABLED: "1"' in text
    assert "public-safe" in text
    assert "public-policy:" in text
    assert "quality:" in text
    assert "security:" in text
    assert "docs:" in text


def test_gitlab_pipeline_cannot_deploy_or_publish_artifacts() -> None:
    text = CI.read_text(encoding="utf-8")

    lowered = text.lower()
    assert "artifacts:" not in lowered
    assert "docker push" not in lowered
    assert "deploy" not in lowered
    assert "sen_qa_" not in lowered


def test_public_gate_entrypoint_is_valid_shell() -> None:
    subprocess.run(["bash", "-n", str(ENTRYPOINT)], check=True)


def test_public_gate_quality_isolates_and_tests_both_python_projects() -> None:
    text = ENTRYPOINT.read_text(encoding="utf-8")

    assert "uv run pytest -q tests" in text
    assert "uv run pytest -q\n" not in text
    assert "uv sync --project apps/travel-map --frozen --dev" in text
    assert (
        "uv run --project apps/travel-map pytest apps/travel-map/tests -q -W error"
        in text
    )


def test_public_gate_policy_mode_executes_repository_policy() -> None:
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "policy"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "public_repo_policy=pass" in result.stdout


def test_public_gate_rejects_unknown_mode_without_input_echo() -> None:
    marker = "PRIVATE_MODE_SENTINEL"
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), marker],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert combined == "public_ci_gate=invalid\n"
    assert marker not in combined


def test_public_gitlab_runbook_preserves_private_boundary() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    for phrase in (
        "Auto DevOps",
        "public-safe",
        "GIT_DEPTH",
        "push mirror",
        "NAS pull-only",
        "원본 PDF",
        "Runner 없음",
        "Rollback",
    ):
        assert phrase in text


def test_bootstrap_report_is_metadata_only() -> None:
    text = REPORT.read_text(encoding="utf-8")

    for key in (
        "project_created=",
        "project_visibility=",
        "runner_available=",
        "container_registry_enabled=",
        "private_data_uploaded=",
    ):
        assert key in text
    assert "/volume" not in text
    assert "PRIVATE" not in text


def test_bootstrap_report_matches_verified_public_state() -> None:
    text = REPORT.read_text(encoding="utf-8")

    for fact in (
        "project_created=1",
        "project_visibility=public",
        "auto_devops_enabled=0",
        "runner_available=0",
        "container_registry_enabled=0",
        "github_mirror_target=h19h29-design/education-admin-rag",
        "github_visibility=public",
        "default_branch=main",
        "gitlab_main_pushed=1",
        "main_protected=1",
        "force_push_allowed=0",
        "github_push_mirror_enabled=1",
        "pipeline_status=pending_runner",
        "project_runner_count=0",
        "group_runner_count=0",
        "instance_runner_count=0",
        "required_runner_tag=public-safe",
        "github_main_matches_gitlab=1",
        "github_hosted_ci_enabled=1",
        "github_actions_run_status=success",
        "github_actions_verified_sha=643a7f78d8423c6b875117d7cca939cd704c7fd7",
        "github_actions_private_data_uploaded=0",
        "public_test_compute_offloaded=1",
        "docker_sbom_status=blocked_no_verified_runner",
        "nas_compute_offload_started=0",
        "mirror_credentials_committed=0",
        "gitlab_project_id=428",
        "milestone_created=1",
        "work_items_created=4",
    ):
        assert fact in text
