#!/usr/bin/env python3
"""Transform one approved confidential GitLab QA note into grounded evidence."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 65_536
MAX_QUESTION_CHARACTERS = 1_000
MAX_SEARCH_OUTPUT_BYTES = 2 * 1024 * 1024
PROJECT_ID = 428
PROJECT_PATH = "h19h19/education-admin-rag"
REQUEST_RE = re.compile(r"^/hermes ask (senqa-[0-9a-f]{32})\n([\s\S]+)$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _ignore() -> None:
    sys.stdout.write("[SILENT]\n")


def _exact_dict(value: object) -> dict[str, Any] | None:
    return value if type(value) is dict else None


def _owner_regular(path: Path, *, executable: bool) -> bool:
    if not path.is_absolute():
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or mode & 0o077
    ):
        return False
    return bool(mode & stat.S_IXUSR) if executable else not bool(mode & 0o111)


def _question(value: str) -> str | None:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_QUESTION_CHARACTERS:
        return None
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        and not character.isspace()
        for character in value
    ):
        return None
    return normalized


def _search(question: str) -> dict[str, object] | None:
    command_value = os.environ.get("SENQA_PREVIEW_SEARCH_COMMAND", "")
    config_value = os.environ.get("SENQA_PREVIEW_SEARCH_CONFIG", "")
    command = Path(command_value)
    config = Path(config_value)
    if not _owner_regular(command, executable=True) or not _owner_regular(
        config, executable=False
    ):
        return None
    try:
        completed = subprocess.run(
            [
                str(command),
                "--config",
                str(config),
                "--json",
                "--limit",
                "20",
                "--",
                question,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if (
        completed.returncode != 0
        or completed.stderr
        or len(completed.stdout) > MAX_SEARCH_OUTPUT_BYTES
    ):
        return None
    parsed: object = None
    try:
        parsed = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if type(parsed) is not dict:
        return None
    if (
        set(parsed)
        != {
            "schema_version",
            "query",
            "warning_code",
            "production_eligible",
            "complete_corpus",
            "results",
        }
        or parsed.get("schema_version") != "sen-qa-preview-search-response/v1"
        or parsed.get("warning_code") != "unreviewed_incomplete_preview"
        or parsed.get("production_eligible") is not False
        or parsed.get("complete_corpus") is not False
        or type(parsed.get("query")) is not str
        or type(parsed.get("results")) is not list
        or len(parsed["results"]) > 20
    ):
        return None
    return {key: value for key, value in parsed.items() if key != "query"}


def _transform(payload: object) -> dict[str, object] | None:
    root = _exact_dict(payload)
    if root is None or root.get("object_kind") != "note":
        return None
    project = _exact_dict(root.get("project"))
    user = _exact_dict(root.get("user"))
    attributes = _exact_dict(root.get("object_attributes"))
    issue = _exact_dict(root.get("issue"))
    if project is None or user is None or attributes is None or issue is None:
        return None
    allowed_username = os.environ.get("SENQA_GITLAB_BOT_USERNAME", "")
    if USERNAME_RE.fullmatch(allowed_username) is None:
        return None
    if (
        root.get("project_id") != PROJECT_ID
        or project.get("id") != PROJECT_ID
        or project.get("path_with_namespace") != PROJECT_PATH
        or user.get("username") != allowed_username
        or attributes.get("noteable_type") != "Issue"
        or issue.get("confidential") is not True
    ):
        return None
    iid = issue.get("iid")
    note = attributes.get("note")
    if type(iid) is not int or not 1 <= iid <= 1_000_000 or type(note) is not str:
        return None
    match = REQUEST_RE.fullmatch(note)
    if match is None:
        return None
    checked_question = _question(match.group(2))
    if checked_question is None:
        return None
    evidence = _search(checked_question)
    if evidence is None:
        return None
    return {
        "command": "ask",
        "project": PROJECT_PATH,
        "project_id": PROJECT_ID,
        "target_iid": iid,
        "request_id": match.group(1),
        "question": checked_question,
        "evidence": evidence,
    }


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        _ignore()
        return
    payload: object = None
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        pass
    transformed = _transform(payload)
    if transformed is None:
        _ignore()
        return
    sys.stdout.write(
        json.dumps(
            transformed, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
