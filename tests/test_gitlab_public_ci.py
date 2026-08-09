from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".gitlab-ci.yml"
ENTRYPOINT = ROOT / "scripts" / "ci-public-gates.sh"


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
