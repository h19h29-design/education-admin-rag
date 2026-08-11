"""Strict public-safe response contract for the education administration QA UI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, NoReturn

PUBLIC_SCHEMA: Literal["senqa-public-answer/v1"] = "senqa-public-answer/v1"
NO_EVIDENCE_TEXT = (
    "등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. "
    "다른 표현이나 핵심어로 다시 검색해 주세요."
)
CASES_ONLY_TEXT = (
    "답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요."
)

_CASE_ID_RE = re.compile(r"^senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}$")
_CASE_ID_SEARCH_RE = re.compile(r"senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}")
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_STOPWORDS = frozenset(
    {
        "관련",
        "경우",
        "대한",
        "무엇",
        "어떻게",
        "알려줘",
        "알려주세요",
        "있는",
        "하는",
    }
)
_FORBIDDEN_PUBLIC_TERMS = (
    "gitlab",
    "webhook",
    "hermes",
    "rag",
    "production_eligible",
    "warning_code",
    "complete_corpus",
    "review_status",
)
_MAX_CASES = 5
_MAX_ANSWER_CHARACTERS = 32_000
_MAX_TITLE_CHARACTERS = 2_000
_MAX_CASE_TEXT_CHARACTERS = 24_000


class PublicAnswerError(ValueError):
    """A value-free public answer contract failure."""


def _raise() -> NoReturn:
    raise PublicAnswerError("public_answer_invalid") from None


def _plain_text(value: object, *, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= maximum
        and not any(
            (ord(character) < 32 or ord(character) == 127) and character not in "\n\r\t"
            for character in value
        )
    )


@dataclass(frozen=True, slots=True)
class PublicCase:
    case_id: str
    edition_year: int
    pdf_pages: tuple[int, ...]
    title: str
    question: str
    answer: str

    def __post_init__(self) -> None:
        if (
            type(self.case_id) is not str
            or _CASE_ID_RE.fullmatch(self.case_id) is None
            or type(self.edition_year) is not int
            or self.edition_year not in range(2020, 2026)
            or type(self.pdf_pages) is not tuple
            or not self.pdf_pages
            or len(self.pdf_pages) > 100
            or any(
                type(page) is not int or not 1 <= page <= 10_000
                for page in self.pdf_pages
            )
            or self.pdf_pages != tuple(sorted(set(self.pdf_pages)))
            or not _plain_text(self.title, maximum=_MAX_TITLE_CHARACTERS)
            or not _plain_text(self.question, maximum=_MAX_CASE_TEXT_CHARACTERS)
            or not _plain_text(self.answer, maximum=_MAX_CASE_TEXT_CHARACTERS)
        ):
            _raise()

    def as_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "case_id": self.case_id,
            "edition_year": self.edition_year,
            "pdf_pages": list(self.pdf_pages),
            "question": self.question,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class PublicAnswer:
    answer: str
    answer_kind: Literal["grounded", "no_evidence", "cases_only"]
    cases: tuple[PublicCase, ...]
    schema_version: Literal["senqa-public-answer/v1"] = PUBLIC_SCHEMA

    def __post_init__(self) -> None:
        if (
            not _plain_text(self.answer, maximum=_MAX_ANSWER_CHARACTERS)
            or self.answer_kind not in {"grounded", "no_evidence", "cases_only"}
            or type(self.cases) is not tuple
            or len(self.cases) > _MAX_CASES
            or any(type(case) is not PublicCase for case in self.cases)
            or len({case.case_id for case in self.cases}) != len(self.cases)
            or self.schema_version != PUBLIC_SCHEMA
            or type(self.schema_version) is not str
        ):
            _raise()
        if self.answer_kind == "no_evidence" and (
            self.answer != NO_EVIDENCE_TEXT or self.cases
        ):
            _raise()
        if self.answer_kind == "cases_only" and (
            self.answer != CASES_ONLY_TEXT or not self.cases
        ):
            _raise()
        if self.answer_kind == "grounded" and (
            not self.cases or validate_grounded_answer(self.answer, self.cases) is None
        ):
            _raise()


def _meaningful_tokens(question: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token
            for token in _TOKEN_RE.findall(question.casefold())
            if len(token) >= 2 and token not in _STOPWORDS
        )
    )


def _public_case_from_result(value: object) -> PublicCase | None:
    if type(value) is not dict:
        return None
    case_id = value.get("case_id")
    edition_year = value.get("edition_year")
    pdf_pages = value.get("pdf_pages")
    title = value.get("title")
    question = value.get("question")
    answer = value.get("answer")
    if (
        type(case_id) is not str
        or type(edition_year) is not int
        or type(pdf_pages) is not list
        or type(title) is not str
        or type(question) is not str
        or type(answer) is not str
    ):
        return None
    try:
        return PublicCase(
            case_id=case_id,
            edition_year=edition_year,
            pdf_pages=tuple(pdf_pages),
            title=title,
            question=question,
            answer=answer,
        )
    except (PublicAnswerError, TypeError):
        return None


def public_cases_from_evidence(
    question: object, evidence: object
) -> tuple[PublicCase, ...]:
    if type(question) is not str or not question.strip() or type(evidence) is not dict:
        _raise()
    results = evidence.get("results")
    if type(results) is not list or len(results) > _MAX_CASES:
        _raise()
    tokens = _meaningful_tokens(question)
    selected: list[PublicCase] = []
    for result in results:
        case = _public_case_from_result(result)
        if case is None:
            _raise()
        searchable = f"{case.title}\n{case.question}\n{case.answer}".casefold()
        heading = f"{case.title}\n{case.question}".casefold()
        matched = {token for token in tokens if token in searchable}
        if len(matched) >= 2 or any(
            len(token) >= 4 and token in heading for token in tokens
        ):
            selected.append(case)
    return tuple(selected)


def validate_grounded_answer(content: object, cases: object) -> str | None:
    if (
        type(content) is not str
        or not content.strip()
        or len(content) > _MAX_ANSWER_CHARACTERS
        or type(cases) is not tuple
        or not cases
        or len(cases) > _MAX_CASES
        or any(type(case) is not PublicCase for case in cases)
    ):
        return None
    checked = content.strip()
    folded = checked.casefold()
    if any(term in folded for term in _FORBIDDEN_PUBLIC_TERMS):
        return None
    allowed_ids = {case.case_id for case in cases}
    if any(
        case_id not in allowed_ids for case_id in _CASE_ID_SEARCH_RE.findall(checked)
    ):
        return None
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n|\n", checked)
        if paragraph.strip()
    ]
    for paragraph in paragraphs:
        if not any(
            any(
                f"[{case.case_id} · {case.edition_year}년 · PDF {page}쪽]" in paragraph
                for page in case.pdf_pages
            )
            for case in cases
        ):
            return None
    return checked


def no_evidence_answer() -> PublicAnswer:
    return PublicAnswer(answer=NO_EVIDENCE_TEXT, answer_kind="no_evidence", cases=())


def cases_only_answer(cases: tuple[PublicCase, ...]) -> PublicAnswer:
    return PublicAnswer(answer=CASES_ONLY_TEXT, answer_kind="cases_only", cases=cases)


def canonical_public_answer_json(answer: object) -> str:
    if type(answer) is not PublicAnswer:
        _raise()
    checked = PublicAnswer(
        answer=answer.answer,
        answer_kind=answer.answer_kind,
        cases=tuple(
            PublicCase(
                case_id=case.case_id,
                edition_year=case.edition_year,
                pdf_pages=tuple(case.pdf_pages),
                title=case.title,
                question=case.question,
                answer=case.answer,
            )
            for case in answer.cases
        ),
        schema_version=answer.schema_version,
    )
    payload = {
        "answer": checked.answer,
        "answer_kind": checked.answer_kind,
        "cases": [case.as_dict() for case in checked.cases],
        "schema_version": checked.schema_version,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
