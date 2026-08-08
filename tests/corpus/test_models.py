"""Behavioral contracts for canonical corpus records and their schemas."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from src.cli import app
from src.corpus.models import (
    Case,
    CaseRelation,
    Chunk,
    Document,
    IngestionRun,
    LawRef,
    SourceSpan,
)


def _span() -> dict[str, object]:
    return {
        "pdf_page_index": 13,
        "page_label": "13",
        "bbox": [126.0, 341.0, 1064.0, 1498.0],
        "text_sha256": "a" * 64,
    }


@pytest.fixture
def case_payload() -> dict[str, object]:
    return {
        "case_id": "senqa-2025-contract-contract-general-1",
        "legacy_ids": ["CT-001"],
        "doc_id": "sen-qa-2025-v1",
        "case_type": "qa",
        "domain": "계약",
        "part": "계약 일반",
        "subtopic": "계약방법 및 체결",
        "case_no": "1",
        "title_raw": "2단계 입찰",
        "title_normalized": "2단계 입찰",
        "question": "제안서 평가위원회 구성에 관한 근거 문의",
        "answer": "정규화된 답변 본문",
        "facts": None,
        "basis_text": "근거와 참고자료 본문",
        "law_ref_ids": ["lawref-2025-000001"],
        "source_spans": [_span()],
        "extraction_source": "ocr",
        "extraction_confidence": 0.98,
        "critical_field_review": "verified",
        "pii_class": "none",
        "anonymization_status": "not_required",
        "currency_status": "historical_reference",
        "search_eligible": True,
        "answer_eligible": True,
        "review_status": "approved",
    }


@pytest.mark.parametrize(
    ("review_status", "pii_class", "search_eligible", "answer_eligible", "valid"),
    [
        ("machine_extracted", "none", False, False, True),
        ("machine_extracted", "none", True, False, False),
        ("needs_review", "anonymized_case", False, False, True),
        ("needs_review", "anonymized_case", False, True, False),
        ("rejected", "quasi_identifier", False, False, True),
        ("rejected", "quasi_identifier", True, True, False),
        ("search_approved", "none", True, False, True),
        ("search_approved", "none", True, True, False),
        ("search_approved", "none", False, False, False),
        ("approved", "none", True, False, True),
        ("approved", "anonymized_case", True, True, True),
        ("approved", "quasi_identifier", True, True, True),
        ("approved", "none", False, False, False),
        ("approved", "public_credit", False, False, True),
        ("approved", "public_credit", True, False, False),
        ("search_approved", "public_credit", False, False, True),
        ("search_approved", "public_credit", True, False, False),
        ("machine_extracted", "restricted", False, False, True),
        ("machine_extracted", "restricted", False, True, False),
    ],
)
def test_case_eligibility_truth_table(
    case_payload: dict[str, object],
    review_status: str,
    pii_class: str,
    search_eligible: bool,
    answer_eligible: bool,
    valid: bool,
) -> None:
    """Catches any review or PII branch that leaks a case into an index."""
    case_payload.update(
        review_status=review_status,
        pii_class=pii_class,
        search_eligible=search_eligible,
        answer_eligible=answer_eligible,
    )

    if valid:
        case = Case.model_validate(case_payload)
        assert (case.search_eligible, case.answer_eligible) == (
            search_eligible,
            answer_eligible,
        )
    else:
        with pytest.raises(ValidationError, match="eligibility|public_credit|restricted"):
            Case.model_validate(case_payload)


@pytest.mark.parametrize("field", ["unknown", "answer_eligible"])
def test_case_forbids_unknown_fields(case_payload: dict[str, object], field: str) -> None:
    """Catches unreviewed fields silently entering canonical case records."""
    if field == "answer_eligible":
        case_payload["unknown"] = True
    else:
        case_payload[field] = True
    with pytest.raises(ValidationError):
        Case.model_validate(case_payload)


@pytest.mark.parametrize(
    ("span_update", "message"),
    [
        ({"pdf_page_index": 0}, "greater than or equal to 1"),
        ({"text_sha256": "A" * 64}, "pattern"),
        ({"bbox": [0.0, 1.0, 0.0, 2.0]}, "ordered"),
        ({"bbox": [0.0, math.nan, 1.0, 2.0]}, "finite"),
    ],
)
def test_source_span_rejects_invalid_citation_geometry(
    span_update: dict[str, object], message: str
) -> None:
    """Catches invalid page hashes or geometry being used as a citation."""
    payload = _span()
    payload.update(span_update)
    with pytest.raises(ValidationError, match=message):
        SourceSpan.model_validate(payload)


def test_source_span_requires_exactly_four_coordinates() -> None:
    """Catches incomplete citation rectangles that cannot locate source text."""
    payload = _span()
    payload["bbox"] = [0.0, 1.0, 2.0]
    with pytest.raises(ValidationError):
        SourceSpan.model_validate(payload)


def test_document_rejects_reversed_source_period() -> None:
    """Catches a document recording an impossible source coverage period."""
    with pytest.raises(ValidationError, match="source period end"):
        Document.model_validate(
            {
                "doc_id": "sen-qa-2025-v1",
                "edition_year": 2025,
                "title": "2025년 교육행정지원시스템 질문·답변 사례집",
                "publisher": "서울특별시교육청",
                "registration_no": "서울교육 2025-109",
                "source_period_start": date(2025, 7, 1),
                "source_period_end": date(2025, 6, 30),
                "source_filename": "2025-questions-answers.pdf",
                "sha256": "b" * 64,
                "pdf_page_count": 314,
                "extraction_method": "ocr",
                "source_dpi": 300,
                "public_url": None,
                "redistribution_status": "unverified",
                "access_level": "staff",
                "page_numbering_rule": "body_same_as_pdf",
                "ingestion_version": "corpus-v1",
            }
        )


def test_chunk_requires_parent_case_eligibility_shape() -> None:
    """Catches an answer chunk that is eligible while its search entry is not."""
    with pytest.raises(ValidationError, match="answer eligibility requires search eligibility"):
        Chunk.model_validate(
            {
                "chunk_id": "senqa-2025-contract-contract-general-1-answer-01",
                "case_id": "senqa-2025-contract-contract-general-1",
                "role": "answer",
                "sequence": 1,
                "text": "답변",
                "embedding_text": "답변",
                "source_span_indexes": [0],
                "token_count": 1,
                "quality_flags": [],
                "search_eligible": False,
                "answer_eligible": True,
            }
        )


@pytest.mark.parametrize("indexes", [[-1], [0, 0]])
def test_chunk_rejects_impossible_local_span_indexes(indexes: list[int]) -> None:
    """Catches a chunk pointing at a negative or duplicate parent source span."""
    payload = {
        "chunk_id": "senqa-2025-contract-contract-general-1-question-01",
        "case_id": "senqa-2025-contract-contract-general-1",
        "role": "question",
        "sequence": 1,
        "text": "질문",
        "embedding_text": "질문",
        "source_span_indexes": indexes,
        "token_count": 1,
        "quality_flags": [],
        "search_eligible": True,
        "answer_eligible": False,
    }
    with pytest.raises(ValidationError, match="source span indexes"):
        Chunk.model_validate(payload)


def test_relation_rejects_a_case_relating_to_itself() -> None:
    """Catches self-relations that make cross-case graph traversal meaningless."""
    with pytest.raises(ValidationError, match="different"):
        CaseRelation.model_validate(
            {
                "relation_id": "rel-1",
                "source_case_id": "senqa-2025-contract-contract-general-1",
                "target_case_id": "senqa-2025-contract-contract-general-1",
                "relation_type": "related",
                "confidence": 0.9,
                "review_status": "approved",
            }
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidences_are_bounded(confidence: float) -> None:
    """Catches confidence values that cannot be interpreted as probabilities."""
    payload = {
        "law_ref_id": "lawref-2025-000001",
        "case_id": "senqa-2025-contract-contract-general-1",
        "display_name": "지방계약법",
        "abbreviation": None,
        "article": "제13조",
        "paragraph": None,
        "item": None,
        "cited_effective_date": None,
        "quote": "관련 규정",
        "source_span": _span(),
        "parsing_confidence": confidence,
        "review_status": "needs_review",
    }
    with pytest.raises(ValidationError):
        LawRef.model_validate(payload)


def test_ingestion_run_rejects_non_utc_or_reversed_times() -> None:
    """Catches release audit records with ambiguous or backwards timestamps."""
    base = {
        "run_id": "run-20250808-001",
        "release_id": "corpus-20250808120000-deadbeef",
        "started_at": datetime(2025, 8, 8, 12, 0, tzinfo=UTC),
        "ended_at": datetime(2025, 8, 8, 12, 1, tzinfo=UTC),
        "manifest_version": "manifest-v1",
        "source_sha256s": ["c" * 64],
        "extractor_version": "extract-v1",
        "ocr_engine_version": "paddle-1",
        "ocr_model_version": "korean-v1",
        "container_image": "sha256:" + "d" * 64,
        "normalizer_version": "normalizer-v1",
        "parser_version": "parser-v1",
        "schema_version": "schema-v1",
        "document_page_counts": {"sen-qa-2025-v1": {"succeeded": 314, "quarantined": 0, "failed": 0}},
        "created_case_ids": [],
        "changed_case_ids": [],
        "deleted_case_ids": [],
        "quality_metrics": {"coverage": 1.0},
        "approved_by": None,
    }
    non_utc = dict(base, started_at=datetime(2025, 8, 8, 21, 0, tzinfo=timezone(timedelta(hours=9))))
    with pytest.raises(ValidationError, match="UTC"):
        IngestionRun.model_validate(non_utc)
    reversed_times = dict(base, ended_at=datetime(2025, 8, 8, 11, 59, tzinfo=UTC))
    with pytest.raises(ValidationError, match="must not precede"):
        IngestionRun.model_validate(reversed_times)


def test_exported_schemas_are_deterministic_and_validate_payloads(
    tmp_path: Path, case_payload: dict[str, object]
) -> None:
    """Catches schema drift or a JSON-schema contract that differs from Pydantic."""
    output = tmp_path / "schemas"
    runner = CliRunner()
    assert runner.invoke(app, ["export-schemas", "--output", str(output)]).exit_code == 0
    first = {path.name: path.read_bytes() for path in sorted(output.glob("*.schema.json"))}
    assert runner.invoke(app, ["export-schemas", "--output", str(output)]).exit_code == 0
    assert {path.name: path.read_bytes() for path in sorted(output.glob("*.schema.json"))} == first

    expected = {
        "document.schema.json",
        "case.schema.json",
        "chunk.schema.json",
        "law-ref.schema.json",
        "case-relation.schema.json",
    }
    assert set(first) == expected
    for name, contents in first.items():
        schema = json.loads(contents)
        assert schema["$id"] == f"data/schemas/{name}"
        jsonschema.Draft202012Validator.check_schema(schema)

    case_schema = json.loads(first["case.schema.json"])
    valid = Case.model_validate(case_payload).model_dump(mode="json")
    jsonschema.Draft202012Validator(case_schema).validate(valid)
    invalid = dict(valid, unknown=True)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(case_schema).validate(invalid)


def test_export_schemas_refuses_to_overwrite_unmanaged_schema(tmp_path: Path) -> None:
    """Catches export deleting a schema that is not owned by this command."""
    output = tmp_path / "schemas"
    output.mkdir()
    foreign = output / "foreign.schema.json"
    foreign.write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["export-schemas", "--output", str(output)])
    assert result.exit_code == 2
    assert "unexpected schema files" in result.output
    assert foreign.read_text(encoding="utf-8") == "{}\n"


def test_committed_schemas_are_a_byte_for_byte_clean_export(tmp_path: Path) -> None:
    """Catches a committed schema changing independently from the model contract."""
    output = tmp_path / "schemas"
    result = CliRunner().invoke(app, ["export-schemas", "--output", str(output)])
    assert result.exit_code == 0
    committed = Path("data/schemas")
    assert {path.name: path.read_bytes() for path in sorted(committed.glob("*.schema.json"))} == {
        path.name: path.read_bytes() for path in sorted(output.glob("*.schema.json"))
    }


def test_export_overwrites_stale_managed_schema(tmp_path: Path) -> None:
    """Catches stale generated schema bytes surviving an approved model export."""
    output = tmp_path / "schemas"
    output.mkdir()
    stale = output / "case.schema.json"
    stale.write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["export-schemas", "--output", str(output)])
    assert result.exit_code == 0
    assert json.loads(stale.read_text(encoding="utf-8"))["$id"] == "data/schemas/case.schema.json"
