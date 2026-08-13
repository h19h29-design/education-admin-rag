import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

PLAN_PATH = Path(
    "docs/superpowers/plans/2026-08-08-education-admin-corpus-rag-foundation.md"
)


def _task_section(task_number: int) -> str:
    plan = PLAN_PATH.read_text(encoding="utf-8")
    section = plan.split(f"## Task {task_number}:", maxsplit=1)[1]
    next_task = f"## Task {task_number + 1}:"
    return section.split(next_task, maxsplit=1)[0]


def test_task6_runtime_import_gate_rejects_wrong_paddle_version(
    tmp_path: Path,
) -> None:
    """Catches a smoke gate that imports Paddle without pin verification."""
    command = next(
        line
        for line in _task_section(6).splitlines()
        if line.startswith("docker run ") and "runtime-imports=ok" in line
    )
    arguments = shlex.split(command)
    python_payload = arguments[arguments.index("-c") + 1]
    module_names = (
        "cv2",
        "paddle",
        "paddleocr",
        "paddlex",
        "pymupdf",
        "pydantic",
        "typer",
    )
    for name in module_names:
        (tmp_path / f"{name}.py").write_text("", encoding="utf-8")
    paddle_module = tmp_path / "paddle.py"
    environment = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tmp_path),
    }

    paddle_module.write_text('__version__ = "3.1.1"\n', encoding="utf-8")
    supported = subprocess.run(
        [sys.executable, "-c", python_payload],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert supported.returncode == 0, supported.stderr

    for unsupported_version in ("3.1.0", "3.3.1"):
        paddle_module.write_text(
            f'__version__ = "{unsupported_version}"\n', encoding="utf-8"
        )
        unsupported = subprocess.run(
            [sys.executable, "-c", python_payload],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert unsupported.returncode != 0
        assert "AssertionError" in unsupported.stderr


def test_task11_host_commands_select_locked_index_dependencies() -> None:
    """Catches clean-host dense/Qdrant commands that omit the index extra."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    root_package = next(
        package
        for package in lock["package"]
        if package["name"] == "education-admin-rag"
    )
    expected = ["qdrant-client", "sentence-transformers"]

    assert project["project"]["optional-dependencies"]["index"] == expected
    assert [
        dependency["name"]
        for dependency in root_package["optional-dependencies"]["index"]
    ] == expected

    host_commands = [
        shlex.split(line)
        for line in _task_section(11).splitlines()
        if line.startswith("uv run ")
    ]
    assert host_commands
    assert all(
        command[:4] == ["uv", "run", "--extra", "index"]
        for command in host_commands
    )
