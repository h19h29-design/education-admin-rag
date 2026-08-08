from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.ingestion.normalize import (
    Correction,
    LexicalWrapEvidence,
    RepeatedLineEvidence,
    TextLayers,
    normalize_text,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_normalization_preserves_critical_entities() -> None:
    raw = (
        "금 1,502,000원 · 37.5% · 2025. 7. 1. · 제12조제3항\n"
        "서울교육-2025-001 · 결론: 가능·불가 · ○○학교 △△직원 ***"
    )

    normalized = normalize_text(raw)

    for critical_value in (
        "1,502,000원",
        "37.5%",
        "2025. 7. 1.",
        "제12조제3항",
        "서울교육-2025-001",
        "가능·불가",
        "○○학교",
        "△△직원",
        "***",
    ):
        assert critical_value in normalized


def test_normalization_is_nfc_idempotent_and_conservative() -> None:
    decomposed = "Cafe\u0301"
    raw = (
        f"  {decomposed}\t질문･답변 ・ 근거\r\nadministra-\n"
        "tion\r\n행정-\n지원  \r\n\r\n\r\n끝  "
    )
    lexical_wrap = LexicalWrapEvidence(
        raw_text_sha256=_digest(raw),
        line_index=1,
        left_fragment="administra",
        right_fragment="tion",
    )

    once = normalize_text(raw, lexical_wraps=(lexical_wrap,))
    twice = normalize_text(once, lexical_wraps=(lexical_wrap,))

    assert once == "Café 질문·답변 · 근거\nadministration\n행정-\n지원\n\n끝"
    assert twice == once


def test_ascii_hyphenated_lines_require_explicit_instance_evidence() -> None:
    raw = "administrative-\nprocess"

    assert normalize_text(raw) == raw


def test_soft_hyphen_is_intrinsic_lexical_wrap_evidence() -> None:
    assert normalize_text("administra\u00ad\ntion") == "administration"


def test_lexical_wrap_evidence_rejects_numeric_or_unbound_fragments() -> None:
    with pytest.raises(ValidationError):
        LexicalWrapEvidence(
            raw_text_sha256="0" * 64,
            line_index=0,
            left_fragment="2025",
            right_fragment="001",
        )

    raw = "administra-\ntion"
    wrong_page = LexicalWrapEvidence(
        raw_text_sha256="0" * 64,
        line_index=0,
        left_fragment="administra",
        right_fragment="tion",
    )
    assert normalize_text(raw, lexical_wraps=(wrong_page,)) == raw


def test_text_layers_accepts_proven_lexical_wrap_evidence() -> None:
    raw = "administra-\ntion"
    evidence = LexicalWrapEvidence(
        raw_text_sha256=_digest(raw),
        line_index=0,
        left_fragment="administra",
        right_fragment="tion",
    )

    layers = TextLayers.from_raw(raw, lexical_wraps=(evidence,))

    assert layers.raw_text == raw
    assert layers.normalized_text == "administration"
    assert layers.corrected_text == "administration"


@pytest.mark.parametrize(
    "protected",
    [
        "서울교육-2025-\n001",
        "시행일 2025-\n08-01",
        "제12조-\n제3항",
        "금액 범위 1,000-\n2,000원",
    ],
)
def test_line_end_hyphen_preserves_protected_identifiers_and_ranges(
    protected: str,
) -> None:
    assert normalize_text(protected) == protected


def test_repeated_lines_require_frequency_and_margin_coordinates() -> None:
    raw = "교육행정지원시스템 질문답변 사례집\n본문 질문\n- 17 -"
    header = RepeatedLineEvidence(
        text="교육행정지원시스템 질문답변 사례집",
        document_page_count=100,
        page_occurrence_count=80,
        y0_fraction=0.01,
        y1_fraction=0.05,
        raw_text_sha256=_digest(raw),
    )
    footer = RepeatedLineEvidence(
        text="- 17 -",
        document_page_count=100,
        page_occurrence_count=80,
        y0_fraction=0.94,
        y1_fraction=0.98,
        raw_text_sha256=_digest(raw),
    )

    assert normalize_text(raw) == raw
    assert normalize_text(raw, repeated_lines=(header, footer)) == "본문 질문"


@pytest.mark.parametrize(
    ("page_occurrence_count", "y0_fraction", "y1_fraction"),
    [
        (50, 0.01, 0.05),
        (90, 0.40, 0.50),
    ],
)
def test_repeated_line_is_retained_when_either_evidence_factor_is_missing(
    page_occurrence_count: int,
    y0_fraction: float,
    y1_fraction: float,
) -> None:
    raw = "공통 문구\n사례 본문"
    evidence = RepeatedLineEvidence(
        text="공통 문구",
        document_page_count=100,
        page_occurrence_count=page_occurrence_count,
        y0_fraction=y0_fraction,
        y1_fraction=y1_fraction,
        raw_text_sha256=_digest(raw),
    )

    assert normalize_text(raw, repeated_lines=(evidence,)) == raw


def test_duplicate_body_text_is_not_removed_without_instance_mapping() -> None:
    raw = "공통 문구\n사례 본문\n공통 문구"
    evidence = RepeatedLineEvidence(
        text="공통 문구",
        document_page_count=10,
        page_occurrence_count=9,
        y0_fraction=0.01,
        y1_fraction=0.05,
        raw_text_sha256=_digest(raw),
    )

    assert normalize_text(raw, repeated_lines=(evidence,)) == raw


def test_line_index_maps_margin_evidence_without_deleting_same_body_text() -> None:
    raw = "공통 문구\n사례 본문\n공통 문구"
    evidence = RepeatedLineEvidence(
        text="공통 문구",
        document_page_count=10,
        page_occurrence_count=9,
        y0_fraction=0.01,
        y1_fraction=0.05,
        line_index=0,
        raw_text_sha256=_digest(raw),
    )

    assert normalize_text(raw, repeated_lines=(evidence,)) == "사례 본문\n공통 문구"


def test_repeated_line_evidence_is_bound_to_raw_page_and_idempotent() -> None:
    raw = "공통 문구\n사례 본문\n공통 문구"
    evidence = RepeatedLineEvidence(
        text="공통 문구",
        document_page_count=10,
        page_occurrence_count=9,
        y0_fraction=0.01,
        y1_fraction=0.05,
        line_index=0,
        raw_text_sha256=_digest(raw),
    )

    once = normalize_text(raw, repeated_lines=(evidence,))
    twice = normalize_text(once, repeated_lines=(evidence,))

    assert once == "사례 본문\n공통 문구"
    assert twice == once


def test_repeated_line_evidence_with_wrong_raw_hash_never_removes_text() -> None:
    raw = "공통 문구\n사례 본문"
    evidence = RepeatedLineEvidence(
        text="공통 문구",
        document_page_count=10,
        page_occurrence_count=9,
        y0_fraction=0.01,
        y1_fraction=0.05,
        raw_text_sha256="0" * 64,
    )

    assert normalize_text(raw, repeated_lines=(evidence,)) == raw


def test_repeated_line_evidence_requires_two_occurrences() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        RepeatedLineEvidence(
            text="공통 문구",
            document_page_count=10,
            page_occurrence_count=1,
            y0_fraction=0.01,
            y1_fraction=0.05,
            raw_text_sha256="0" * 64,
        )


def test_text_layers_and_correction_log_are_immutable() -> None:
    layers = TextLayers.from_raw("질문･답변\r\n금 100원")
    corrected_at = datetime(2026, 8, 8, 5, 30, tzinfo=UTC)
    before_hash = _digest(layers.corrected_text)
    after_text = "질문·답변\n금 1,000원"
    after_hash = _digest(after_text)

    corrected = layers.with_correction(
        after_text,
        reviewer_id="reviewer-a",
        corrected_at=corrected_at,
        reason_code="verify_amount_against_source",
        expected_before_sha256=before_hash,
        expected_after_sha256=after_hash,
    )

    assert layers.raw_text == "질문･답변\r\n금 100원"
    assert layers.normalized_text == "질문·답변\n금 100원"
    assert layers.corrected_text == layers.normalized_text
    assert layers.corrections == ()
    assert corrected.raw_text == layers.raw_text
    assert corrected.normalized_text == layers.normalized_text
    assert corrected.corrected_text == after_text
    assert corrected.corrections == (
        Correction(
            reviewer_id="reviewer-a",
            corrected_at=corrected_at,
            reason_code="verify_amount_against_source",
            before_sha256=before_hash,
            after_sha256=after_hash,
        ),
    )
    with pytest.raises(ValidationError):
        corrected.corrected_text = "mutation"


def test_correction_rejects_noop_non_utc_and_invalid_hash_boundaries() -> None:
    layers = TextLayers.from_raw("원문")
    now = datetime(2026, 8, 8, 5, 30, tzinfo=UTC)

    with pytest.raises(ValueError, match="change text"):
        layers.with_correction(
            "원문",
            reviewer_id="reviewer-a",
            corrected_at=now,
            reason_code="source_check",
        )
    with pytest.raises(ValueError, match="explicit UTC"):
        layers.with_correction(
            "교정",
            reviewer_id="reviewer-a",
            corrected_at=datetime(2026, 8, 8, 5, 30),  # noqa: DTZ001 - rejection fixture
            reason_code="source_check",
        )
    with pytest.raises(ValueError, match="explicit UTC"):
        layers.with_correction(
            "교정",
            reviewer_id="reviewer-a",
            corrected_at=datetime(
                2026,
                8,
                8,
                14,
                30,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            reason_code="source_check",
        )
    with pytest.raises(ValueError, match="before SHA-256 boundary"):
        layers.with_correction(
            "교정",
            reviewer_id="reviewer-a",
            corrected_at=now,
            reason_code="source_check",
            expected_before_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="after SHA-256 boundary"):
        layers.with_correction(
            "교정",
            reviewer_id="reviewer-a",
            corrected_at=now,
            reason_code="source_check",
            expected_after_sha256="not-a-sha",
        )


def test_correction_model_rejects_equal_or_malformed_hashes() -> None:
    valid_hash = "a" * 64
    payload = {
        "reviewer_id": "reviewer-a",
        "corrected_at": datetime(2026, 8, 8, 5, 30, tzinfo=UTC),
        "reason_code": "source_check",
        "before_sha256": valid_hash,
        "after_sha256": valid_hash,
    }

    with pytest.raises(ValidationError, match="different SHA-256"):
        Correction.model_validate(payload)
    with pytest.raises(ValidationError, match="String should match pattern"):
        Correction.model_validate({**payload, "after_sha256": "A" * 64})


@pytest.mark.parametrize(
    ("reviewer_id", "reason_code"),
    [
        ("reviewer-a\nspoof", "source_check"),
        ("reviewer-a", "source_check\nspoof"),
        ("r" * 65, "source_check"),
        ("reviewer-a", "r" * 65),
    ],
)
def test_correction_rejects_noncanonical_or_unbounded_audit_fields(
    reviewer_id: str,
    reason_code: str,
) -> None:
    with pytest.raises(ValidationError):
        Correction(
            reviewer_id=reviewer_id,
            corrected_at=datetime(2026, 8, 8, 5, 30, tzinfo=UTC),
            reason_code=reason_code,
            before_sha256="a" * 64,
            after_sha256="b" * 64,
        )


@pytest.mark.parametrize("minute", [29, 30])
def test_correction_timestamps_must_increase_without_backdating(minute: int) -> None:
    first = TextLayers.from_raw("원문").with_correction(
        "첫 교정",
        reviewer_id="reviewer-a",
        corrected_at=datetime(2026, 8, 8, 5, 30, tzinfo=UTC),
        reason_code="source_check",
    )

    with pytest.raises(ValueError, match="strictly increase"):
        first.with_correction(
            "둘째 교정",
            reviewer_id="reviewer-b",
            corrected_at=datetime(2026, 8, 8, 5, minute, tzinfo=UTC),
            reason_code="second_source_check",
        )


def test_later_correction_timestamp_extends_hash_chain() -> None:
    first = TextLayers.from_raw("원문").with_correction(
        "첫 교정",
        reviewer_id="reviewer-a",
        corrected_at=datetime(2026, 8, 8, 5, 30, tzinfo=UTC),
        reason_code="source_check",
    )

    second = first.with_correction(
        "둘째 교정",
        reviewer_id="reviewer-b",
        corrected_at=datetime(2026, 8, 8, 5, 31, tzinfo=UTC),
        reason_code="second_source_check",
    )

    assert len(second.corrections) == 2
    assert second.corrections[0].after_sha256 == second.corrections[1].before_sha256
