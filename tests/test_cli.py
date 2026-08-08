import subprocess
import sys

from typer.testing import CliRunner

from src.cli import app


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
