from __future__ import annotations

import json
from email.message import Message
from typing import Self

import pytest

from src.integrations.gitlab_qa_delivery import (
    DeliveryError,
    build_answer_comment,
    post_answer_comment,
)

REQUEST_ID = "senqa-0123456789abcdef0123456789abcdef"


class _Response:
    status = 201
    headers = Message()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return b'{"id":91}'


def test_comment_has_machine_marker_and_nonproduction_warning() -> None:
    comment = build_answer_comment(
        REQUEST_ID,
        "근거 답변\n\nsenqa-2022-case-a · 2022년 · PDF 4쪽",
    )

    assert comment.startswith(f"<!-- senqa-answer:v1 request_id={REQUEST_ID} -->\n")
    assert "미검수 프리뷰" in comment
    assert "production_eligible=false" in comment
    assert "senqa-2022-case-a" in comment


def test_post_uses_exact_gitlab_issue_notes_endpoint_and_private_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("src.integrations.gitlab_qa_delivery.urlopen", fake_urlopen)

    note_id = post_answer_comment(
        issue_iid=73,
        request_id=REQUEST_ID,
        content="근거 답변",
        token="secret-token",
    )

    request = captured["request"]
    assert note_id == "91"
    assert captured["timeout"] == 15.0
    assert request.full_url == (
        "https://gitlab.aigov.go.kr/api/v4/projects/428/issues/73/notes"
    )
    assert request.get_header("Private-token") == "secret-token"
    assert request.get_method() == "POST"
    payload = json.loads(request.data)
    assert payload["body"].startswith(
        f"<!-- senqa-answer:v1 request_id={REQUEST_ID} -->"
    )


@pytest.mark.parametrize(
    ("issue_iid", "request_id", "content", "token"),
    (
        (True, REQUEST_ID, "answer", "token"),
        (0, REQUEST_ID, "answer", "token"),
        (1, "bad", "answer", "token"),
        (1, REQUEST_ID, "", "token"),
        (1, REQUEST_ID, "answer", ""),
        (1, REQUEST_ID, "A" * 32_001, "token"),
    ),
)
def test_invalid_delivery_fields_fail_value_free(
    issue_iid: object,
    request_id: object,
    content: object,
    token: object,
) -> None:
    marker = "PRIVATE_DELIVERY_SENTINEL"
    with pytest.raises(DeliveryError) as caught:
        post_answer_comment(
            issue_iid=issue_iid,
            request_id=request_id,
            content=f"{content}{marker}" if content else content,
            token=token,
        )

    rendered = repr(caught.value) + str(caught.value)
    assert rendered == "DeliveryError('delivery_invalid')delivery_invalid"
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
