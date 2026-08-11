"""Strict GitLab issue-note delivery for confidential SEN-QA answers."""

from __future__ import annotations

import json
import re
from typing import NoReturn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.integrations.gitlab_qa_public import (
    PublicAnswer,
    canonical_public_answer_json,
)

_PROJECT_ID = 428
_API_ROOT = "https://gitlab.aigov.go.kr/api/v4"
_REQUEST_RE = re.compile(r"^senqa-[0-9a-f]{32}$")
_MAX_COMMENT_BYTES = 192 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024


class DeliveryError(ValueError):
    pass


def _raise() -> NoReturn:
    raise DeliveryError("delivery_invalid") from None


def build_answer_comment(request_id: object, answer: object) -> str:
    if (
        type(request_id) is not str
        or _REQUEST_RE.fullmatch(request_id) is None
        or type(answer) is not PublicAnswer
    ):
        _raise()
    payload = canonical_public_answer_json(answer)
    comment = f"<!-- senqa-answer:v2 request_id={request_id} -->\n{payload}"
    if len(comment.encode("utf-8")) > _MAX_COMMENT_BYTES:
        _raise()
    return comment


def post_answer_comment(
    *, issue_iid: object, request_id: object, answer: object, token: object
) -> str:
    if (
        type(issue_iid) is not int
        or not 1 <= issue_iid <= 1_000_000
        or type(token) is not str
        or not token
        or len(token) > 512
        or any(character.isspace() for character in token)
    ):
        _raise()
    comment = build_answer_comment(request_id, answer)
    payload = json.dumps(
        {"body": comment}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    request = Request(
        f"{_API_ROOT}/projects/{_PROJECT_ID}/issues/{issue_iid}/notes",
        data=payload,
        headers={"Content-Type": "application/json", "PRIVATE-TOKEN": token},
        method="POST",
    )
    failed = False
    response_payload: object = None
    try:
        with urlopen(request, timeout=15.0) as response:
            if response.status != 201:
                failed = True
            else:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    failed = True
                else:
                    response_payload = json.loads(raw)
    except (OSError, HTTPError, URLError, UnicodeError, json.JSONDecodeError):
        failed = True
    if failed or type(response_payload) is not dict:
        _raise()
    note_id = response_payload.get("id")
    if type(note_id) is not int or note_id < 1:
        _raise()
    return str(note_id)
