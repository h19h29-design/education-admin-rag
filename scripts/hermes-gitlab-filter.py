from __future__ import annotations

import json
import sys
from typing import Any

MAX_INPUT_BYTES = 65_536
PROJECT_ID = 428
PROJECT_PATH = "h19h19/education-admin-rag"
ALLOWED_USERNAME = "h19h19"
GITLAB_PROJECT_URL = "https://gitlab.aigov.go.kr/h19h19/education-admin-rag"


def _ignore() -> None:
    sys.stdout.write("[SILENT]\n")


def _exact_dict(value: object) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    return value


def _exact_iid(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        return None
    return value


def _transform(payload: object) -> dict[str, object] | None:
    root = _exact_dict(payload)
    if root is None or root.get("object_kind") != "note":
        return None

    project = _exact_dict(root.get("project"))
    user = _exact_dict(root.get("user"))
    attributes = _exact_dict(root.get("object_attributes"))
    if project is None or user is None or attributes is None:
        return None
    if root.get("project_id") != PROJECT_ID:
        return None
    if project.get("id") != PROJECT_ID:
        return None
    if project.get("path_with_namespace") != PROJECT_PATH:
        return None
    if user.get("username") != ALLOWED_USERNAME:
        return None

    command_text = attributes.get("note")
    if command_text not in {"/hermes review", "/hermes status"}:
        return None
    command = command_text.removeprefix("/hermes ")

    noteable_type = attributes.get("noteable_type")
    if noteable_type == "MergeRequest":
        target = _exact_dict(root.get("merge_request"))
        target_kind = "merge_request"
        path = "merge_requests"
    elif noteable_type == "Issue":
        target = _exact_dict(root.get("issue"))
        target_kind = "issue"
        path = "issues"
    else:
        return None
    if command == "review" and target_kind != "merge_request":
        return None
    if target is None:
        return None
    iid = _exact_iid(target.get("iid"))
    if iid is None:
        return None

    return {
        "command": command,
        "project": PROJECT_PATH,
        "project_id": PROJECT_ID,
        "target_iid": iid,
        "target_kind": target_kind,
        "target_url": f"{GITLAB_PROJECT_URL}/-/{path}/{iid}",
    }


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        _ignore()
        return
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _ignore()
        return
    transformed = _transform(payload)
    if transformed is None:
        _ignore()
        return
    sys.stdout.write(
        json.dumps(
            transformed,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
