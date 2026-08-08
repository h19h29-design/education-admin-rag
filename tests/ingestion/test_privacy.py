from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from src.corpus.ids import make_case_id
from src.ingestion.normalize import normalize_text
from src.ingestion.privacy import (
    CaseType,
    PrivacyFinding,
    classify_privacy,
    scan_text,
)


def _secret_shape(*fragments: str) -> str:
    return "".join(fragments)


_CANONICAL_CASE_ID = make_case_id(2025, "계약", "계약 일반", "1")


def _case_location(field: str) -> str:
    return f"case-{_CANONICAL_CASE_ID}:{field}"


def test_privacy_report_never_contains_detected_values_or_hashes() -> None:
    detected_phone = "010-1234-5678"
    findings = scan_text(
        f"연락처 {detected_phone}, 추가 연락처 {detected_phone}",
        location_id=_case_location("answer"),
    )

    assert len(findings) == 1
    assert findings[0].kind == "phone"
    assert findings[0].location_id == _case_location("answer")
    assert findings[0].count == 2
    serialized = findings[0].model_dump_json()
    assert detected_phone not in serialized
    assert "010" not in serialized
    assert "sha" not in serialized.lower()
    assert detected_phone not in repr(findings)


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("주민번호 900101-1234567", "resident_registration_number"),
        ("OCR 주민번호 후보 9001011234567", "resident_registration_number"),
        ("전화 02-123-4567", "phone"),
        ("메일 test.person+qa@example.org", "email"),
        ("입금 계좌번호 110-123-456789", "bank_account"),
        pytest.param(
            _secret_shape(
                "OPENAI_API_KEY=", "sk", "-", "proj", "-", "abcdefghijklmnop", "123456"
            ),
            "api_token",
            id="openai-token",
        ),
        pytest.param(
            _secret_shape("token gh", "p_", "abcdefghijklmnop", "qrstuvwxyz123456"),
            "api_token",
            id="github-token",
        ),
        pytest.param(
            _secret_shape("key AK", "IA", "IOSFODNN", "7EXAMPLE"),
            "api_token",
            id="aws-token",
        ),
        pytest.param(
            _secret_shape("stripe sk", "_live_", "abcdefghijkl", "mnopqrstuvwx"),
            "api_token",
            id="stripe-token",
        ),
        pytest.param(
            _secret_shape("huggingface h", "f_", "abcdefghijklmnop", "qrstuvwxyz123456"),
            "api_token",
            id="huggingface-token",
        ),
        pytest.param(
            _secret_shape("sendgrid S", "G.", "abcdefghijklmnop", ".", "qrstuvwxyz123456"),
            "api_token",
            id="sendgrid-token",
        ),
        pytest.param(
            _secret_shape(
                "JWT eyJhbGciOi", "JIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0In0", ".", "signature123"
            ),
            "jwt",
            id="jwt-token",
        ),
        ("-----BEGIN PRIVATE KEY-----", "pem_private_key"),
        ("https://worker:password123@internal.example/path", "url_credentials"),
        ("https://service-account@internal.example/path", "url_credentials"),
    ],
)
def test_high_risk_candidate_kinds_are_scanned_across_all_text(
    text: str, expected_kind: str
) -> None:
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert expected_kind in {finding.kind for finding in findings}


def test_labeled_grouped_bank_candidate_and_dotted_phone_are_detected() -> None:
    findings = scan_text(
        "후보 계좌 110-123-456789, 연락 010.1234.5678",
        location_id=_case_location("attachment"),
    )

    assert [(finding.kind, finding.count) for finding in findings] == [
        ("phone", 1),
        ("bank_account", 1),
    ]


