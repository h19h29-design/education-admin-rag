"""Fail-closed corpus quality assessment and privacy-safe review queues."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.corpus.ids import make_case_id, validate_case_id
from src.corpus.models import Case, Document
from src.ingestion.policy import OCR_LOW_CONFIDENCE_THRESHOLD

OcrLayoutReview: TypeAlias = Literal[
    "not_applicable", "unreviewed", "sample_passed", "segment_verified", "error_found"
]
QualityReason: TypeAlias = Literal[
    "credits-excluded",
    "critical-fields-unverified",
    "extraction-method-mismatch",
    "law-reference-invalid",
    "low-confidence",
    "native-confidence-not-exact",
    "ocr-layout-error",
    "ocr-layout-review-missing",
    "page-out-of-range",
    "public-credit-excluded",
    "required-field-missing",
    "restricted-pii",
    "review-state-not-candidate",
    "source-document-mismatch",
    "source-text-hash-mismatch",
    "source-text-missing",
    "source-text-unexpected",
    "unsupported-edition-year",
    "year-extraction-policy-mismatch",
]

_RELEASE_ID_RE = re.compile(
    r"^corpus-(?P<timestamp>[0-9]{14})-(?P<git_sha>[0-9a-f]{8})$"
)
_OCR_LAYOUT_REVIEWS: frozenset[str] = frozenset(
    {"not_applicable", "unreviewed", "sample_passed", "segment_verified", "error_found"}
)
_PAGE_BOUND_REASONS: frozenset[QualityReason] = frozenset(
    {"page-out-of-range", "source-text-hash-mismatch", "source-text-missing"}
)


class QualityGateError(Exception):
    """A sanitized quality-gate or review-queue boundary failure."""


def _validate_case_id_shape(value: str) -> str:
    try:
        return validate_case_id(value)
    except ValueError as error:
        raise ValueError("case identifier is invalid") from error


def _finding_sort_key(
    finding_key: tuple[int | None, QualityReason],
) -> tuple[bool, int, str]:
    page_id, reason_code = finding_key
    return (page_id is not None, page_id or 0, reason_code)


class QualityFinding(BaseModel):
    """One aggregate finding with no source text or detected value."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    reason_code: QualityReason
    case_id: str
    page_id: int | None = Field(default=None, ge=1)
    count: int = Field(ge=1)

    @model_validator(mode="after")
    def has_safe_case_id(self) -> QualityFinding:
        _validate_case_id_shape(self.case_id)
        return self


