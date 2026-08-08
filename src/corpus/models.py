"""Strict, portable contracts for canonical education-administration data."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any, Literal, TypeAlias

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

ReviewStatus: TypeAlias = Literal[
    "machine_extracted", "needs_review", "search_approved", "approved", "rejected"
]
PiiClass: TypeAlias = Literal[
    "none", "anonymized_case", "quasi_identifier", "public_credit", "restricted"
]
CurrencyStatus: TypeAlias = Literal[
    "unverified", "current", "historical_reference", "superseded"
]

_SAFE_PII_CLASSES = ["none", "anonymized_case", "quasi_identifier"]
_UNSAFE_REVIEW_STATUSES = ["machine_extracted", "needs_review", "rejected"]
_FALSE_ELIGIBILITY = {
    "properties": {
        "search_eligible": {"const": False},
        "answer_eligible": {"const": False},
    }
}


def _add_case_schema_invariants(schema: dict[str, Any]) -> None:
    """Add standard JSON Schema conditionals equivalent to Case eligibility policy."""
    schema["allOf"] = [
        {
            "if": {
                "properties": {"case_type": {"const": "credits"}},
                "required": ["case_type"],
            },
            "then": _FALSE_ELIGIBILITY,
        },
        {
            "if": {
                "properties": {
                    "pii_class": {"enum": ["public_credit", "restricted"]}
                },
                "required": ["pii_class"],
            },
            "then": _FALSE_ELIGIBILITY,
        },
        {
            "if": {
                "properties": {
                    "case_type": {"not": {"const": "credits"}},
                    "pii_class": {"enum": _SAFE_PII_CLASSES},
                    "review_status": {"enum": _UNSAFE_REVIEW_STATUSES},
                },
                "required": ["case_type", "pii_class", "review_status"],
            },
            "then": _FALSE_ELIGIBILITY,
        },
        {
            "if": {
                "properties": {
                    "case_type": {"not": {"const": "credits"}},
                    "pii_class": {"enum": _SAFE_PII_CLASSES},
                    "review_status": {"const": "search_approved"},
                },
                "required": ["case_type", "pii_class", "review_status"],
            },
            "then": {
                "properties": {
                    "search_eligible": {"const": True},
                    "answer_eligible": {"const": False},
                }
            },
        },
        {
            "if": {
                "properties": {
                    "case_type": {"not": {"const": "credits"}},
                    "pii_class": {"enum": _SAFE_PII_CLASSES},
                    "review_status": {"const": "approved"},
                },
                "required": ["case_type", "pii_class", "review_status"],
            },
            "then": {"properties": {"search_eligible": {"const": True}}},
        },
    ]


def _add_chunk_schema_invariants(schema: dict[str, Any]) -> None:
    """Add standard JSON Schema conditionals equivalent to Chunk privacy policy."""
    schema["allOf"] = [
        {
            "if": {
                "properties": {
                    "pii_class": {"enum": ["public_credit", "restricted"]}
                },
                "required": ["pii_class"],
            },
            "then": _FALSE_ELIGIBILITY,
        },
        {
            "if": {
                "properties": {"answer_eligible": {"const": True}},
                "required": ["answer_eligible"],
            },
            "then": {"properties": {"search_eligible": {"const": True}}},
        },
    ]


class CanonicalModel(BaseModel):
    """Base class that rejects data not explicitly covered by a reviewed contract."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceSpan(CanonicalModel):
    """A locatable fragment in an original PDF page."""

    model_config = ConfigDict(
        json_schema_extra={
            "$comment": (
                "Runtime canonical validation is required because JSON Schema cannot compare "
                "bbox coordinates to enforce x0 < x1 and y0 < y1."
            )
        }
    )

    pdf_page_index: int = Field(ge=1)
    page_label: str | None = None
    bbox: tuple[float, float, float, float]
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_finite_ordered_bbox(self) -> SourceSpan:
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(value) for value in self.bbox):
            raise ValueError("bbox coordinates must be finite")
        if x0 >= x1 or y0 >= y1:
            raise ValueError("bbox coordinates must be ordered x0,y0,x1,y1")
        return self


class Document(CanonicalModel):
    """A verified source document and the policy required to use it safely."""

    model_config = ConfigDict(
        json_schema_extra={
            "$comment": (
                "Runtime canonical validation is required because JSON Schema cannot compare "
                "source_period_start and source_period_end."
            )
        }
    )

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    registration_no: str | None = None
    source_period_start: date | None = None
    source_period_end: date | None = None
    source_filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int = Field(gt=0)
    extraction_method: Literal["native", "ocr"]
    source_dpi: int | None = Field(default=None, gt=0)
    public_url: AnyHttpUrl | None = None
    redistribution_status: Literal["unverified", "approved", "denied"]
    access_level: Literal["staff", "public"]
    page_numbering_rule: str = Field(min_length=1)
    ingestion_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def has_chronological_source_period(self) -> Document:
        if (
            self.source_period_start is not None
            and self.source_period_end is not None
            and self.source_period_end < self.source_period_start
        ):
            raise ValueError("source period end must not precede source period start")
        return self