@pytest.mark.parametrize(
    "phone",
    ["01012345678", "07012345678", "070-1234-5678"],
)
def test_additional_phone_forms_are_detected(phone: str) -> None:
    findings = scan_text(f"연락처 {phone}", location_id=_case_location("answer"))

    assert [(finding.kind, finding.count) for finding in findings] == [("phone", 1)]


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("연락처 010 - 1234 - 5678", "phone"),
        ("연락처 070-\n1234-5678", "phone"),
        ("입금 계좌 번호: 110-123-456789", "bank_account"),
        ("입금 계좌번호\n110 - 123 - 456789", "bank_account"),
        (
            "성명: 홍길동\n소속: 서울특별시교육청\n직위: 교육연구사",
            "name_organization_title",
        ),
    ],
)
def test_ocr_whitespace_variants_trigger_restricted_lockout(
    text: str,
    expected_kind: str,
) -> None:
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert expected_kind in {finding.kind for finding in findings}
    decision = classify_privacy(
        findings,
        case_type="qa",
        proposed_search_eligible=True,
        proposed_answer_eligible=True,
    )
    assert decision.pii_class == "restricted"
    assert (decision.search_eligible, decision.answer_eligible) == (False, False)


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("연락처 010‐1234‐5678", "phone"),
        ("연락처 070–\n1234–5678", "phone"),
        ("입금 계좌번호 110‐123‐456789", "bank_account"),
        ("입금 계좌번호 110–\n123–456789", "bank_account"),
    ],
)
def test_unicode_dash_ocr_candidates_are_detected(
    text: str,
    expected_kind: str,
) -> None:
    """Catches OCR dash normalization hiding a phone or labeled account."""
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert expected_kind in {finding.kind for finding in findings}


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("연락처 010―1234―5678", "phone"),
        ("입금 계좌번호 110―123―456789", "bank_account"),
        ("주민번호 900101―1234567", "resident_registration_number"),
    ],
)
@pytest.mark.parametrize("normalize_first", [False, True], ids=["raw", "normalized"])
def test_horizontal_bar_ocr_candidates_are_detected(
    text: str,
    expected_kind: str,
    normalize_first: bool,
) -> None:
    """Catches U+2015 OCR punctuation bypassing high-risk detectors."""
    if normalize_first:
        text = normalize_text(text)
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert expected_kind in {finding.kind for finding in findings}


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("연락처\n010\n1234\n5678", "phone"),
        ("은행 계좌\n110\n123\n456789", "bank_account"),
        ("주민번호\n900101\n1234567", "resident_registration_number"),
        ("외국인등록번호\n900101\n5123456", "resident_registration_number"),
    ],
)
@pytest.mark.parametrize("normalize_first", [False, True], ids=["raw", "normalized"])
def test_labeled_bare_newline_candidates_are_detected(
    text: str,
    expected_kind: str,
    normalize_first: bool,
) -> None:
    """Catches a bounded labeled OCR layout hiding separated numeric groups."""
    if normalize_first:
        text = normalize_text(text)
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert expected_kind in {finding.kind for finding in findings}


@pytest.mark.parametrize(
    ("text", "unexpected_kind"),
    [
        ("분류 코드 010 2025 1234", "phone"),
        ("은행 2025 08 1234", "bank_account"),
        ("은행 2025―08―1234", "bank_account"),
    ],
)
@pytest.mark.parametrize("normalize_first", [False, True], ids=["raw", "normalized"])
def test_unrelated_numeric_groups_do_not_form_high_risk_findings(
    text: str,
    unexpected_kind: str,
    normalize_first: bool,
) -> None:
    """Catches ambiguous year and code groups being treated as contact data."""
    if normalize_first:
        text = normalize_text(text)
    findings = scan_text(text, location_id=_case_location("all-text"))

    assert unexpected_kind not in {finding.kind for finding in findings}


@pytest.mark.parametrize("normalize_first", [False, True], ids=["raw", "normalized"])
def test_unlabeled_bare_newline_rrn_is_not_detected(normalize_first: bool) -> None:
    """Catches generic line joining without explicit resident-number evidence."""
    text = "기준값\n900101\n1234567"
    if normalize_first:
        text = normalize_text(text)

    findings = scan_text(text, location_id=_case_location("all-text"))

    assert all(
        finding.kind != "resident_registration_number" for finding in findings
    )


def test_rrn_split_after_a_hyphen_is_detected() -> None:
    """Catches a single OCR line break bypassing resident-number detection."""
    findings = scan_text(
        "주민번호 900101-\n1234567",
        location_id=_case_location("all-text"),
    )

    assert [(finding.kind, finding.count) for finding in findings] == [
        ("resident_registration_number", 1)
    ]


def test_unrelated_numeric_lines_do_not_form_a_phone_number() -> None:
    """Catches independent OCR lines being joined into a false phone finding."""
    findings = scan_text(
        "분류 코드\n010\n2025\n1234\n종료",
        location_id=_case_location("all-text"),
    )

    assert all(finding.kind != "phone" for finding in findings)


@pytest.mark.parametrize("near_miss", ["010123456789", "170-1234-5678"])
def test_phone_near_misses_are_not_detected(near_miss: str) -> None:
    assert scan_text(near_miss, location_id=_case_location("answer")) == ()


def test_labeled_unseparated_account_is_detected_but_document_number_is_not() -> None:
    account = scan_text(
        "입금 계좌번호 110123456789",
        location_id=_case_location("answer"),
    )
    document_number = scan_text(
        "문서번호 서울교육-2025-08-123456",
        location_id=_case_location("answer"),
    )

    assert [(finding.kind, finding.count) for finding in account] == [
        ("bank_account", 1)
    ]
    assert all(finding.kind != "bank_account" for finding in document_number)


