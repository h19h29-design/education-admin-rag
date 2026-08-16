from __future__ import annotations

import json

import pytest

from src.integrations.gitlab_qa_public import (
    CASES_ONLY_TEXT,
    NO_EVIDENCE_TEXT,
    PublicAnswer,
    PublicCase,
    canonical_public_answer_json,
    cases_only_answer,
    no_evidence_answer,
    public_cases_from_evidence,
    temporarily_unavailable_answer,
    validate_grounded_answer,
)


def _result(
    *,
    title: str = "학교 계약",
    question: str = "수의계약이 가능한 경우",
    answer: str = "계약 금액과 적용 기준을 확인합니다.",
) -> dict[str, object]:
    return {
        "answer": answer,
        "basis": "관련 지침",
        "candidate_sha256": "a" * 64,
        "case_id": "senqa-2022-case-a",
        "case_no": "1",
        "citations": [
            {
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "pdf_page_index": 4,
                "text_sha256": "b" * 64,
            }
        ],
        "doc_id": "sen-qa-2022",
        "domain": "계약",
        "edition_year": 2022,
        "facts": "사실관계",
        "part": "계약업무",
        "pdf_pages": [4],
        "question": question,
        "review_status": "needs_review",
        "subtopic": None,
        "title": title,
    }


def _evidence(result: dict[str, object]) -> dict[str, object]:
    return {
        "complete_corpus": False,
        "production_eligible": False,
        "results": [result],
        "schema_version": "sen-qa-preview-search-response/v1",
        "warning_code": "unreviewed_incomplete_preview",
    }


def _case() -> PublicCase:
    return PublicCase(
        case_id="senqa-2022-case-a",
        edition_year=2022,
        pdf_pages=(4,),
        title="학교 계약",
        question="수의계약이 가능한 경우",
        answer="계약 금액과 적용 기준을 확인합니다.",
    )


def test_no_relevant_case_returns_empty() -> None:
    evidence = _evidence(
        _result(title="급식 계약", question="수의계약 기준", answer="기준을 확인합니다")
    )
    assert public_cases_from_evidence("학교폭력 조치", evidence) == ()


def test_two_meaningful_terms_make_case_relevant() -> None:
    cases = public_cases_from_evidence("학교 수의계약 기준", _evidence(_result()))
    assert [case.case_id for case in cases] == ["senqa-2022-case-a"]


def test_twenty_relevant_cases_are_preserved_in_rank_order() -> None:
    results: list[dict[str, object]] = []
    for index in range(20):
        result = _result()
        result["case_id"] = f"senqa-2022-case-{index}"
        results.append(result)
    evidence = _evidence(results[0])
    evidence["results"] = results

    cases = public_cases_from_evidence("학교 수의계약 기준", evidence)

    assert len(cases) == 20
    assert [case.case_id for case in cases] == [
        f"senqa-2022-case-{index}" for index in range(20)
    ]


def test_long_specific_term_in_title_is_relevant() -> None:
    evidence = _evidence(
        _result(
            title="기간제교원 채용",
            question="절차 안내",
            answer="공고 후 채용합니다.",
        )
    )
    assert len(public_cases_from_evidence("기간제교원", evidence)) == 1


def test_long_specific_term_in_answer_is_relevant() -> None:
    evidence = _evidence(
        _result(
            title="계약 문의",
            question="절차 안내",
            answer="이 경우에는 수의계약 절차를 적용합니다.",
        )
    )

    assert len(public_cases_from_evidence("수의계약", evidence)) == 1


@pytest.mark.parametrize("query", ["계약", "물품", "복무", "휴가"])
def test_single_short_business_term_in_case_is_relevant(query: str) -> None:
    evidence = _evidence(
        _result(
            title="계약 물품 복무 휴가",
            question="업무 처리 기준",
            answer="관련 절차를 확인합니다.",
        )
    )

    assert len(public_cases_from_evidence(query, evidence)) == 1


def test_public_case_drops_internal_fields() -> None:
    case = public_cases_from_evidence("학교 수의계약", _evidence(_result()))[0]
    assert set(case.as_dict()) == {
        "answer",
        "case_id",
        "edition_year",
        "pdf_pages",
        "question",
        "title",
    }


@pytest.mark.parametrize(
    "leak",
    [
        "GitLab",
        "Webhook",
        "Hermes",
        "RAG",
        "production_eligible",
        "warning_code",
        "complete_corpus",
        "review_status",
    ],
)
def test_grounded_answer_rejects_internal_terms(leak: str) -> None:
    case = _case()
    content = (
        f"계약 기준입니다. [{case.case_id} · 2022년 · PDF 4쪽]\n"
        f"{leak} [{case.case_id} · 2022년 · PDF 4쪽]"
    )
    assert validate_grounded_answer(content, (case,)) is None


def test_grounded_answer_requires_allowed_case_year_and_page_per_paragraph() -> None:
    case = _case()
    assert validate_grounded_answer("계약 기준입니다.", (case,)) is None
    assert (
        validate_grounded_answer(
            "계약 기준입니다. [2022년 · PDF 999쪽]",
            (case,),
        )
        is None
    )
    assert (
        validate_grounded_answer(
            "계약 기준입니다. [senqa-2022-case-a · 2022년 · PDF 4쪽]",
            (case,),
        )
        is None
    )
    valid = "계약 기준입니다. [2022년 · PDF 4쪽]"
    assert validate_grounded_answer(valid, (case,)) == valid


def test_fixed_fallback_answers_are_exact() -> None:
    assert no_evidence_answer() == PublicAnswer(
        answer=NO_EVIDENCE_TEXT,
        answer_kind="no_evidence",
        cases=(),
    )
    assert cases_only_answer((_case(),)) == PublicAnswer(
        answer=CASES_ONLY_TEXT,
        answer_kind="cases_only",
        cases=(_case(),),
    )


def test_temporary_unavailability_is_a_public_answer_without_cases() -> None:
    message = "서버 부하로 약간의 대기 시간이 필요합니다. 잠시 후 다시 시도해 주세요."
    answer = temporarily_unavailable_answer()

    assert json.loads(canonical_public_answer_json(answer)) == {
        "answer": message,
        "answer_kind": "temporarily_unavailable",
        "cases": [],
        "schema_version": "senqa-public-answer/v1",
    }


def test_canonical_json_has_only_public_fields() -> None:
    encoded = canonical_public_answer_json(
        PublicAnswer(
            answer="계약 기준입니다. [2022년 · PDF 4쪽]",
            answer_kind="grounded",
            cases=(_case(),),
        )
    )
    assert encoded.endswith("\n")
    payload = json.loads(encoded)
    assert set(payload) == {"answer", "answer_kind", "cases", "schema_version"}
    assert set(payload["cases"][0]) == {
        "answer",
        "case_id",
        "edition_year",
        "pdf_pages",
        "question",
        "title",
    }


def test_public_models_reject_wrong_runtime_types() -> None:
    with pytest.raises(ValueError, match="public_answer_invalid"):
        PublicCase(
            case_id="senqa-2022-case-a",
            edition_year=True,  # type: ignore[arg-type]
            pdf_pages=(4,),
            title="학교 계약",
            question="수의계약",
            answer="기준",
        )
