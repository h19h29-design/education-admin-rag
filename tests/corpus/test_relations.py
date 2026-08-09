"""Behavior contracts for exact law citations and reviewed case relations."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date

import pytest

from src.corpus.models import Case, CaseRelation, SourceSpan
from src.corpus.relations import (
    LawSource,
    RelationApproval,
    RelationError,
    VerifiedCaseRelation,
    VerifiedLawRef,
    canonicalize_relation,
    extract_law_refs,
    propose_relation,
    revalidate_verified_law_ref,
    revalidate_verified_relation,
)


def _span(raw_text: str, *, page: int = 17) -> SourceSpan:
    return SourceSpan(
        pdf_page_index=page,
        page_label=str(page),
        bbox=(10.0, 20.0, 500.0, 40.0),
        text_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
    )


def _case(
    case_id: str,
    *,
    title: str,
    raw_text: str,
    basis_text: str | None = None,
) -> Case:
    return Case(
        case_id=case_id,
        legacy_ids=(),
        doc_id="sen-qa-2025",
        case_type="qa",
        domain="계약",
        part="계약 일반",
        subtopic=None,
        case_no=case_id.rsplit("-", 1)[-1],
        title_raw=title,
        title_normalized=title,
        question="계약 절차 문의",
        answer="관련 법령에 따라 처리합니다",
        facts=None,
        basis_text=basis_text,
        law_ref_ids=(),
        source_spans=(_span(raw_text),),
        extraction_source="ocr",
        extraction_confidence=0.99,
        critical_field_review="verified",
        pii_class="none",
        anonymization_status="not_required",
        currency_status="historical_reference",
        search_eligible=True,
        answer_eligible=True,
        review_status="approved",
    )


def test_law_reference_preserves_printed_name_quote_and_span() -> None:
    """Catches citation parsing replacing the historical printed source."""
    raw = '근거: 「지방계약법」 제12조제3항제2호 (시행 2024. 7. 1.) "원문 인용"'
    normalized = '「지방계약법」 제12조제3항제2호 (시행 2024. 7. 1.) "원문 인용"'
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    refs = extract_law_refs(case, (LawSource(normalized, raw, 0),))

    assert len(refs) == 1
    law_ref = refs[0]
    assert law_ref.display_name == "지방계약법"
    assert law_ref.article == "제12조"
    assert law_ref.paragraph == "제3항"
    assert law_ref.item == "제2호"
    assert law_ref.cited_effective_date == date(2024, 7, 1)
    assert law_ref.quote == raw
    assert law_ref.source_span == case.source_spans[0]
    assert isinstance(law_ref, VerifiedLawRef)
    assert law_ref.source_span_index == 0
    assert revalidate_verified_law_ref(law_ref, case) == law_ref


def test_law_reference_does_not_inherit_case_level_human_approval() -> None:
    """Catches a reviewed Case laundering heuristic LawRef fields to approved."""
    raw = "근거: 「지방계약법」 제12조"
    normalized = "「지방계약법」 제12조"
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    law_ref = extract_law_refs(case, (LawSource(normalized, raw, 0),))[0]

    assert law_ref.law_ref.review_status == "needs_review"


def test_verified_law_reference_keeps_unambiguous_duplicate_span_index() -> None:
    """Catches storage guessing an index when two canonical spans compare equal."""
    raw = "근거: 「지방계약법」 제12조"
    normalized = "「지방계약법」 제12조"
    original = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )
    case = original.model_copy(
        update={"source_spans": (original.source_spans[0], original.source_spans[0])}
    )

    refs = extract_law_refs(case, (LawSource(normalized, raw, 1),))

    assert refs[0].source_span_index == 1


def test_law_reference_rejects_normalized_name_substitution() -> None:
    """Catches automatic current-name substitution being presented as source text."""
    raw = "근거: 「구 지방계약법」 제12조"
    normalized = "「현행 지방계약법」 제12조"
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    with pytest.raises(RelationError, match="printed citation"):
        extract_law_refs(case, (LawSource(normalized, raw, 0),))


def test_law_reference_preserves_explicit_printed_abbreviation() -> None:
    """Catches an explicit historical alias being discarded or normalized."""
    raw = '근거: 「지방자치단체를 당사자로 하는 계약에 관한 법률」 이하 "지방계약법"이라 한다 제12조'
    normalized = raw.removeprefix("근거: ")
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    refs = extract_law_refs(case, (LawSource(normalized, raw, 0),))

    assert refs[0].abbreviation == "지방계약법"


def test_law_reference_rejects_normalized_only_abbreviation() -> None:
    """Catches an alias absent from the printed raw citation being invented."""
    raw = "근거: 「지방계약법」 제12조"
    normalized = '「지방계약법」 이하 "지계법"이라 한다 제12조'
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    with pytest.raises(RelationError, match="printed citation"):
        extract_law_refs(case, (LawSource(normalized, raw, 0),))


def test_law_reference_cannot_fabricate_article_or_date_from_normalized_text() -> None:
    """Catches legal fields absent from raw source being invented by normalization."""
    raw = "근거: 「지방계약법」"
    normalized = "「지방계약법」 제999조 (시행 2099. 12. 31.)"
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    with pytest.raises(RelationError, match="printed citation"):
        extract_law_refs(case, (LawSource(normalized, raw, 0),))


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("근거: 「지방계약법」 제12조", "「지방계약법」 제12조제9항"),
        ("근거: 「지방계약법」 제12조제3항", "「지방계약법」 제12조제3항제8호"),
        (
            "근거: 「지방계약법」 제12조 (시행 2024. 7. 1.)",
            "「지방계약법」 제12조 (시행 2099. 7. 1.)",
        ),
    ],
)
def test_law_reference_rejects_each_normalized_only_protected_field(
    raw: str,
    normalized: str,
) -> None:
    """Catches paragraph, item, and date drift hidden behind a matching law name."""
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="2단계 입찰",
        raw_text=raw,
        basis_text=normalized,
    )

    with pytest.raises(RelationError, match="printed citation"):
        extract_law_refs(case, (LawSource(normalized, raw, 0),))


@pytest.mark.parametrize("relation_type", ["supersedes", "conflicts"])
def test_sensitive_relation_requires_externally_pinned_review(
    relation_type: str,
) -> None:
    """Catches chronology or a self-asserted approved field promoting policy relations."""
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )
    candidate = propose_relation(
        first,
        second,
        relation_type=relation_type,
        confidence=0.9,
        evidence_sha256="a" * 64,
    )

    with pytest.raises(RelationError, match="review approval"):
        canonicalize_relation(
            candidate, approval=None, expected_approval_sha256="b" * 64
        )

    attacker = RelationApproval.create(
        candidate,
        reviewer_id="attacker",
        evidence_sha256="a" * 64,
    )
    with pytest.raises(RelationError, match="pinned approval"):
        canonicalize_relation(
            candidate,
            approval=attacker,
            expected_approval_sha256="b" * 64,
        )


def test_related_and_duplicate_candidates_have_one_symmetric_orientation() -> None:
    """Catches reverse duplicates creating two relation identities."""
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="동일 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="동일 절차",
        raw_text="근거 없음",
    )

    forward = propose_relation(
        first,
        second,
        relation_type="duplicate",
        confidence=1.0,
        evidence_sha256="c" * 64,
    )
    reverse = propose_relation(
        second,
        first,
        relation_type="duplicate",
        confidence=1.0,
        evidence_sha256="c" * 64,
    )

    assert forward == reverse
    assert forward.source_case_id < forward.target_case_id
    assert forward.relation_type == "duplicate"


def test_verified_relation_retains_approval_and_endpoint_content_bindings() -> None:
    """Catches canonicalization collapsing evidence into a forgeable plain model."""
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )
    candidate = propose_relation(
        first,
        second,
        relation_type="supersedes",
        confidence=0.9,
        evidence_sha256="a" * 64,
    )
    approval = RelationApproval.create(
        candidate,
        reviewer_id="reviewer-a",
        evidence_sha256="a" * 64,
    )

    verified = canonicalize_relation(
        candidate,
        approval=approval,
        expected_approval_sha256=approval.fingerprint_sha256,
    )

    assert isinstance(verified, VerifiedCaseRelation)
    assert verified.relation.relation_type == "supersedes"
    assert verified.approval_sha256 == approval.fingerprint_sha256
    assert verified.source_content_sha256 == candidate.source_content_sha256
    assert verified.target_content_sha256 == candidate.target_content_sha256
    assert (
        revalidate_verified_relation(
            verified,
            first,
            second,
            expected_approval_sha256=approval.fingerprint_sha256,
        )
        == verified
    )

    forged_plain = CaseRelation(
        relation_id=verified.relation.relation_id,
        source_case_id=verified.relation.source_case_id,
        target_case_id=verified.relation.target_case_id,
        relation_type="supersedes",
        confidence=0.9,
        review_status="approved",
    )
    with pytest.raises(RelationError, match="verified relation"):
        revalidate_verified_relation(
            forged_plain,
            first,
            second,
            expected_approval_sha256=approval.fingerprint_sha256,
        )

    changed_second = second.model_copy(update={"answer": "변경된 답변"})
    with pytest.raises(RelationError, match="endpoint content"):
        revalidate_verified_relation(
            verified,
            first,
            changed_second,
            expected_approval_sha256=approval.fingerprint_sha256,
        )

    forged_wrapper = object.__new__(VerifiedCaseRelation)
    object.__setattr__(forged_wrapper, "relation", verified.relation)
    object.__setattr__(forged_wrapper, "approval", verified.approval)
    object.__setattr__(forged_wrapper, "approval_sha256", verified.approval_sha256)
    object.__setattr__(
        forged_wrapper,
        "source_content_sha256",
        verified.source_content_sha256,
    )
    object.__setattr__(
        forged_wrapper,
        "target_content_sha256",
        verified.target_content_sha256,
    )
    object.__setattr__(forged_wrapper, "binding_sha256", "0" * 64)
    with pytest.raises(RelationError, match="binding"):
        revalidate_verified_relation(
            forged_wrapper,
            first,
            second,
            expected_approval_sha256=approval.fingerprint_sha256,
        )


def test_relation_approval_binds_confidence_exactly() -> None:
    """Catches reuse of an approval after changing a canonical relation score."""
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )
    candidate = propose_relation(
        first,
        second,
        relation_type="supersedes",
        confidence=0.9,
        evidence_sha256="a" * 64,
    )
    approval = RelationApproval.create(
        candidate,
        reviewer_id="reviewer-a",
        evidence_sha256="a" * 64,
    )

    with pytest.raises(RelationError, match="candidate"):
        canonicalize_relation(
            replace(candidate, confidence=0.1),
            approval=approval,
            expected_approval_sha256=approval.fingerprint_sha256,
        )


@pytest.mark.parametrize("confidence", [True, float("nan"), float("inf"), -0.0])
def test_relation_confidence_rejects_noncanonical_numbers(confidence: object) -> None:
    """Catches bool, non-finite, and signed-zero confidence ambiguity."""
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )

    with pytest.raises(RelationError, match="evidence"):
        propose_relation(
            first,
            second,
            relation_type="related",
            confidence=confidence,  # type: ignore[arg-type]
            evidence_sha256="a" * 64,
        )


def test_malformed_exact_relation_dataclass_fails_value_free() -> None:
    """Catches annotation bypass raising a raw TypeError or retaining private values."""
    sentinel = "PRIVATE-RELATION-VALUE"
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )
    candidate = propose_relation(
        first,
        second,
        relation_type="related",
        confidence=0.8,
        evidence_sha256="a" * 64,
    )
    approval = RelationApproval.create(
        candidate,
        reviewer_id="reviewer-a",
        evidence_sha256="a" * 64,
    )
    object.__setattr__(approval, "reviewer_id", [sentinel])

    with pytest.raises(RelationError, match="review approval") as captured:
        canonicalize_relation(
            candidate,
            approval=approval,
            expected_approval_sha256="b" * 64,
        )

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_forged_case_revalidation_emits_no_value_bearing_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches Pydantic serialization warnings leaking rejected source values."""
    sentinel = "PRIVATE-CASE-SENTINEL"
    case = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    )
    forged = case.model_copy(update={"question": object()})
    object.__setattr__(
        forged, "question", type("Private", (), {"__repr__": lambda _: sentinel})()
    )

    with pytest.raises(RelationError, match="canonical case"):
        extract_law_refs(forged, ())

    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_relation_rejects_noncanonical_case_id_without_echoing_it() -> None:
    """Catches provider tokens or long numbers entering relation endpoint IDs."""
    sentinel = "sk-live-privateprovidertoken1234567890"
    first = _case(
        "senqa-2025-contract-contract-general-1",
        title="기존 절차",
        raw_text="근거 없음",
    ).model_copy(update={"case_id": f"senqa-2025-contract-contract-general-{sentinel}"})
    second = _case(
        "senqa-2025-contract-contract-general-2",
        title="변경 절차",
        raw_text="근거 없음",
    )

    with pytest.raises(RelationError, match="relation case") as captured:
        propose_relation(
            first,
            second,
            relation_type="related",
            confidence=0.5,
            evidence_sha256="a" * 64,
        )

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