def test_identity_and_audit_quasi_identifiers_are_reported_separately() -> None:
    text = (
        "서울특별시교육청 주무관 홍길동. 감사일 2025. 7. 1., 금액 1,502,000원, "
        "직종 교사, 학교급 중학교, 대상 ○○학교 △△직원"
    )

    findings = scan_text(text, location_id="audit-7:facts", case_type="audit")

    assert [(finding.kind, finding.count) for finding in findings] == [
        ("name_organization_title", 1),
        ("audit_date", 1),
        ("audit_money", 1),
        ("audit_occupation", 1),
        ("audit_school_level", 1),
        ("anonymization_mark", 2),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "홍길동 서울특별시교육청 주무관",
        "성명: 홍길동, 소속: 서울특별시교육청, 직위: 주무관",
        "서울특별시교육청 교육연구사 홍길동",
    ],
)
def test_identity_triple_is_detected_in_common_field_orders(text: str) -> None:
    findings = scan_text(text, location_id=_case_location("credits"))

    assert [(finding.kind, finding.count) for finding in findings] == [
        ("name_organization_title", 1)
    ]


def test_korean_audit_date_is_detected_but_impossible_date_is_not() -> None:
    valid = scan_text(
        "감사일 2025년 7월 1일",
        location_id="audit-korean-date:facts",
        case_type="audit",
    )
    invalid = scan_text(
        "표기 오류 2025년 13월 40일",
        location_id="audit-invalid-date:facts",
        case_type="audit",
    )

    assert [(finding.kind, finding.count) for finding in valid] == [("audit_date", 1)]
    assert all(finding.kind != "audit_date" for finding in invalid)


def test_anonymization_surname_requires_safe_context() -> None:
    masked = scan_text(
        "대상자: 김모 교사",
        location_id="audit-masked:facts",
        case_type="audit",
    )
    ordinary_words = scan_text(
        "사업 규모와 학부모 안내",
        location_id="audit-ordinary:facts",
        case_type="audit",
    )

    assert [(finding.kind, finding.count) for finding in masked] == [
        ("anonymization_mark", 1)
    ]
    assert all(finding.kind != "anonymization_mark" for finding in ordinary_words)


def test_findings_are_aggregated_in_deterministic_policy_order() -> None:
    text = (
        "b@example.org / 010-1111-2222 / a@example.org / "
        "010-3333-4444 / 900101-1234567"
    )

    first = scan_text(text, location_id=_case_location("question"))
    second = scan_text(text, location_id=_case_location("question"))

    assert first == second
    assert [(item.kind, item.count) for item in first] == [
        ("resident_registration_number", 1),
        ("phone", 2),
        ("email", 2),
    ]


def test_scanner_handles_long_adversarial_input_without_emitting_input() -> None:
    adversarial = ("a." * 100_000) + "@"

    findings = scan_text(adversarial, location_id=_case_location("answer"))

    assert findings == ()
    assert adversarial not in repr(findings)


@pytest.mark.parametrize(
    ("prefix", "separator"),
    [
        ("계좌 번호 ", "- "),
        ("계좌 번호 ", "― "),
        ("연락처 010", "― "),
    ],
)
def test_bounded_ocr_separators_do_not_turn_long_near_miss_into_a_finding(
    prefix: str,
    separator: str,
) -> None:
    adversarial = prefix + (separator * 100_000) + "끝"

    assert scan_text(adversarial, location_id=_case_location("answer")) == ()


def test_ocr_spacing_does_not_classify_document_or_non_phone_numbers() -> None:
    near_miss = (
        "문서번호 서울교육-2025-08-123456, "
        "상담 번호 170 - 1234 - 5678, 사업 규모와 학부모 안내"
    )

    assert scan_text(near_miss, location_id=_case_location("answer")) == ()


@pytest.mark.parametrize(
    ("text", "case_type", "audit_masked", "expected_class"),
    [
        ("연락처 010-1234-5678", "qa", False, "restricted"),
        ("서울특별시교육청 주무관 홍길동", "qa", False, "restricted"),
        ("제작진 서울특별시교육청 주무관 홍길동", "credits", False, "public_credit"),
        ("대상 ○○학교 △△직원", "audit", True, "anonymized_case"),
        ("감사일 2025. 7. 1. 금액 1,502,000원", "audit", False, "quasi_identifier"),
        ("교육행정 일반 질의", "qa", False, "none"),
    ],
)
def test_privacy_decision_truth_table(
    text: str,
    case_type: CaseType,
    audit_masked: bool,
    expected_class: str,
) -> None:
    findings = scan_text(text, location_id=_case_location("all"), case_type=case_type)

    decision = classify_privacy(
        findings,
        case_type=case_type,
        audit_masked=audit_masked,
        proposed_search_eligible=True,
        proposed_answer_eligible=True,
    )

    assert decision.pii_class == expected_class
    if expected_class in {"restricted", "public_credit"}:
        assert decision.search_eligible is False
        assert decision.answer_eligible is False
    else:
        assert decision.search_eligible is True
        assert decision.answer_eligible is True
    assert decision.public_redistribution_approved is False


