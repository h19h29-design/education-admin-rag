from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FILTER = ROOT / "scripts" / "hermes-gitlab-qa-filter.py"
REQUEST_ID = "senqa-0123456789abcdef0123456789abcdef"


def _payload(
    *,
    note: str = f"/hermes ask {REQUEST_ID}\n수의계약이 가능한 경우를 알려줘",
    username: str = "senqa-worker-bot",
    confidential: object = True,
    iid: object = 73,
    project_id: object = 428,
    project_path: object = "h19h19/education-admin-rag",
) -> dict[str, Any]:
    return {
        "object_kind": "note",
        "project_id": project_id,
        "project": {"id": project_id, "path_with_namespace": project_path},
        "user": {"username": username},
        "object_attributes": {"note": note, "noteable_type": "Issue"},
        "issue": {"iid": iid, "confidential": confidential},
    }


def _search_fixture(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    search = tmp_path / "search"
    search.write_text(
        "#!/bin/sh\n"
        + (
            "exit 7\n"
            if exit_code
            else 'printf \'%s\\n\' \'{"complete_corpus":false,"production_eligible":false,"query":"fixture","results":[{"case_id":"senqa-2022-case-a","edition_year":2022,"pdf_pages":[4],"title":"fixture"}],"schema_version":"sen-qa-preview-search-response/v1","warning_code":"unreviewed_incomplete_preview"}\'\n'
        )
    )
    search.chmod(0o500)
    config = tmp_path / "config.json"
    config.write_text("{}\n")
    config.chmod(0o600)
    return search, config


def _run(
    tmp_path: Path, payload: object, *, exit_code: int = 0
) -> subprocess.CompletedProcess[bytes]:
    search, config = _search_fixture(tmp_path, exit_code=exit_code)
    environment = os.environ.copy()
    environment.update(
        {
            "SENQA_GITLAB_BOT_USERNAME": "senqa-worker-bot",
            "SENQA_PREVIEW_SEARCH_COMMAND": str(search),
            "SENQA_PREVIEW_SEARCH_CONFIG": str(config),
        }
    )
    return subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=False,
        env=environment,
        timeout=5,
    )


def test_exact_confidential_ask_returns_bounded_grounded_payload(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _payload())

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "command": "ask",
        "evidence": {
            "complete_corpus": False,
            "production_eligible": False,
            "results": [
                {
                    "case_id": "senqa-2022-case-a",
                    "edition_year": 2022,
                    "pdf_pages": [4],
                    "title": "fixture",
                }
            ],
            "schema_version": "sen-qa-preview-search-response/v1",
            "warning_code": "unreviewed_incomplete_preview",
        },
        "project": "h19h19/education-admin-rag",
        "project_id": 428,
        "question": "수의계약이 가능한 경우를 알려줘",
        "request_id": REQUEST_ID,
        "target_iid": 73,
    }


def test_rejects_public_issue_wrong_actor_project_and_command_without_echo(
    tmp_path: Path,
) -> None:
    marker = "PRIVATE_QUESTION_SENTINEL"
    rejected = (
        _payload(confidential=False),
        _payload(username="someone"),
        _payload(project_id=429),
        _payload(project_path="someone/other"),
        _payload(note=f"/hermes ask {REQUEST_ID} {marker}"),
        _payload(note=f"please /hermes ask {REQUEST_ID}\n{marker}"),
        _payload(note=f"/hermes ask senqa-not-a-token\n{marker}"),
        _payload(iid=True),
    )

    for index, payload in enumerate(rejected):
        result = _run(tmp_path / str(index), payload)
        assert result.returncode == 0
        assert result.stdout == b"[SILENT]\n"
        assert result.stderr == b""
        assert marker.encode() not in result.stdout + result.stderr


def test_question_bounds_and_control_characters_fail_closed(tmp_path: Path) -> None:
    rejected = (
        _payload(note=f"/hermes ask {REQUEST_ID}\n"),
        _payload(note=f"/hermes ask {REQUEST_ID}\n" + "A" * 1_001),
        _payload(note=f"/hermes ask {REQUEST_ID}\nhello\x00world"),
    )
    for index, payload in enumerate(rejected):
        result = _run(tmp_path / str(index), payload)
        assert result.stdout == b"[SILENT]\n"
        assert result.stderr == b""


def test_search_failure_is_silent_and_does_not_echo_question(tmp_path: Path) -> None:
    marker = "PRIVATE_SEARCH_FAILURE_SENTINEL"
    result = _run(
        tmp_path,
        _payload(note=f"/hermes ask {REQUEST_ID}\n{marker}"),
        exit_code=7,
    )

    assert result.returncode == 0
    assert result.stdout == b"[SILENT]\n"
    assert result.stderr == b""
    assert marker.encode() not in result.stdout + result.stderr


def test_search_command_is_invoked_without_shell_interpretation(tmp_path: Path) -> None:
    search, config = _search_fixture(tmp_path)
    sentinel = tmp_path / "must-not-exist"
    environment = os.environ.copy()
    environment.update(
        {
            "SENQA_GITLAB_BOT_USERNAME": "senqa-worker-bot",
            "SENQA_PREVIEW_SEARCH_COMMAND": str(search),
            "SENQA_PREVIEW_SEARCH_CONFIG": str(config),
        }
    )
    payload = _payload(note=f"/hermes ask {REQUEST_ID}\nhello; touch {sentinel}")

    result = subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=False,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 0
    assert not sentinel.exists()
    assert json.loads(result.stdout)["question"].endswith(str(sentinel))


def test_search_and_config_must_be_owner_controlled_regular_files(
    tmp_path: Path,
) -> None:
    search, config = _search_fixture(tmp_path)
    search.chmod(stat.S_IRWXU | stat.S_IWGRP)
    environment = os.environ.copy()
    environment.update(
        {
            "SENQA_GITLAB_BOT_USERNAME": "senqa-worker-bot",
            "SENQA_PREVIEW_SEARCH_COMMAND": str(search),
            "SENQA_PREVIEW_SEARCH_CONFIG": str(config),
        }
    )

    result = subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=json.dumps(_payload()).encode(),
        capture_output=True,
        check=False,
        env=environment,
        timeout=5,
    )

    assert result.stdout == b"[SILENT]\n"
    assert result.stderr == b""
