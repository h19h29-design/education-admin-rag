from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.release import ReleaseError, load_storage_policy


def _policy() -> str:
    return """
schema_version = "sen-qa-storage-policy/v1"
ingestion_uid = 21001
search_uid = 21002
evaluator_uid = 21003
reviewer_gid = 22001
source_root = "/volume1/education-admin/source"
artifact_root = "/volume1/education-admin/artifacts"
private_eval_root = "/volume1/education-admin/private-eval"
""".strip()


def test_service_identities_are_distinct_non_root(tmp_path: Path) -> None:
    """Catches root or shared service identities defeating least privilege."""
    target = tmp_path / "storage-policy.toml"
    target.write_text(_policy(), encoding="utf-8")

    policy = load_storage_policy(target)

    assert policy.ingestion_uid > 0
    assert policy.search_uid > 0
    assert policy.evaluator_uid > 0
    assert policy.reviewer_gid > 0
    assert len({policy.ingestion_uid, policy.search_uid, policy.evaluator_uid}) == 3


@pytest.mark.parametrize(
    "replacement",
    [
        "ingestion_uid = 0",
        "search_uid = 21001",
        'artifact_root = "relative/artifacts"',
        'private_eval_root = "/volume1/education-admin/artifacts"',
        'private_eval_root = "/volume1/education-admin/artifacts/private"',
        'source_root = "/"',
        'source_root = "/volume1/education-admin/source\\nforged"',
    ],
)
def test_storage_policy_rejects_unsafe_identity_and_root_aliases(
    tmp_path: Path, replacement: str
) -> None:
    """Catches identities or roots that collapse an isolation boundary."""
    original = _policy()
    key = replacement.split(" = ", maxsplit=1)[0]
    lines = [
        replacement if line.startswith(f"{key} = ") else line
        for line in original.splitlines()
    ]
    target = tmp_path / "storage-policy.toml"
    target.write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(ReleaseError, match="storage_policy_invalid") as captured:
        load_storage_policy(target)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_storage_policy_rejects_symlink_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    """Catches policy path redirection or a named pipe stalling release startup."""
    target = tmp_path / "storage-policy.toml"
    target.write_text(_policy(), encoding="utf-8")
    link = tmp_path / "policy-link.toml"
    link.symlink_to(target)
    with pytest.raises(ReleaseError, match="storage_policy_invalid"):
        load_storage_policy(link)

    fifo = tmp_path / "policy.fifo"
    os.mkfifo(fifo)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from src.release import ReleaseError,load_storage_policy; "
                f"p=Path({str(fifo)!r}); "
                "\ntry: load_storage_policy(p)"
                "\nexcept ReleaseError: raise SystemExit(0)"
                "\nraise SystemExit(1)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