def test_high_risk_finding_overrides_credit_or_masking_classification() -> None:
    findings = scan_text(
        "제작진 연락처 010-1234-5678",
        location_id="credits-1:all",
        case_type="credits",
    )

    decision = classify_privacy(
        findings,
        case_type="credits",
        audit_masked=True,
        proposed_search_eligible=True,
        proposed_answer_eligible=True,
    )

    assert decision.pii_class == "restricted"
    assert (decision.search_eligible, decision.answer_eligible) == (False, False)


def test_single_unmasked_audit_field_is_not_combinable_by_itself() -> None:
    findings = scan_text(
        "감사일 2025. 7. 1.",
        location_id="audit-8:facts",
        case_type="audit",
    )

    decision = classify_privacy(
        findings,
        case_type="audit",
        audit_masked=False,
        proposed_search_eligible=False,
        proposed_answer_eligible=False,
    )

    assert decision.pii_class == "none"


def test_answer_eligibility_cannot_be_proposed_without_search_eligibility() -> None:
    with pytest.raises(ValueError, match="answer eligibility requires search eligibility") as error:
        classify_privacy(
            (),
            case_type="qa",
            proposed_search_eligible=False,
            proposed_answer_eligible=True,
        )

    assert "api" not in str(error.value).lower()


def test_classification_rejects_runtime_case_type_typo_before_eligibility() -> None:
    invalid_case_type = cast(CaseType, "qa-typo")

    with pytest.raises(ValueError, match="unsupported case type"):
        classify_privacy(
            (),
            case_type=invalid_case_type,
            proposed_search_eligible=True,
            proposed_answer_eligible=True,
        )


@pytest.mark.parametrize(
    "location_id",
    [
        "010-1234-5678",
        "case-010-1234-5678:answer",
        "case-line\nbreak:answer",
        f"case-{'a' * 32}:answer",
        "free-form location",
    ],
)
def test_scan_rejects_noncanonical_or_sensitive_location_without_echo(
    location_id: str,
) -> None:
    with pytest.raises(ValueError, match="canonical opaque structure") as error:
        scan_text("일반 본문", location_id=location_id)

    assert location_id not in str(error.value)


@pytest.mark.parametrize(
    "case_id",
    [
        make_case_id(2025, "학교회계 지출", "계약 일반", "1"),
        make_case_id(
            2025,
            "학교회계 지출",
            "계약 일반",
            "1",
            13,
            "2단계 입찰",
            duplicate=True,
        ),
    ],
)
def test_scan_accepts_long_and_duplicate_canonical_case_locations(case_id: str) -> None:
    location_id = f"case-{case_id}:answer"

    assert len(case_id) > 48
    assert scan_text("일반 본문", location_id=location_id) == ()
    assert PrivacyFinding(
        kind="phone",
        location_id=location_id,
        count=1,
    ).location_id == location_id


def test_case_location_rejects_structural_but_noncanonical_entity_without_echo() -> None:
    location_id = "case-structurally-safe-label:answer"

    with pytest.raises(ValueError, match="canonical opaque structure") as error:
        scan_text("일반 본문", location_id=location_id)

    assert location_id not in str(error.value)


def test_location_rejects_provider_token_and_compact_sensitive_numbers_without_echo() -> None:
    provider_token = _secret_shape(
        "sk",
        "-",
        "proj",
        "-",
        "abcdefghijklmnop",
        "123456",
    )
    sensitive_values = (
        provider_token,
        "110123456789",
        "01012345678",
        "900101-1234567",
        "a" * 32,
    )

    for sensitive_value in sensitive_values:
        for location_id in (
            f"case-{sensitive_value}:answer",
            f"doc-safe-{sensitive_value}:answer",
            f"case-{_CANONICAL_CASE_ID}:{sensitive_value}",
        ):
            with pytest.raises(
                ValueError,
                match="canonical opaque structure",
            ) as error:
                scan_text("일반 본문", location_id=location_id)

            assert sensitive_value not in str(error.value)


def test_finding_boundary_rejects_sensitive_location_without_echo() -> None:
    sensitive_location = "case-010-1234-5678:answer"

    with pytest.raises(ValidationError) as error:
        PrivacyFinding(
            kind="phone",
            location_id=sensitive_location,
            count=1,
        )

    assert sensitive_location not in str(error.value)
    assert "010-1234-5678" not in repr(error.value)


def test_privacy_models_reject_extra_fields_that_could_leak_values() -> None:
    with pytest.raises(ValueError) as error:
        PrivacyFinding.model_validate(
            {
                "kind": "phone",
                "location_id": _case_location("all"),
                "count": 1,
                "detected_value": "010-1234-5678",
            }
        )

    assert "010-1234-5678" not in str(error.value)
