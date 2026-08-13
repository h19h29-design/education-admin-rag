from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FILTER = ROOT / "scripts" / "hermes-gitlab-filter.py"


def _note_payload(
    *,
    note: str = "/hermes review",
    username: str = "h19h19",
    project_id: object = 428,
    project_path: object = "h19h19/education-admin-rag",
    noteable_type: object = "MergeRequest",
    iid: object = 17,
) -> dict[str, Any]:
    return {
        "object_kind": "note",
        "project_id": project_id,
        "project": {
            "id": project_id,
            "path_with_namespace": project_path,
        },
        "user": {"username": username},
        "object_attributes": {
            "note": note,
            "noteable_type": noteable_type,
        },
        "merge_request": {"iid": iid},
        "issue": {"iid": iid},
    }


def _run(payload: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=False,
    )


def test_review_command_emits_only_fixed_merge_request_metadata() -> None:
    result = _run(
        _note_payload(
            note="/hermes review",
            iid=17,
        )
    )

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "command": "review",
        "project": "h19h19/education-admin-rag",
        "project_id": 428,
        "target_iid": 17,
        "target_kind": "merge_request",
        "target_url": (
            "https://gitlab.aigov.go.kr/h19h19/education-admin-rag/-/merge_requests/17"
        ),
    }


def test_status_command_accepts_an_issue_without_forwarding_issue_text() -> None:
    marker = "PRIVATE_ISSUE_SENTINEL"
    payload = _note_payload(
        note="/hermes status",
        noteable_type="Issue",
        iid=23,
    )
    payload["issue"]["description"] = marker

    result = _run(payload)

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "command": "status",
        "project": "h19h19/education-admin-rag",
        "project_id": 428,
        "target_iid": 23,
        "target_kind": "issue",
        "target_url": (
            "https://gitlab.aigov.go.kr/h19h19/education-admin-rag/-/issues/23"
        ),
    }
    assert marker.encode() not in result.stdout + result.stderr


def test_unapproved_project_user_or_command_is_ignored_without_echo() -> None:
    marker = "PRIVATE_COMMAND_SENTINEL"
    rejected = (
        _note_payload(project_id=429),
        _note_payload(project_path="someone/other"),
        _note_payload(username="someone"),
        _note_payload(note=f"/hermes review {marker}"),
        _note_payload(note="please run /hermes review"),
        _note_payload(noteable_type="Commit"),
        _note_payload(iid=True),
        _note_payload(iid=0),
        _note_payload(iid=1_000_001),
    )

    for payload in rejected:
        result = _run(payload)
        assert result.returncode == 0
        assert result.stdout == b"[SILENT]\n"
        assert result.stderr == b""
        assert marker.encode() not in result.stdout + result.stderr


def test_malformed_or_oversized_input_is_ignored_without_echo() -> None:
    malformed = subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=b"PRIVATE_JSON_SENTINEL",
        capture_output=True,
        check=False,
    )
    oversized = subprocess.run(
        [sys.executable, str(FILTER)],
        cwd=ROOT,
        input=b"{" + b'"x":"' + (b"A" * 65_537) + b'"}',
        capture_output=True,
        check=False,
    )

    for result in (malformed, oversized):
        assert result.returncode == 0
        assert result.stdout == b"[SILENT]\n"
        assert result.stderr == b""
        assert b"PRIVATE_JSON_SENTINEL" not in result.stdout + result.stderr
