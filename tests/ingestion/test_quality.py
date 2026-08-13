from __future__ import annotations

import json
import os
import stat
import traceback
import warnings
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.corpus.ids import make_case_id
from src.corpus.models import Case, Document, SourceSpan
from src.ingestion.policy import OCR_LOW_CONFIDENCE_THRESHOLD
from src.ingestion.quality import (
    OcrLayoutReview,
    QualityAssessment,
    QualityFinding,
    QualityGateError,
    QualityReason,
    assess_case,
    write_review_queue,
)

SOURCE_TEXT = "질문과 답변의 승인 원문"


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _document(
    *,
    year: int = 2025,
    page_count: int = 20,
    extraction_method: str = "ocr",
    redistribution_status: str = "unverified",
    access_level: str = "staff",
) -> Document:
    return Document.model_validate(
        {
            "doc_id": f"sen-qa-{year}",
            "edition_year": year,
            "title": f"{year} 사례집",
            "publisher": "교육기관",
            "source_filename": f"{year}.pdf",
            "sha256": "a" * 64,
            "pdf_page_count": page_count,
            "extraction_method": extraction_method,
            "source_dpi": 300 if extraction_method == "ocr" else None,
            "public_url": None,
            "redistribution_status": redistribution_status,
            "access_level": access_level,
            "page_numbering_rule": "body_same_as_pdf",
            "ingestion_version": "corpus-v1",
        }
    )


def _case(
    *,
    year: int = 2025,
    case_id: str | None = None,
    case_type: str = "qa",
    domain: str = "계약",
    part: str = "계약 일반",
    case_no: str = "1",
    page: int = 13,
    source_text: str = SOURCE_TEXT,
    span_hash_text: str | None = None,
    question: str | None = "질문",
    answer: str | None = "답변",
    facts: str | None = None,
    basis_text: str | None = "근거",
    law_ref_ids: tuple[str, ...] = (),
    extraction_source: str = "ocr",
    confidence: float = 0.95,
    critical_field_review: str = "verified",
    pii_class: str = "none",
    review_status: str = "machine_extracted",
) -> Case:
    if case_type == "audit" and facts is None:
        facts = "익명화된 사실관계"
    if case_type == "law_index" and not law_ref_ids:
        law_ref_ids = ("lawref-2025-000001",)
    stable_case_id = case_id or make_case_id(year, domain, part, case_no)
    search_eligible = review_status in {"search_approved", "approved"}
    answer_eligible = review_status == "approved"
    if case_type == "credits" or pii_class in {"public_credit", "restricted"}:
        search_eligible = False
        answer_eligible = False
    return Case.model_validate(
        {
            "case_id": stable_case_id,
            "legacy_ids": (),
            "doc_id": f"sen-qa-{year}",
            "case_type": case_type,
            "domain": domain,
            "part": part,
            "subtopic": None,
            "case_no": case_no,
            "title_raw": "원문 제목",
            "title_normalized": "정규화 제목",
            "question": question,
            "answer": answer,
            "facts": facts,
            "basis_text": basis_text,
            "law_ref_ids": law_ref_ids,
            "source_spans": (
                SourceSpan(
                    pdf_page_index=page,
                    page_label=str(page),
                    bbox=(10.0, 20.0, 100.0, 200.0),
                    text_sha256=_digest(span_hash_text or source_text),
                ),
            ),
            "extraction_source": extraction_source,
            "extraction_confidence": confidence,
            "critical_field_review": critical_field_review,
            "pii_class": pii_class,
            "anonymization_status": "not_required",
            "currency_status": "historical_reference",
            "search_eligible": search_eligible,
            "answer_eligible": answer_eligible,
            "review_status": review_status,
        }
    )


def _reason_codes(assessment: QualityAssessment) -> set[str]:
    return {finding.reason_code for finding in assessment.findings}


def _exception_diagnostics(error: BaseException) -> str:
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    diagnostics: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        diagnostics.extend((str(current), repr(current)))
        if isinstance(current, ValidationError):
            diagnostics.append(repr(current.errors()))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(diagnostics)


