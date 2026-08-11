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
from src.integrations.gitlab_qa_public import PublicAnswer, PublicCase

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


def _answer() -> PublicAnswer:
    return PublicAnswer(
        answer="근거 답변입니다. [2022년 · PDF 4쪽]",
        answer_kind="grounded",
        cases=(
            PublicCase(
                case_id="senqa-2022-case-a",
                edition_year=2022,
                pdf_pages=(4,),
                title="계약 사례",
                question="수의계약이 가능한가요?",
                answer="관련 기준을 확인합니다.",
            ),
        ),
    )


def test_comment_has_v2_machine_marker_and_only_public_payload() -> None:
    comment = build_answer_comment(REQUEST_ID, _answer())

    marker, payload_text = comment.split("\n", 1)
    assert marker == f"<!-- senqa-answer:v2 request_id={REQUEST_ID} -->"
    payload = json.loads(payload_text)
    assert payload["answer_kind"] == "grounded"
    assert payload["cases"][0]["pdf_pages"] == [4]
    for forbidden in (
        "production_eligible",
        "warning_code",
        "complete_corpus",
        "review_status",
        "Hermes",
    ):
        assert forbidden not in comment


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
        answer=_answer(),
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
        f"<!-- senqa-answer:v2 request_id={REQUEST_ID} -->"
    )


@pytest.mark.parametrize(
    ("issue_iid", "request_id", "answer", "token"),
    (
        (True, REQUEST_ID, _answer(), "token"),
        (0, REQUEST_ID, _answer(), "token"),
        (1, "bad", _answer(), "token"),
        (1, REQUEST_ID, "not-an-answer", "token"),
        (1, REQUEST_ID, _answer(), ""),
    ),
)
def test_invalid_delivery_fields_fail_value_free(
    issue_iid: object,
    request_id: object,
    answer: object,
    token: object,
) -> None:
    marker = "PRIVATE_DELIVERY_SENTINEL"
    with pytest.raises(DeliveryError) as caught:
        post_answer_comment(
            issue_iid=issue_iid,
            request_id=request_id,
            answer=answer,
            token=token,
        )

    rendered = repr(caught.value) + str(caught.value)
    assert rendered == "DeliveryError('delivery_invalid')delivery_invalid"
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
