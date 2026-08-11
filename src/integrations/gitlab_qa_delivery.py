"""Strict GitLab issue-note delivery for confidential SEN-QA answers."""

from __future__ import annotations

import json
import re
from typing import NoReturn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_PROJECT_ID = 428
_API_ROOT = "https://gitlab.aigov.go.kr/api/v4"
_REQUEST_RE = re.compile(r"^senqa-[0-9a-f]{32}$")
_MAX_CONTENT_CHARACTERS = 32_000
_MAX_RESPONSE_BYTES = 64 * 1024


class DeliveryError(ValueError):
    pass


def _raise() -> NoReturn:
    raise DeliveryError("delivery_invalid") from None


def build_answer_comment(request_id: object, content: object) -> str:
    if (
        type(request_id) is not str
        or _REQUEST_RE.fullmatch(request_id) is None
        or type(content) is not str
        or not content.strip()
        or len(content) > _MAX_CONTENT_CHARACTERS
    ):
        _raise()
    return (
        f"<!-- senqa-answer:v1 request_id={request_id} -->\n"
        "> **미검수 프리뷰** · `production_eligible=false` · "
        "운영 판단으로 사용하지 마세요.\n\n"
        f"{content.strip()}"
    )


def post_answer_comment(
    *, issue_iid: object, request_id: object, content: object, token: object
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
    comment = build_answer_comment(request_id, content)
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