class QualityAssessment(BaseModel):
    """Automated result that always routes a candidate through human review."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    case_id: str
    page_ids: tuple[int, ...]
    findings: tuple[QualityFinding, ...]
    automated_quality_passed: bool
    target_review_status: Literal["needs_review"] = "needs_review"
    search_eligible: Literal[False] = False
    answer_eligible: Literal[False] = False
    public_redistribution_candidate: Literal[False] = False

    @model_validator(mode="after")
    def is_consistent_and_fail_closed(self) -> QualityAssessment:
        _validate_case_id_shape(self.case_id)
        if not self.page_ids:
            raise ValueError("quality assessment requires at least one page")
        if tuple(sorted(set(self.page_ids))) != self.page_ids or any(
            page < 1 for page in self.page_ids
        ):
            raise ValueError("page identifiers must be unique, positive, and sorted")
        page_ids = set(self.page_ids)
        finding_keys = tuple(
            (finding.page_id, finding.reason_code) for finding in self.findings
        )
        if len(set(finding_keys)) != len(finding_keys):
            raise ValueError("finding keys must be unique")
        if finding_keys != tuple(sorted(finding_keys, key=_finding_sort_key)):
            raise ValueError("finding keys must be canonically sorted")
        if any(finding.case_id != self.case_id for finding in self.findings):
            raise ValueError("finding case must match assessment case")
        if any(
            finding.reason_code in _PAGE_BOUND_REASONS and finding.page_id is None
            for finding in self.findings
        ):
            raise ValueError("finding page must belong to assessment pages")
        if any(
            finding.page_id is not None and finding.page_id not in page_ids
            for finding in self.findings
        ):
            raise ValueError("finding page must belong to assessment pages")
        if self.automated_quality_passed != (not self.findings):
            raise ValueError("automated quality result does not match findings")
        return self


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _required_field_count(case: Case) -> int:
    if case.case_type == "qa":
        return sum(_is_blank(value) for value in (case.question, case.answer))
    if case.case_type == "audit":
        return sum(_is_blank(value) for value in (case.facts, case.answer))
    if case.case_type == "law_index":
        has_missing_reference = not case.law_ref_ids or any(
            _is_blank(reference) for reference in case.law_ref_ids
        )
        return int(_is_blank(case.basis_text)) + int(has_missing_reference)
    return 0


def _validate_case_identity(case: Case, document: Document) -> None:
    try:
        regular_id = make_case_id(
            document.edition_year,
            case.domain,
            case.part,
            case.case_no,
        )
        first_page = case.source_spans[0].pdf_page_index
        duplicate_id = make_case_id(
            document.edition_year,
            case.domain,
            case.part,
            case.case_no,
            first_page,
            case.title_raw,
            duplicate=True,
        )
    except (IndexError, ValueError) as error:
        raise QualityGateError("case identifier is invalid") from error
    if case.case_id not in {regular_id, duplicate_id}:
        raise QualityGateError("case identifier is invalid")


def assess_case(
    case: Case,
    document: Document,
    source_text_by_span: Mapping[int, str],
    *,
    ocr_layout_review: OcrLayoutReview,
) -> QualityAssessment:
    """Assess one candidate without copying source text into the result."""
    _validate_case_identity(case, document)
    if ocr_layout_review not in _OCR_LAYOUT_REVIEWS:
        raise QualityGateError("OCR layout review policy is invalid")

    finding_counts: dict[tuple[int | None, QualityReason], int] = {}

    def add(reason: QualityReason, page_id: int | None = None, count: int = 1) -> None:
        key = (page_id, reason)
        finding_counts[key] = finding_counts.get(key, 0) + count

    required_missing = _required_field_count(case)
    if required_missing:
        add("required-field-missing", count=required_missing)
    if case.case_type == "law_index" and len(case.law_ref_ids) != len(
        set(case.law_ref_ids)
    ):
        add("law-reference-invalid")

    if case.case_type == "credits":
        add("credits-excluded")
    if case.pii_class == "restricted":
        add("restricted-pii")
    elif case.pii_class == "public_credit":
        add("public-credit-excluded")

    if case.doc_id != document.doc_id:
        add("source-document-mismatch")
    if case.extraction_source != document.extraction_method:
        add("extraction-method-mismatch")

    expected_method: Literal["native", "ocr"] | None
    if document.edition_year in {2020, 2021, 2022}:
        expected_method = "native"
    elif document.edition_year in {2023, 2024, 2025}:
        expected_method = "ocr"
    else:
        expected_method = None
        add("unsupported-edition-year")
    if expected_method is not None and (
        document.extraction_method != expected_method
        or case.extraction_source != expected_method
    ):
        add("year-extraction-policy-mismatch")

    if case.extraction_source == "native":
        if case.extraction_confidence != 1.0:
            add("native-confidence-not-exact")
    elif case.extraction_confidence < OCR_LOW_CONFIDENCE_THRESHOLD:
        add("low-confidence")
    if case.review_status not in {"machine_extracted", "needs_review"}:
        add("review-state-not-candidate")

    expected_span_indexes = set(range(len(case.source_spans)))
    unexpected_span_count = len(set(source_text_by_span) - expected_span_indexes)
    if unexpected_span_count:
        add("source-text-unexpected", count=unexpected_span_count)

    for span_index, span in enumerate(case.source_spans):
        if span.pdf_page_index > document.pdf_page_count:
            add("page-out-of-range", span.pdf_page_index)
        source_text = source_text_by_span.get(span_index)
        if not isinstance(source_text, str):
            add("source-text-missing", span.pdf_page_index)
        elif sha256(source_text.encode("utf-8")).hexdigest() != span.text_sha256:
            add("source-text-hash-mismatch", span.pdf_page_index)

    if case.extraction_source == "ocr" and document.edition_year in {2023, 2024, 2025}:
        if document.edition_year in {2023, 2024}:
            if case.critical_field_review != "verified":
                add("critical-fields-unverified")
        elif document.edition_year == 2025:
            if ocr_layout_review in {"not_applicable", "unreviewed"}:
                add("ocr-layout-review-missing")
            elif ocr_layout_review == "error_found":
                add("ocr-layout-error")

    findings = tuple(
        QualityFinding(
            reason_code=reason,
            case_id=case.case_id,
            page_id=page_id,
            count=count,
        )
        for (page_id, reason), count in sorted(
            finding_counts.items(),
            key=lambda item: _finding_sort_key(item[0]),
        )
    )
    page_ids = tuple(sorted({span.pdf_page_index for span in case.source_spans}))
    return QualityAssessment(
        case_id=case.case_id,
        page_ids=page_ids,
        findings=findings,
        automated_quality_passed=not findings,
    )


def _review_queue_directory(artifact_root: Path) -> Path:
    try:
        absolute_root = artifact_root.absolute()
        current = Path(absolute_root.anchor)
        for part in absolute_root.parts[1:]:
            current /= part
            if current.is_symlink():
                raise QualityGateError("review queue storage path is unsafe")
        resolved_root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise QualityGateError("review queue storage path is unavailable") from error
    if not resolved_root.is_dir() or resolved_root == Path(resolved_root.anchor):
        raise QualityGateError("review queue storage path is unsafe")

    queue_directory = resolved_root / "review-queue"
    try:
        if queue_directory.exists() or queue_directory.is_symlink():
            if queue_directory.is_symlink() or not queue_directory.is_dir():
                raise QualityGateError("review queue storage path is unsafe")
        else:
            queue_directory.mkdir(mode=0o750)
        os.chmod(queue_directory, 0o750)
    except OSError as error:
        raise QualityGateError("review queue storage path is unavailable") from error
    return queue_directory


def _declared_field_mapping(
    value: BaseModel,
    expected_fields: Mapping[str, object],
) -> dict[str, object] | None:
    raw_fields = object.__getattribute__(value, "__dict__")
    if type(raw_fields) is not dict or set(raw_fields) != set(expected_fields):
        return None
    return {field_name: raw_fields[field_name] for field_name in expected_fields}


def _revalidate_queue_assessment(supplied: object) -> QualityAssessment:
    if type(supplied) is not QualityAssessment:
        raise QualityGateError("review queue assessment is invalid")
    assessment_fields = _declared_field_mapping(
        supplied,
        QualityAssessment.model_fields,
    )
    if assessment_fields is None:
        raise QualityGateError("review queue assessment is invalid")

    supplied_findings = assessment_fields["findings"]
    if type(supplied_findings) is not tuple:
        raise QualityGateError("review queue assessment is invalid")
    finding_fields: list[dict[str, object]] = []
    for supplied_finding in supplied_findings:
        if type(supplied_finding) is not QualityFinding:
            raise QualityGateError("review queue assessment is invalid")
        fields = _declared_field_mapping(
            supplied_finding,
            QualityFinding.model_fields,
        )
        if fields is None:
            raise QualityGateError("review queue assessment is invalid")
        finding_fields.append(fields)
    assessment_fields["findings"] = tuple(finding_fields)

    validated: QualityAssessment | None = None
    try:
        validated = QualityAssessment.model_validate(assessment_fields)
    except (TypeError, ValueError):
        pass
    if validated is None:
        raise QualityGateError("review queue assessment is invalid")
    return validated


def _queue_bytes(assessments: Iterable[QualityAssessment]) -> bytes:
    by_case: dict[str, QualityAssessment] = {}
    for supplied_assessment in assessments:
        assessment = _revalidate_queue_assessment(supplied_assessment)
        if assessment.case_id in by_case:
            raise QualityGateError("review queue contains duplicate case identifiers")
        by_case[assessment.case_id] = assessment
    if not by_case:
        raise QualityGateError("review queue cannot be empty")

    records: list[dict[str, object]] = []
    for case_id in sorted(by_case):
        assessment = by_case[case_id]
        if assessment.findings:
            records.extend(
                {
                    "case_id": finding.case_id,
                    "page_id": finding.page_id,
                    "reason_code": finding.reason_code,
                    "count": finding.count,
                }
                for finding in assessment.findings
            )
        else:
            records.append(
                {
                    "case_id": assessment.case_id,
                    "page_id": None,
                    "reason_code": "human-review-required",
                    "count": 1,
                }
            )
    return b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )


def write_review_queue(
    artifact_root: Path,
    release_id: str,
    assessments: Iterable[QualityAssessment],
) -> Path:
    """Atomically replace one deterministic, value-free review queue."""
    match = _RELEASE_ID_RE.fullmatch(release_id)
    if match is None:
        raise QualityGateError("release identifier is invalid")
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        raise QualityGateError("release identifier is invalid")
    queue_directory = _review_queue_directory(artifact_root)
    output = queue_directory / f"{release_id}.jsonl"
    if output.is_symlink():
        raise QualityGateError("review queue storage path is unsafe")
    payload = _queue_bytes(assessments)

    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=queue_directory,
            prefix=f".{release_id}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            file_descriptor = -1
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    except OSError as error:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise QualityGateError("cannot write review queue") from error
    return output
