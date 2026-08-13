import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
EXPECTED_FINDING = {
    "fingerprint": "baseline-fingerprint",
    "commit": "baseline-commit",
    "path": "historical.txt",
    "rule_id": "test-rule",
}


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o700)


def _make_gate_repo(tmp_path: Path, history_report: str, require_ignore_flag: bool) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o700)
    (repo / "config").mkdir()
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/scan-secrets.sh", repo / "scripts/scan-secrets.sh")
    shutil.copy(ROOT / "config/gitleaks.toml", repo / "config/gitleaks.toml")
    (repo / "config/revoked-secrets-baseline.json").write_text(
        """{
  "schema_version": 1,
  "known_historical_findings": [
    {
      "fingerprint": "baseline-fingerprint",
      "commit": "baseline-commit",
      "path": "historical.txt",
      "rule_id": "test-rule"
    }
  ]
}
""",
        encoding="utf-8",
    )
    (repo / "historical.txt").write_text("safe fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "security-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Security Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "test fixture"], cwd=repo, check=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scan_log = tmp_path / "scan.log"
    history_path = tmp_path / "history.json"
    history_path.write_text(history_report, encoding="utf-8")
    fake_scanner = tmp_path / "gitleaks"
    _write_executable(
        fake_scanner,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_SCAN_LOG"
if [[ "${REQUIRE_IGNORE_FLAG:-0}" == "1" && " $* " != *" --ignore-gitleaks-allow "* ]]; then
  exit 2
fi
report=""
for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "--report-path" ]]; then
    next=$((index + 1))
    report="${!next}"
  fi
done
if [[ "$1" == "dir" ]]; then
  printf '[]' > "$report"
else
  cp "$FAKE_HISTORY_REPORT" "$report"
fi
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then
    : > "$2"
    exit 0
  fi
  shift
done
exit 2
""",
    )
    _write_executable(
        fake_bin / "shasum",
        """#!/usr/bin/env bash
printf 'b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5  fixture\\n'
""",
    )
    _write_executable(
        fake_bin / "uname",
        """#!/usr/bin/env bash
if [[ "$1" == "-s" ]]; then
  printf 'Darwin\\n'
else
  printf 'arm64\\n'
fi
""",
    )
    _write_executable(
        fake_bin / "tar",
        """#!/usr/bin/env bash
set -euo pipefail
destination=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-C" ]]; then
    destination="$2"
    break
  fi
  shift
done
cp "$FAKE_GITLEAKS" "$destination/gitleaks"
""",
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GITLEAKS": str(fake_scanner),
        "FAKE_SCAN_LOG": str(scan_log),
        "FAKE_HISTORY_REPORT": str(history_path),
        "REQUIRE_IGNORE_FLAG": "1" if require_ignore_flag else "0",
        "TMPDIR": str(tmp_path),
    }
    return repo, environment, scan_log


def _run_gate(repo: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/scan-secrets.sh"],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_ignores_inline_allow_directives_for_both_scans(tmp_path: Path) -> None:
    repo, environment, scan_log = _make_gate_repo(
        tmp_path,
        '[{"Fingerprint":"baseline-fingerprint","Commit":"baseline-commit","File":"historical.txt","RuleID":"test-rule"}]',
        require_ignore_flag=True,
    )

    result = _run_gate(repo, environment)

    assert result.returncode == 0, result.stderr
    invocations = scan_log.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 2
    assert all("--ignore-gitleaks-allow" in invocation for invocation in invocations)


def test_gate_rejects_untracked_gitleaksignore_anywhere_in_repo(tmp_path: Path) -> None:
    repo, environment, scan_log = _make_gate_repo(
        tmp_path,
        '[{"Fingerprint":"baseline-fingerprint","Commit":"baseline-commit","File":"historical.txt","RuleID":"test-rule"}]',
        require_ignore_flag=False,
    )
    ignored = repo / "nested/.gitleaksignore"
    ignored.parent.mkdir()
    ignored.write_text("fixture-only\n", encoding="utf-8")

    result = _run_gate(repo, environment)

    assert result.returncode == 2
    assert ".gitleaksignore" in result.stderr
    assert not scan_log.exists()


def test_gate_preserves_empty_commit_metadata_columns(tmp_path: Path) -> None:
    repo, environment, _ = _make_gate_repo(
        tmp_path,
        '[{"Fingerprint":"current-fingerprint","Commit":"","File":"fixture.txt","RuleID":"test-rule"}]',
        require_ignore_flag=False,
    )

    result = _run_gate(repo, environment)

    assert result.returncode == 1
    assert "finding fingerprint=current-fingerprint commit= path=fixture.txt rule=test-rule" in result.stderr