class Case(CanonicalModel):
    """One independently citable question-answer, audit, law-index, or credits record."""

    model_config = ConfigDict(json_schema_extra=_add_case_schema_invariants)

    case_id: str = Field(min_length=1)
    legacy_ids: tuple[str, ...] = ()
    doc_id: str = Field(min_length=1)
    case_type: Literal["qa", "audit", "law_index", "credits"]
    domain: str = Field(min_length=1)
    part: str = Field(min_length=1)
    subtopic: str | None = None
    case_no: str = Field(min_length=1)
    title_raw: str = Field(min_length=1)
    title_normalized: str = Field(min_length=1)
    question: str | None = None
    answer: str | None = None
    facts: str | None = None
    basis_text: str | None = None
    law_ref_ids: tuple[str, ...] = ()
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    extraction_source: Literal["native", "ocr"]
    extraction_confidence: float = Field(ge=0, le=1)
    critical_field_review: str = Field(min_length=1)
    pii_class: PiiClass
    anonymization_status: str = Field(min_length=1)
    currency_status: CurrencyStatus
    search_eligible: bool
    answer_eligible: bool
    review_status: ReviewStatus

    @model_validator(mode="after")
    def eligibility_matches_review_and_privacy(self) -> Case:
        reason: str
        if self.case_type == "credits":
            expected = (False, False)
            reason = "credits"
        elif self.pii_class in {"public_credit", "restricted"}:
            expected = (False, False)
            reason = self.pii_class
        elif self.review_status in {"machine_extracted", "needs_review", "rejected"}:
            expected = (False, False)
            reason = "review status"
        elif self.review_status == "search_approved":
            expected = (True, False)
            reason = "review status"
        else:  # approved + safe PII class
            expected = None
            reason = "approved eligibility"

        actual = (self.search_eligible, self.answer_eligible)
        if expected is not None and actual != expected:
            raise ValueError(f"eligibility violates {reason} policy")
        if expected is None and not self.search_eligible:
            raise ValueError("eligibility requires approved cases to be searchable")
        if self.answer_eligible and not self.search_eligible:
            raise ValueError("answer eligibility requires search eligibility")
        return self


class Chunk(CanonicalModel):
    """A retrievable child of a single canonical case."""

    model_config = ConfigDict(json_schema_extra=_add_chunk_schema_invariants)

    chunk_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    role: Literal["question", "answer", "basis", "facts", "table"]
    sequence: int = Field(ge=1)
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    source_span_indexes: tuple[int, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    token_count: int = Field(ge=0)
    quality_flags: tuple[str, ...] = ()
    pii_class: PiiClass
    search_eligible: bool
    answer_eligible: bool

    @model_validator(mode="after")
    def has_valid_local_references_and_eligibility(self) -> Chunk:
        if any(index < 0 for index in self.source_span_indexes) or len(
            set(self.source_span_indexes)
        ) != len(self.source_span_indexes):
            raise ValueError("source span indexes must be unique non-negative indexes")
        if self.pii_class in {"public_credit", "restricted"} and (
            self.search_eligible or self.answer_eligible
        ):
            raise ValueError(f"eligibility violates {self.pii_class} policy")
        if self.answer_eligible and not self.search_eligible:
            raise ValueError("answer eligibility requires search eligibility")
        return self


class LawRef(CanonicalModel):
    """A law or guidance citation preserved exactly as the source document printed it."""

    law_ref_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    abbreviation: str | None = None
    article: str | None = None
    paragraph: str | None = None
    item: str | None = None
    cited_effective_date: date | None = None
    quote: str = Field(min_length=1)
    source_span: SourceSpan
    parsing_confidence: float = Field(ge=0, le=1)
    currency_status: CurrencyStatus
    review_status: ReviewStatus


class CaseRelation(CanonicalModel):
    """A reviewed relationship between two distinct canonical cases."""

    model_config = ConfigDict(
        json_schema_extra={
            "$comment": (
                "Runtime canonical validation is required because JSON Schema cannot compare "
                "source_case_id and target_case_id to reject self-relations."
            )
        }
    )

    relation_id: str = Field(min_length=1)
    source_case_id: str = Field(min_length=1)
    target_case_id: str = Field(min_length=1)
    relation_type: Literal["related", "duplicate", "supersedes", "conflicts"]
    confidence: float = Field(ge=0, le=1)
    review_status: ReviewStatus

    @model_validator(mode="after")
    def relates_distinct_cases(self) -> CaseRelation:
        if self.source_case_id == self.target_case_id:
            raise ValueError("case relations must connect different cases")
        return self


class DocumentPageCounts(CanonicalModel):
    """Per-document terminal extraction counts recorded by an ingestion run."""

    succeeded: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    failed: int = Field(ge=0)


class IngestionRun(CanonicalModel):
    """Reproducibility and approval metadata for one canonical-corpus build."""

    run_id: str = Field(min_length=1)
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    started_at: datetime
    ended_at: datetime | None = None
    manifest_version: str = Field(min_length=1)
    source_sha256s: tuple[str, ...] = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    ocr_engine_version: str | None = None
    ocr_model_version: str | None = None
    container_image: str = Field(min_length=1)
    normalizer_version: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_page_counts: dict[str, DocumentPageCounts]
    created_case_ids: tuple[str, ...] = ()
    changed_case_ids: tuple[str, ...] = ()
    deleted_case_ids: tuple[str, ...] = ()
    quality_metrics: dict[str, float] = Field(default_factory=dict)
    approved_by: str | None = None

    @model_validator(mode="after")
    def has_consistent_utc_timeline_and_hashes(self) -> IngestionRun:
        if not _is_utc(self.started_at) or (self.ended_at is not None and not _is_utc(self.ended_at)):
            raise ValueError("ingestion timestamps must be explicit UTC values")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ingestion end time must not precede start time")
        for source_sha256 in self.source_sha256s:
            if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
                raise ValueError("source SHA-256 values must be lowercase hexadecimal")
        return self


def _is_utc(value: datetime) -> bool:
    """Return whether a datetime is unambiguous and explicitly UTC."""
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)
