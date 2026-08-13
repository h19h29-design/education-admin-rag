from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "verify-public-repo.sh"


def _run(tmp_path: Path, tracked_path: str) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / tracked_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return subprocess.run(
        ["bash", str(POLICY)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_repository_policy_accepts_source(tmp_path: Path) -> None:
    result = _run(tmp_path, "src/example.py")

    assert result.returncode == 0
    assert "public_repo_policy=pass" in result.stdout


def test_public_repository_policy_rejects_pdf_without_echo(tmp_path: Path) -> None:
    result = _run(tmp_path, "source-name.pdf")

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "class=document" in combined
    assert "source-name" not in combined


def test_public_repository_policy_rejects_canonical_db_without_echo(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "artifacts/private-name.sqlite3")

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "class=private-artifact" in combined
    assert "private-name" not in combined


def test_public_repository_policy_rejects_private_key_without_echo(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "config/private-name.pem")

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "class=credential" in combined
    assert "private-name" not in combined
