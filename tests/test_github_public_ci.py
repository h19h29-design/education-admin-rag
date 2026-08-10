from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "public-safe.yml"
IMAGE_WORKFLOW = ROOT / ".github" / "workflows" / "public-images.yml"


def _workflow() -> dict[str, object]:
    assert WORKFLOW.is_file(), "public-safe GitHub workflow is missing"
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_github_workflow_uses_read_only_full_history_public_gates() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}

    job = workflow["jobs"]["public-safe"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", steps[0]["uses"])
    assert steps[0]["with"] == {"fetch-depth": "0"}
    assert re.fullmatch(r"actions/setup-python@[0-9a-f]{40}", steps[1]["uses"])
    assert steps[1]["with"] == {"python-version": "3.11"}
    assert "--require-hashes" in steps[2]["run"]
    assert [step["run"] for step in steps[3:]] == [
        "./scripts/ci-public-gates.sh policy",
        "./scripts/ci-public-gates.sh quality",
        "./scripts/ci-public-gates.sh security",
        "./scripts/ci-public-gates.sh docs",
    ]


def test_github_workflow_has_no_private_or_publishing_surface() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["public-safe"]
    serialized = repr(workflow).lower()

    assert "cache" not in job
    assert all(
        not step.get("uses", "").startswith("actions/cache@") for step in job["steps"]
    )
    for forbidden in (
        "secrets.",
        "artifacts",
        "docker push",
        "sen_qa_",
        "nas",
        "deploy",
    ):
        assert forbidden not in serialized


def test_public_ci_declares_its_test_runtime_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = set(project["dependency-groups"]["dev"])

    assert {"pyyaml", "qdrant-client"} <= dev_dependencies


def test_public_image_build_is_manual_digest_pinned_and_source_only() -> None:
    assert IMAGE_WORKFLOW.is_file(), "public image build workflow is missing"
    workflow = yaml.load(
        IMAGE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )

    assert set(workflow["on"]) == {"workflow_dispatch"}
    image_input = workflow["on"]["workflow_dispatch"]["inputs"]["image"]
    assert image_input["type"] == "choice"
    assert image_input["options"] == ["all", "ingestion", "indexer"]
    assert workflow["permissions"] == {"contents": "read", "packages": "write"}
    assert set(workflow["jobs"]) == {"ingestion", "indexer"}

    expected = {
        "ingestion": "docker/ingestion.Dockerfile",
        "indexer": "docker/indexer.Dockerfile",
    }
    for job_name, dockerfile in expected.items():
        job = workflow["jobs"][job_name]
        assert f"github.event.inputs.image == '{job_name}'" in job["if"]
        assert job["runs-on"] == "ubuntu-24.04"
        commands = "\n".join(
            step.get("run", "") for step in job["steps"] if "run" in step
        )
        assert "${{ secrets." not in commands
        assert "${{ github.token }}" in repr(job)
        assert f"-f {dockerfile}" in commands
        assert "--platform linux/amd64" in commands
        assert "--provenance=mode=max" in commands
        assert "--sbom=true" in commands
        assert "--push" in commands
        assert ":${GITHUB_SHA}" in commands
        assert ":latest" not in commands
        assert "artifacts" not in commands.lower()
        assert "SEN_QA_" not in commands