@pytest.mark.parametrize(
    "bad_case_id",
    [
        make_case_id(2024, "계약", "계약 일반", "1"),
        make_case_id(2025, "감사", "계약 일반", "1"),
        "010-1234-5678",
    ],
)
def test_case_identity_must_match_canonical_fields_without_echo(
    bad_case_id: str,
) -> None:
    candidate = _case(case_id=bad_case_id)

    with pytest.raises(QualityGateError, match="case identifier") as error:
        assess_case(
            candidate,
            _document(),
            {0: SOURCE_TEXT},
            ocr_layout_review="sample_passed",
        )

    assert bad_case_id not in str(error.value)


def test_canonical_duplicate_case_identity_is_accepted() -> None:
    duplicate_id = make_case_id(
        2025,
        "계약",
        "계약 일반",
        "1",
        13,
        "원문 제목",
        duplicate=True,
    )

    assessment = assess_case(
        _case(case_id=duplicate_id),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert assessment.automated_quality_passed is True


def test_qa_requires_question_and_answer_but_never_reports_values() -> None:
    secret = "010-1234-5678"
    candidate = _case(question=None, answer=None, source_text=secret)

    assessment = assess_case(candidate, _document(), {0: secret}, ocr_layout_review="sample_passed")

    assert "required-field-missing" in _reason_codes(assessment)
    assert assessment.automated_quality_passed is False
    assert assessment.target_review_status == "needs_review"
    assert secret not in assessment.model_dump_json()


@pytest.mark.parametrize(
    ("candidate", "expected_reason"),
    [
        (_case(case_type="audit", facts="", answer=None), "required-field-missing"),
        (
            _case(case_type="law_index", basis_text=None, law_ref_ids=("lawref-2025-000001",)),
            "required-field-missing",
        ),
        (_case(case_type="credits", pii_class="public_credit"), "credits-excluded"),
    ],
)
def test_case_type_specific_required_fields(candidate: Case, expected_reason: str) -> None:
    assessment = assess_case(
        candidate,
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert expected_reason in _reason_codes(assessment)


@pytest.mark.parametrize(
    ("law_ref_ids", "expected_reason"),
    [
        (("",), "required-field-missing"),
        (("   ",), "required-field-missing"),
        (("lawref-2025-000001", "lawref-2025-000001"), "law-reference-invalid"),
    ],
)
def test_law_index_requires_nonblank_unique_references(
    law_ref_ids: tuple[str, ...], expected_reason: str
) -> None:
    assessment = assess_case(
        _case(case_type="law_index", law_ref_ids=law_ref_ids),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert expected_reason in _reason_codes(assessment)


def test_source_text_registry_is_required_and_hash_checked_without_hash_leak() -> None:
    candidate = _case(span_hash_text="expected source")

    missing = assess_case(candidate, _document(), {}, ocr_layout_review="sample_passed")
    changed = assess_case(
        candidate,
        _document(),
        {0: "changed private source"},
        ocr_layout_review="sample_passed",
    )

    assert "source-text-missing" in _reason_codes(missing)
    assert "source-text-hash-mismatch" in _reason_codes(changed)
    serialized = changed.model_dump_json()
    assert "changed private source" not in serialized
    assert _digest("changed private source") not in serialized


def test_document_page_and_extraction_contracts_fail_closed() -> None:
    candidate = _case(page=21, extraction_source="ocr")
    document = _document(page_count=20, extraction_method="native")

    assessment = assess_case(
        candidate,
        document,
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert {"page-out-of-range", "extraction-method-mismatch"} <= _reason_codes(assessment)


@pytest.mark.parametrize(
    ("year", "method", "confidence", "expected_reason"),
    [
        (2020, "native", 1.0, None),
        (2022, "native", 1.0, None),
        (2023, "ocr", 0.95, None),
        (2025, "ocr", 0.95, None),
        (2022, "ocr", 0.95, "year-extraction-policy-mismatch"),
        (2025, "native", 1.0, "year-extraction-policy-mismatch"),
        (2019, "native", 1.0, "unsupported-edition-year"),
        (2026, "ocr", 0.95, "unsupported-edition-year"),
    ],
)
def test_year_extraction_policy_matrix(
    year: int,
    method: str,
    confidence: float,
    expected_reason: str | None,
) -> None:
    layout_review: OcrLayoutReview = (
        "sample_passed" if year == 2025 and method == "ocr" else "not_applicable"
    )
    assessment = assess_case(
        _case(year=year, extraction_source=method, confidence=confidence),
        _document(year=year, extraction_method=method),
        {0: SOURCE_TEXT},
        ocr_layout_review=layout_review,
    )

    if expected_reason is None:
        assert "year-extraction-policy-mismatch" not in _reason_codes(assessment)
        assert "unsupported-edition-year" not in _reason_codes(assessment)
    else:
        assert expected_reason in _reason_codes(assessment)


@pytest.mark.parametrize(
    ("confidence", "expected_failure"),
    [(1.0, False), (0.999999, True)],
)
def test_native_extraction_confidence_must_be_exact(
    confidence: float, expected_failure: bool
) -> None:
    assessment = assess_case(
        _case(year=2022, extraction_source="native", confidence=confidence),
        _document(year=2022, extraction_method="native"),
        {0: SOURCE_TEXT},
        ocr_layout_review="not_applicable",
    )

    assert ("native-confidence-not-exact" in _reason_codes(assessment)) is expected_failure


@pytest.mark.parametrize(
    ("confidence", "passes"),
    [
        (OCR_LOW_CONFIDENCE_THRESHOLD, True),
        (OCR_LOW_CONFIDENCE_THRESHOLD - 0.0001, False),
    ],
)
def test_confidence_threshold_is_shared_and_inclusive(confidence: float, passes: bool) -> None:
    assessment = assess_case(
        _case(confidence=confidence),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert ("low-confidence" not in _reason_codes(assessment)) is passes


@pytest.mark.parametrize("year", [2023, 2024])
def test_2023_2024_ocr_requires_verified_critical_fields(year: int) -> None:
    assessment = assess_case(
        _case(year=year, critical_field_review="unverified"),
        _document(year=year),
        {0: SOURCE_TEXT},
        ocr_layout_review="not_applicable",
    )

    assert "critical-fields-unverified" in _reason_codes(assessment)


@pytest.mark.parametrize(
    ("layout_review", "expected_reason"),
    [
        ("unreviewed", "ocr-layout-review-missing"),
        ("error_found", "ocr-layout-error"),
        ("sample_passed", None),
        ("segment_verified", None),
    ],
)
def test_2025_ocr_layout_policy_is_explicit(
    layout_review: OcrLayoutReview, expected_reason: str | None
) -> None:
    assessment = assess_case(
        _case(),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review=layout_review,
    )

    if expected_reason is None:
        assert "ocr-layout-review-missing" not in _reason_codes(assessment)
        assert "ocr-layout-error" not in _reason_codes(assessment)
    else:
        assert expected_reason in _reason_codes(assessment)


@pytest.mark.parametrize("review_status", ["search_approved", "approved", "rejected"])
def test_quality_gate_cannot_bypass_initial_review_queue(review_status: str) -> None:
    assessment = assess_case(
        _case(review_status=review_status),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    assert "review-state-not-candidate" in _reason_codes(assessment)
    assert assessment.target_review_status == "needs_review"
    assert assessment.search_eligible is False
    assert assessment.answer_eligible is False


@pytest.mark.parametrize(
    ("pii_class", "expected_reason"),
    [
        ("restricted", "restricted-pii"),
        ("public_credit", "public-credit-excluded"),
        ("quasi_identifier", None),
        ("anonymized_case", None),
        ("none", None),
    ],
)
def test_privacy_class_controls_quality_without_auto_approval(
    pii_class: str, expected_reason: str | None
) -> None:
    assessment = assess_case(
        _case(pii_class=pii_class),
        _document(redistribution_status="approved", access_level="public"),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    if expected_reason is not None:
        assert expected_reason in _reason_codes(assessment)
    assert assessment.public_redistribution_candidate is False
    assert assessment.search_eligible is False
    assert assessment.answer_eligible is False


def test_findings_are_deterministic_and_only_contain_location_counts() -> None:
    secret = "s" + "k" + "-live-do-not-report-this-value"
    assessment = assess_case(
        _case(question=None, confidence=0.1, source_text=secret, pii_class="restricted"),
        _document(),
        {0: secret},
        ocr_layout_review="error_found",
    )

    assert tuple(assessment.findings) == tuple(
        sorted(
            assessment.findings,
            key=lambda item: (item.case_id, item.page_id or 0, item.reason_code),
        )
    )
    dumped = assessment.model_dump_json()
    assert secret not in dumped
    assert _digest(secret) not in dumped
    assert all(finding.count >= 1 for finding in assessment.findings)


@pytest.mark.parametrize(
    "case_no",
    [
        "010-1234-5678",
        "110-123-456789",
        "900101-1234567",
        "sk" + "-proj-" + "abcdefghijklmnop123456",
    ],
)
def test_sensitive_source_case_numbers_fail_before_quality_metadata_without_echo(
    case_no: str,
) -> None:
    with pytest.raises(ValueError, match="case number") as error:
        _case(case_no=case_no)

    diagnostics = "\n".join(
        (
            str(error.value),
            repr(error.value),
            "".join(traceback.format_exception(error.value)),
        )
    )
    reversible_digest = sha256(case_no.lower().encode("utf-8")).hexdigest()[:12]
    assert case_no not in diagnostics
    assert reversible_digest not in diagnostics


def test_quality_metadata_boundary_rejects_value_bearing_canonical_shape_without_echo() -> None:
    unsafe_id = "senqa-2025-contract-contract-general-110123456789"

    with pytest.raises(ValueError, match="case identifier") as error:
        QualityFinding(
            reason_code="required-field-missing",
            case_id=unsafe_id,
            page_id=None,
            count=1,
        )

    assert unsafe_id not in str(error.value)


def test_quality_assessment_binds_every_finding_to_its_case_and_pages() -> None:
    first_case_id = make_case_id(2025, "계약", "계약 일반", "1")
    second_case_id = make_case_id(2025, "계약", "계약 일반", "2")
    foreign_case = QualityFinding(
        reason_code="required-field-missing",
        case_id=second_case_id,
        page_id=13,
        count=1,
    )
    foreign_page = QualityFinding(
        reason_code="source-text-hash-mismatch",
        case_id=first_case_id,
        page_id=999,
        count=1,
    )

    with pytest.raises(ValueError, match="finding case"):
        QualityAssessment(
            case_id=first_case_id,
            page_ids=(13,),
            findings=(foreign_case,),
            automated_quality_passed=False,
        )
    with pytest.raises(ValueError, match="finding page"):
        QualityAssessment(
            case_id=first_case_id,
            page_ids=(13,),
            findings=(foreign_page,),
            automated_quality_passed=False,
        )
    with pytest.raises(ValueError, match="at least one page"):
        QualityAssessment(
            case_id=first_case_id,
            page_ids=(),
            findings=(),
            automated_quality_passed=True,
        )


@pytest.mark.parametrize(
    "reason_code",
    ["page-out-of-range", "source-text-missing", "source-text-hash-mismatch"],
)
def test_source_page_findings_require_a_bound_page(
    reason_code: QualityReason,
) -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    finding = QualityFinding(
        reason_code=reason_code,
        case_id=case_id,
        page_id=None,
        count=1,
    )

    with pytest.raises(ValueError, match="finding page"):
        QualityAssessment(
            case_id=case_id,
            page_ids=(13,),
            findings=(finding,),
            automated_quality_passed=False,
        )


def test_quality_assessment_rejects_duplicate_finding_keys() -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    finding = QualityFinding(
        reason_code="required-field-missing",
        case_id=case_id,
        page_id=None,
        count=1,
    )

    with pytest.raises(ValueError, match="unique"):
        QualityAssessment(
            case_id=case_id,
            page_ids=(13,),
            findings=(finding, finding.model_copy(update={"count": 2})),
            automated_quality_passed=False,
        )


def test_quality_assessment_requires_canonical_finding_order() -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    page_finding = QualityFinding(
        reason_code="source-text-missing",
        case_id=case_id,
        page_id=13,
        count=1,
    )
    case_finding = QualityFinding(
        reason_code="required-field-missing",
        case_id=case_id,
        page_id=None,
        count=1,
    )

    with pytest.raises(ValueError, match="sorted"):
        QualityAssessment(
            case_id=case_id,
            page_ids=(13,),
            findings=(page_finding, case_finding),
            automated_quality_passed=False,
        )


def test_review_queue_revalidates_constructed_assessment_before_serializing(
    tmp_path: Path,
) -> None:
    first_case_id = make_case_id(2025, "계약", "계약 일반", "1")
    second_case_id = make_case_id(2025, "계약", "계약 일반", "2")
    forged = QualityAssessment.model_construct(
        case_id=first_case_id,
        page_ids=(13,),
        findings=(
            QualityFinding(
                reason_code="source-text-hash-mismatch",
                case_id=second_case_id,
                page_id=999,
                count=1,
            ),
        ),
        automated_quality_passed=False,
        target_review_status="needs_review",
        search_eligible=False,
        answer_eligible=False,
        public_redistribution_candidate=False,
    )

    with pytest.raises(QualityGateError, match="assessment is invalid"):
        write_review_queue(
            tmp_path,
            "corpus-20260808123001-5a719340",
            [forged],
        )


def test_review_queue_rejects_untyped_findings_without_diagnostic_value_leak(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    secret = "s" + "k" + "-proj-forged-quality-finding-value"
    forged = QualityAssessment.model_construct(
        case_id=case_id,
        page_ids=(13,),
        findings=(secret,),
        automated_quality_passed=False,
        target_review_status="needs_review",
        search_eligible=False,
        answer_eligible=False,
        public_redistribution_candidate=False,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(QualityGateError, match="assessment is invalid") as error:
            write_review_queue(
                tmp_path,
                "corpus-20260808123001-5a719340",
                [forged],
            )

    diagnostics = "\n".join(
        [
            _exception_diagnostics(error.value),
            "".join(traceback.format_exception(error.value)),
            caplog.text,
            *(str(warning.message) for warning in caught),
        ]
    )
    assert secret not in diagnostics
    assert error.value.__cause__ is None
    assert caught == []


def test_review_queue_discards_sensitive_pydantic_validation_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    secret = "s" + "k" + "-proj-invalid-declared-reason"
    forged_finding = QualityFinding.model_construct(
        reason_code=secret,
        case_id=case_id,
        page_id=None,
        count=1,
    )
    forged = QualityAssessment.model_construct(
        case_id=case_id,
        page_ids=(13,),
        findings=(forged_finding,),
        automated_quality_passed=False,
        target_review_status="needs_review",
        search_eligible=False,
        answer_eligible=False,
        public_redistribution_candidate=False,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(QualityGateError, match="assessment is invalid") as error:
            write_review_queue(
                tmp_path,
                "corpus-20260808123001-5a719340",
                [forged],
            )

    diagnostics = "\n".join(
        [
            _exception_diagnostics(error.value),
            "".join(traceback.format_exception(error.value)),
            caplog.text,
            *(str(warning.message) for warning in caught),
        ]
    )
    assert secret not in diagnostics
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert caught == []


def test_review_queue_rejects_assessment_subtypes(tmp_path: Path) -> None:
    class AssessmentSubtype(QualityAssessment):
        pass

    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    supplied = AssessmentSubtype(
        case_id=case_id,
        page_ids=(13,),
        findings=(),
        automated_quality_passed=True,
    )

    with pytest.raises(QualityGateError, match="assessment is invalid"):
        write_review_queue(
            tmp_path,
            "corpus-20260808123001-5a719340",
            [supplied],
        )


def test_review_queue_rejects_undeclared_model_fields_without_echo(
    tmp_path: Path,
) -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    secret = "s" + "k" + "-proj-undeclared-quality-field"
    forged = QualityAssessment.model_construct(
        case_id=case_id,
        page_ids=(13,),
        findings=(),
        automated_quality_passed=True,
        target_review_status="needs_review",
        search_eligible=False,
        answer_eligible=False,
        public_redistribution_candidate=False,
    )
    forged.__dict__["undeclared"] = secret

    with pytest.raises(QualityGateError, match="assessment is invalid") as error:
        write_review_queue(
            tmp_path,
            "corpus-20260808123001-5a719340",
            [forged],
        )

    assert secret not in _exception_diagnostics(error.value)
    assert error.value.__cause__ is None


def test_review_queue_rejects_constructed_nondeterministic_finding_order(
    tmp_path: Path,
) -> None:
    case_id = make_case_id(2025, "계약", "계약 일반", "1")
    page_finding = QualityFinding(
        reason_code="source-text-missing",
        case_id=case_id,
        page_id=13,
        count=1,
    )
    case_finding = QualityFinding(
        reason_code="required-field-missing",
        case_id=case_id,
        page_id=None,
        count=1,
    )
    forged = QualityAssessment.model_construct(
        case_id=case_id,
        page_ids=(13,),
        findings=(page_finding, case_finding),
        automated_quality_passed=False,
        target_review_status="needs_review",
        search_eligible=False,
        answer_eligible=False,
        public_redistribution_candidate=False,
    )

    with pytest.raises(QualityGateError, match="assessment is invalid"):
        write_review_queue(
            tmp_path,
            "corpus-20260808123001-5a719340",
            [forged],
        )


def test_review_queue_is_atomic_restrictive_deterministic_and_value_free(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o750)
    secret = "eyJ" + "hbGciOiJIUzI1NiJ9." + "private." + "signature"
    first = assess_case(
        _case(
            case_no="2",
            question=None,
            confidence=0.1,
            pii_class="restricted",
            source_text=secret,
        ),
        _document(),
        {0: secret},
        ocr_layout_review="sample_passed",
    )
    second = assess_case(
        _case(case_no="1"),
        _document(),
        {0: SOURCE_TEXT},
        ocr_layout_review="sample_passed",
    )

    output = write_review_queue(
        artifact_root,
        "corpus-20260808123001-5a719340",
        [first, second],
    )
    initial = output.read_bytes()
    write_review_queue(
        artifact_root,
        "corpus-20260808123001-5a719340",
        [second, first],
    )

    assert output.read_bytes() == initial
    assert secret.encode() not in initial
    assert _digest(secret).encode() not in initial
    records = [json.loads(line) for line in initial.decode().splitlines()]
    assert [record["case_id"] for record in records] == sorted(
        record["case_id"] for record in records
    )
    record_keys = [
        (
            record["case_id"],
            record["page_id"] is not None,
            record["page_id"] or 0,
            record["reason_code"],
        )
        for record in records
    ]
    assert len(records) == 4
    assert record_keys == sorted(record_keys)
    assert all(
        set(record) == {"case_id", "page_id", "reason_code", "count"}
        for record in records
    )
    assert records[0] == {
        "case_id": second.case_id,
        "page_id": None,
        "reason_code": "human-review-required",
        "count": 1,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o750


def test_review_queue_preserves_each_finding_page_association(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    first_text = "첫 페이지 원문"
    second_text = "둘째 페이지 원문"
    candidate = _case(source_text=first_text)
    candidate = Case.model_validate(
        {
            **candidate.model_dump(),
            "source_spans": (
                SourceSpan(
                    pdf_page_index=13,
                    page_label="13",
                    bbox=(10.0, 20.0, 100.0, 200.0),
                    text_sha256=_digest(first_text),
                ),
                SourceSpan(
                    pdf_page_index=14,
                    page_label="14",
                    bbox=(10.0, 20.0, 100.0, 200.0),
                    text_sha256=_digest(second_text),
                ),
            ),
        }
    )
    assessment = assess_case(
        candidate,
        _document(),
        {0: first_text, 1: "변경된 둘째 페이지"},
        ocr_layout_review="sample_passed",
    )

    output = write_review_queue(
        artifact_root,
        "corpus-20260808123001-5a719340",
        [assessment],
    )

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "case_id": candidate.case_id,
        "page_id": 14,
        "reason_code": "source-text-hash-mismatch",
        "count": 1,
    }


@pytest.mark.parametrize(
    "release_id",
    [
        "../escape",
        "corpus-latest",
        "corpus-20260808123001-5a71934",
        "bad\nvalue",
        "corpus-20261308123001-5a719340",
        "corpus-20260230123001-5a719340",
        "corpus-20260808243001-5a719340",
        "corpus-20260808126001-5a719340",
        "corpus-20260808123061-5a719340",
    ],
)
def test_review_queue_rejects_unsafe_release_ids(tmp_path: Path, release_id: str) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    assessment = assess_case(
        _case(), _document(), {0: SOURCE_TEXT}, ocr_layout_review="sample_passed"
    )

    with pytest.raises(QualityGateError, match="release identifier"):
        write_review_queue(artifact_root, release_id, [assessment])


def test_review_queue_rejects_symlinked_storage(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    assessment = assess_case(
        _case(), _document(), {0: SOURCE_TEXT}, ocr_layout_review="sample_passed"
    )

    with pytest.raises(QualityGateError, match="storage path"):
        write_review_queue(
            linked_root,
            "corpus-20260808123001-5a719340",
            [assessment],
        )


def test_review_queue_rejects_symlinked_ancestor_and_output(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    artifact_root = linked_parent / "artifacts"
    artifact_root.mkdir()
    assessment = assess_case(
        _case(), _document(), {0: SOURCE_TEXT}, ocr_layout_review="sample_passed"
    )

    with pytest.raises(QualityGateError, match="storage path"):
        write_review_queue(
            artifact_root,
            "corpus-20260808123001-5a719340",
            [assessment],
        )

    safe_root = tmp_path / "safe-artifacts"
    safe_root.mkdir()
    queue_directory = safe_root / "review-queue"
    queue_directory.mkdir()
    external = tmp_path / "external.jsonl"
    external.write_text("unchanged", encoding="utf-8")
    (queue_directory / "corpus-20260808123001-5a719340.jsonl").symlink_to(external)
    with pytest.raises(QualityGateError, match="storage path"):
        write_review_queue(
            safe_root,
            "corpus-20260808123001-5a719340",
            [assessment],
        )
    assert external.read_text(encoding="utf-8") == "unchanged"


def test_review_queue_rejects_empty_or_duplicate_case_sets(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    assessment = assess_case(
        _case(), _document(), {0: SOURCE_TEXT}, ocr_layout_review="sample_passed"
    )

    with pytest.raises(QualityGateError, match="empty"):
        write_review_queue(
            artifact_root,
            "corpus-20260808123001-5a719340",
            [],
        )
    with pytest.raises(QualityGateError, match="duplicate"):
        write_review_queue(
            artifact_root,
            "corpus-20260808123001-5a719340",
            [assessment, assessment],
        )


def test_review_queue_preserves_previous_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    assessment = assess_case(
        _case(), _document(), {0: SOURCE_TEXT}, ocr_layout_review="sample_passed"
    )
    output = write_review_queue(
        artifact_root,
        "corpus-20260808123001-5a719340",
        [assessment],
    )
    previous = output.read_bytes()

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("src.ingestion.quality.os.replace", fail_replace)
    with pytest.raises(QualityGateError, match="review queue"):
        write_review_queue(
            artifact_root,
            "corpus-20260808123001-5a719340",
            [assessment],
        )

    assert output.read_bytes() == previous
    assert not list(output.parent.glob("*.tmp"))
