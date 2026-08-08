"""Fail-closed annual page parser with exact, value-free source provenance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal, Self, cast, overload

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.corpus.models import SourceSpan
from src.ingestion.extract_common import LayoutEvidence, RawPage, revalidate_raw_page
from src.ingestion.extract_native import (
    ExtractedPageRecord,
    NativeExtractionError,
    NativePageRecord,
    QuarantinedPageRecord,
    validate_native_page_record,
)
from src.ingestion.extract_ocr import (
    ExtractedOcrPageRecord,
    OcrExtractionError,
    OcrPageRecord,
    QuarantinedOcrPageRecord,
    validate_ocr_page_record,
)
from src.ingestion.manifest import SourceDocument

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SENTENCE_END_RE = re.compile(r"[.!?。！？]\s*$")
_CASE_LABEL_RE = re.compile(
    r"^(?:번호\s*상자|사례\s*번호|카드\s*사례)\s*[:：#]?\s*([0-9]+(?:-[0-9]+)*)\s*$"
)
_QUESTION_RE = re.compile(r"^질문(?P<ordinal>[1-9][0-9]*)?\s*[:：]?\s*(?P<text>.+)$")
_ANSWER_RE = re.compile(r"^답변(?P<ordinal>[1-9][0-9]*)?\s*[:：]?\s*(?P<text>.+)$")
_CONTINUED_ANSWER_RE = re.compile(r"^답변\s*계속\s*[:：]?\s*(?P<text>.+)$")
_TITLE_RE = re.compile(r"^(?:질문\s*제목|제목(?:·상황)?)\s*[:：]?\s*(?P<text>.+)$")
_BASIS_RE = re.compile(
    r"^(?:관련\s*근거|관련근거|근거|참고자료)\s*[:：]?\s*(?P<text>.+)$"
)
_TARGET_RE = re.compile(r"^대상\s*[:：]?\s*(?P<text>.+)$")
_SITUATION_RE = re.compile(r"^상황\s*[:：]?\s*(?P<text>.+)$")
_FACTS_RE = re.compile(r"^(?:감사\s*사실|사실)\s*[:：]?\s*(?P<text>.+)$")
_DOMAIN_RE = re.compile(r"^대분류(?:\s*탭)?\s*[:：]\s*(?P<text>.+)$")
_PART_RE = re.compile(r"^편\s*[:：]\s*(?P<text>.+)$")
_CENTERED_PART_RE = re.compile(r"^[0-9]+\s*편\s+(?P<text>.+)$")
_SUBTOPIC_RE = re.compile(r"^소주제\s*[:：]\s*(?P<text>.+)$")
_BULLET_RE = re.compile(r"^[•●▪∙]\s*(?P<text>.+)$")
_BULLET_ONLY_RE = re.compile(r"^[•●▪∙]$")
_AUDIT_RUN_RE = re.compile(r"^감사\s*사례$")
_AUDIT_TITLE_RE = re.compile(r"^(?P<number>[0-9]{1,3})[.]\s*(?P<title>\S.*)$")
_ROMAN_DOMAIN_RE = re.compile(
    r"^(?:[IVXLCDM]{1,5}|[Ⅰ-Ⅻ])[.)]?\s+(?P<text>\S.*)$",
    re.IGNORECASE,
)
_NUMBERED_ROLE_RE = re.compile(
    r"^[1-9][0-9]*[.)]\s*(?P<role>질문|답변|근거|참고자료|대상|상황)\s*$"
)
_UNLABELED_ORDINAL_RE = re.compile(r"^(?P<ordinal>[1-9][0-9]*)[.)]\s*(?P<text>.+)$")
_SAFE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_HEADING_ORDINAL_RE = re.compile(r"^(?:[0-9]{1,2}|[IVXLCDM]{1,5})$", re.IGNORECASE)
_ROMAN_TAB_RE = re.compile(r"^(?:[IVXLCDM]{1,5}|[Ⅰ-Ⅻ])[.)]?$", re.IGNORECASE)
_LAYOUT_REGISTRY_HASH_PREFIX = b"sen-qa-layout-segment-registry-v1\0"

PageRole = Literal["cover", "toc", "credits", "law_list", "body"]
ReviewStatus = Literal["machine_extracted", "needs_review"]
CriticalReview = Literal["not_applicable", "unverified", "sampling_required"]
FragmentRole = Literal[
    "title", "question", "answer", "facts", "basis", "target", "situation"
]


class ParserContractError(ValueError):
    """Value-free error raised before untrusted page envelopes enter parsing."""


class ParserModel(BaseModel):
    """Strict immutable base for deterministic parser contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _layout_registry_sha256(
    *,
    detector_version: str,
    doc_id: str,
    edition_year: int,
    sampling_status: str,
    segment_start_pdf_page: int,
    segment_end_pdf_page: int,
    source_sha256: str,
) -> str:
    payload = {
        "detector_version": detector_version,
        "doc_id": doc_id,
        "edition_year": edition_year,
        "policy_version": "layout-segment-registry-v1",
        "sampling_status": sampling_status,
        "segment_end_pdf_page": segment_end_pdf_page,
        "segment_key": "approved-document-body",
        "segment_start_pdf_page": segment_start_pdf_page,
        "source_sha256": source_sha256,
    }
    return hashlib.sha256(
        _LAYOUT_REGISTRY_HASH_PREFIX
        + json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


class LayoutSegmentProvenance(ParserModel):
    """Registry-bound OCR layout segment for one exact rendered page."""

    segment_id: str
    segment_key: Literal["approved-document-body"]
    segment_start_pdf_page: int = Field(ge=1)
    segment_end_pdf_page: int = Field(ge=1)
    registry_policy_version: Literal["layout-segment-registry-v1"]
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detector_version: str = Field(min_length=1)
    region_count: int = Field(ge=0)
    sampling_status: Literal["all_cases_required", "sampling_required"]
    doc_id: str = Field(min_length=1)
    edition_year: Literal[2024, 2025]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_index: int = Field(ge=1)
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_exact_registry_binding(self) -> Self:
        if not _SAFE_SEGMENT_RE.fullmatch(self.segment_id):
            raise ValueError("layout segment identifier is invalid")
        if (
            self.segment_start_pdf_page > self.segment_end_pdf_page
            or not self.segment_start_pdf_page
            <= self.pdf_page_index
            <= self.segment_end_pdf_page
        ):
            raise ValueError("layout segment page is outside registry range")
        expected_sampling = (
            "all_cases_required" if self.edition_year == 2024 else "sampling_required"
        )
        if self.sampling_status != expected_sampling:
            raise ValueError("layout segment sampling status is invalid")
        expected_registry = _layout_registry_sha256(
            detector_version=self.detector_version,
            doc_id=self.doc_id,
            edition_year=self.edition_year,
            sampling_status=self.sampling_status,
            segment_start_pdf_page=self.segment_start_pdf_page,
            segment_end_pdf_page=self.segment_end_pdf_page,
            source_sha256=self.source_sha256,
        )
        if self.registry_sha256 != expected_registry:
            raise ValueError("layout segment registry digest is invalid")
        return self


class ParserLine(ParserModel):
    """One exact source span plus parser-only normalized projection."""

    raw_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)
    font: str
    size: float
    source_block_index: int = Field(ge=0)
    source_line_index: int = Field(ge=0)
    source_span_index: int = Field(ge=0)
    semantic_hint: str | None = None
    raw_text_sha256: str
    duplicate_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def has_locatable_exact_provenance(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(value) for value in self.bbox) or x0 >= x1 or y0 >= y1:
            raise ValueError("parser line bbox must have positive area")
        if not math.isfinite(self.size):
            raise ValueError("parser line size must be finite")
        expected = hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()
        if (
            not _SHA256_RE.fullmatch(self.raw_text_sha256)
            or self.raw_text_sha256 != expected
        ):
            raise ValueError("parser line hash does not match exact source text")
        return self


class VerifiedPageRolePolicy(ParserModel):
    """Manifest-reviewed page roles; raw words never decide front/back matter."""

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    extraction_source: Literal["native", "ocr"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int = Field(gt=0)
    body_start_pdf_page: int = Field(ge=1)
    body_end_pdf_page: int = Field(ge=1)
    cover_page_indexes: tuple[int, ...] = (1,)
    toc_page_indexes: tuple[int, ...] = ()
    credits_page_indexes: tuple[int, ...] = ()
    law_list_page_indexes: tuple[int, ...] = ()

    @classmethod
    def from_source_document(
        cls,
        document: SourceDocument,
        *,
        cover_page_indexes: tuple[int, ...] = (1,),
        toc_page_indexes: tuple[int, ...] = (),
        credits_page_indexes: tuple[int, ...] = (),
        law_list_page_indexes: tuple[int, ...] = (),
    ) -> Self:
        """Bind role decisions to one exact manifest document."""
        return cls(
            doc_id=document.doc_id,
            edition_year=document.edition_year,
            extraction_source=document.extraction_method,
            source_sha256=document.sha256,
            pdf_page_count=document.pdf_page_count,
            body_start_pdf_page=document.page_numbering.body_start_pdf_page,
            body_end_pdf_page=document.page_numbering.body_end_pdf_page,
            cover_page_indexes=cover_page_indexes,
            toc_page_indexes=toc_page_indexes,
            credits_page_indexes=credits_page_indexes,
            law_list_page_indexes=law_list_page_indexes,
        )

    @model_validator(mode="after")
    def has_disjoint_bounded_roles(self) -> Self:
        if (
            self.body_start_pdf_page > self.body_end_pdf_page
            or self.body_end_pdf_page > self.pdf_page_count
        ):
            raise ValueError("page role body bounds are invalid")
        groups = (
            self.cover_page_indexes,
            self.toc_page_indexes,
            self.credits_page_indexes,
            self.law_list_page_indexes,
        )
        flat = tuple(index for group in groups for index in group)
        if any(group != tuple(sorted(set(group))) for group in groups):
            raise ValueError("page role indexes must be unique and sorted")
        if len(flat) != len(set(flat)) or any(
            index < 1 or index > self.pdf_page_count for index in flat
        ):
            raise ValueError("page role indexes must be disjoint and bounded")
        return self

    def role_for(self, pdf_page_index: int) -> PageRole | None:
        if pdf_page_index in self.cover_page_indexes:
            return "cover"
        if pdf_page_index in self.toc_page_indexes:
            return "toc"
        if pdf_page_index in self.credits_page_indexes:
            return "credits"
        if pdf_page_index in self.law_list_page_indexes:
            return "law_list"
        if self.body_start_pdf_page <= pdf_page_index <= self.body_end_pdf_page:
            return "body"
        return None


class ParserPage(ParserModel):
    """Extractor-neutral page envelope retaining only exact locatable spans."""

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    extraction_source: Literal["native", "ocr"]
    source_sha256: str | None
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    page_width: float | None
    page_height: float | None
    render_sha256: str | None
    lines: tuple[ParserLine, ...]
    normalized_text: str | None = None
    page_status: Literal["extracted", "quarantined"] = "extracted"
    page_role_hint: PageRole | None = None
    quality_flags: tuple[str, ...] = ()
    upstream_review_status: ReviewStatus
    critical_review_policy: Literal[
        "not_applicable",
        "all-fields-human-verification",
        "stratified-sample-with-layout-escalation",
    ]
    critical_fields: tuple[str, ...] = ()
    layout_evidence: LayoutEvidence
    layout_segment_provenance: LayoutSegmentProvenance | None = None
    upstream_reason_code: str | None = None

    @model_validator(mode="after")
    def has_consistent_page_envelope(self) -> Self:
        if self.page_status == "quarantined":
            if self.lines or self.upstream_reason_code is None:
                raise ValueError("quarantined parser page has invalid provenance")
            return self
        if (
            self.page_width is None
            or self.page_height is None
            or self.render_sha256 is None
        ):
            raise ValueError("extracted parser page requires complete geometry")
        if (
            not math.isfinite(self.page_width)
            or not math.isfinite(self.page_height)
            or self.page_width <= 0
            or self.page_height <= 0
        ):
            raise ValueError("parser page geometry is invalid")
        if not _SHA256_RE.fullmatch(self.render_sha256):
            raise ValueError("parser page render hash is invalid")
        if self.source_sha256 is not None and not _SHA256_RE.fullmatch(
            self.source_sha256
        ):
            raise ValueError("parser page source hash is invalid")
        keys = tuple(_line_order_key(line) for line in self.lines)
        if keys != tuple(sorted(keys)):
            raise ValueError("parser lines must be deterministically ordered")
        if any(
            line.bbox[0] < 0
            or line.bbox[1] < 0
            or line.bbox[2] > self.page_width
            or line.bbox[3] > self.page_height
            for line in self.lines
        ):
            raise ValueError("parser line must remain inside page geometry")
        segment = self.layout_segment_provenance
        if segment is not None and (
            self.extraction_source != "ocr"
            or self.source_sha256 is None
            or segment.doc_id != self.doc_id
            or segment.edition_year != self.edition_year
            or segment.source_sha256 != self.source_sha256
            or segment.pdf_page_index != self.pdf_page_index
            or segment.render_sha256 != self.render_sha256
            or (
                self.layout_evidence.detector_version is not None
                and segment.detector_version != self.layout_evidence.detector_version
            )
            or segment.region_count != len(self.layout_evidence.regions)
        ):
            raise ValueError("layout segment provenance does not match parser page")
        if (
            self.edition_year in (2024, 2025)
            and self.page_role_hint == "body"
            and segment is None
        ):
            raise ValueError("body OCR page requires layout segment provenance")
        return self


class HierarchyState(ParserModel):
    domain: str | None = None
    part: str | None = None
    subtopic: str | None = None


class RoleFragment(ParserModel):
    role: FragmentRole
    ordinal: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1)
    source_span: SourceSpan
    confidence: float = Field(ge=0.0, le=1.0)
    continuation: bool = False


class ParsedCaseCandidate(ParserModel):
    """Fail-closed pre-canonical candidate; Task 9 performs canonical promotion."""

    doc_id: str
    edition_year: int
    case_type: Literal["qa", "audit"]
    domain: str
    part: str
    subtopic: str | None
    case_no: str
    fragments: tuple[RoleFragment, ...]
    title: str
    question: str | None
    answer: str | None
    facts: str | None
    basis_text: str | None
    target_text: str | None
    situation_text: str | None
    source_spans: tuple[SourceSpan, ...]
    extraction_source: Literal["native", "ocr"]
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    boundary_status: Literal["closed"] = "closed"
    layout_segment_id: str | None
    layout_segment_provenances: tuple[LayoutSegmentProvenance, ...]
    upstream_review_status: ReviewStatus
    critical_field_review: CriticalReview
    review_status: ReviewStatus
    search_eligible: Literal[False] = False
    answer_eligible: Literal[False] = False


class BoundaryQuarantine(ParserModel):
    """Value-free, locatable ambiguity record."""

    location_id: str = Field(pattern=r"^loc-[0-9a-f]{32}$")
    reason_code: Literal["ambiguous_boundary"] = "ambiguous_boundary"
    page_ids: tuple[int, ...] = Field(min_length=1)
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    span_count: int = Field(ge=1)

    @model_validator(mode="after")
    def matches_source_locations(self) -> Self:
        if self.span_count != len(self.source_spans):
            raise ValueError("quarantine span count does not match provenance")
        if self.page_ids != tuple(sorted(set(self.page_ids))):
            raise ValueError("quarantine pages must be unique and sorted")
        return self


class UpstreamPageQuarantine(ParserModel):
    """Value-free upstream page failure retained even when no text span exists."""

    location_id: str = Field(pattern=r"^loc-[0-9a-f]{32}$")
    reason_code: Literal[
        "page-extraction-failed",
        "page-render-failed",
        "ocr-adapter-failed",
        "ocr-provenance-invalid",
    ]
    page_ids: tuple[int, ...] = Field(min_length=1, max_length=1)
    source_spans: tuple[()] = ()
    span_count: Literal[0] = 0
    occurrence_count: Literal[1] = 1


ParserQuarantine = BoundaryQuarantine | UpstreamPageQuarantine


class MetadataTransition(ParserModel):
    pdf_page_index: int = Field(ge=1)
    role: Literal["cover", "toc", "credits", "law_list", "domain", "part", "subtopic"]
    value: str | None
    source_span: SourceSpan
    hierarchy_before: HierarchyState
    hierarchy_after: HierarchyState


class ParseResult(ParserModel, Sequence[ParsedCaseCandidate]):
    cases: tuple[ParsedCaseCandidate, ...] = ()
    quarantines: tuple[ParserQuarantine, ...] = ()
    transitions: tuple[MetadataTransition, ...] = ()

    def __len__(self) -> int:
        return len(self.cases)

    @overload
    def __getitem__(self, index: int) -> ParsedCaseCandidate: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ParsedCaseCandidate, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ParsedCaseCandidate | tuple[ParsedCaseCandidate, ...]:
        return self.cases[index]

    def __iter__(self) -> Iterator[ParsedCaseCandidate]:  # type: ignore[override]
        return iter(self.cases)


def canonical_result_bytes(result: ParseResult) -> bytes:
    """Return deterministic canonical JSON without exception or log strings."""
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalized_projection(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    )
    return "\n".join(
        re.sub(r"[^\S\n]+", " ", line).strip() for line in normalized.split("\n")
    ).strip()


def _line_order_key(
    line: ParserLine,
) -> tuple[float, float, float, float, int, int, int, str]:
    return (
        line.bbox[1],
        line.bbox[0],
        line.bbox[3],
        line.bbox[2],
        line.source_block_index,
        line.source_line_index,
        line.source_span_index,
        line.raw_text_sha256,
    )


def _validated_page_role_policy(
    policy: VerifiedPageRolePolicy,
) -> VerifiedPageRolePolicy:
    try:
        if type(policy) is VerifiedPageRolePolicy:
            payload = {
                name: getattr(policy, name)
                for name in VerifiedPageRolePolicy.model_fields
            }
            return VerifiedPageRolePolicy.model_validate(payload)
    except (TypeError, ValueError, AttributeError):
        pass
    raise ParserContractError("verified page role policy is invalid")


def _policy_role_for_envelope(
    policy: VerifiedPageRolePolicy,
    *,
    doc_id: str,
    edition_year: int,
    extraction_source: Literal["native", "ocr"],
    pdf_page_index: int,
    source_sha256: str | None,
    document_pdf_page_count: int | None = None,
) -> PageRole:
    checked = _validated_page_role_policy(policy)
    if (
        checked.doc_id != doc_id
        or checked.edition_year != edition_year
        or checked.extraction_source != extraction_source
        or pdf_page_index > checked.pdf_page_count
        or source_sha256 != checked.source_sha256
        or (
            document_pdf_page_count is not None
            and document_pdf_page_count != checked.pdf_page_count
        )
    ):
        raise ParserContractError("page envelope does not match verified role policy")
    role = checked.role_for(pdf_page_index)
    if role is None:
        raise ParserContractError("verified page role policy leaves page unclassified")
    return role


def parser_page_from_raw_page(
    raw_page: RawPage,
    *,
    normalized_text: str | None,
    page_role_hint: PageRole | None = None,
    page_role_policy: VerifiedPageRolePolicy | None = None,
    retained_raw_block_indexes: tuple[int, ...] | None = None,
    quality_flags: tuple[str, ...] = (),
    upstream_review_status: ReviewStatus,
    critical_review_policy: Literal[
        "not_applicable",
        "all-fields-human-verification",
        "stratified-sample-with-layout-escalation",
    ],
    critical_fields: tuple[str, ...] = (),
    layout_segment_provenance: LayoutSegmentProvenance | None = None,
    source_sha256: str | None = None,
) -> ParserPage:
    """Adapt immutable raw spans without hashing or citing normalized text."""
    validated_raw_page = revalidate_raw_page(raw_page)
    if validated_raw_page is None:
        raise ParserContractError("raw page provenance is invalid")
    raw_page = validated_raw_page
    selected = (
        set(retained_raw_block_indexes)
        if retained_raw_block_indexes is not None
        else set(range(len(raw_page.raw_blocks)))
    )
    if any(index < 0 or index >= len(raw_page.raw_blocks) for index in selected):
        raise ParserContractError("retained raw block indexes are invalid")
    deduped: dict[tuple[str, tuple[float, float, float, float]], ParserLine] = {}
    counts: dict[tuple[str, tuple[float, float, float, float]], int] = {}
    try:
        for block_index, block in enumerate(raw_page.raw_blocks):
            if block_index not in selected:
                continue
            for line_index, raw_line in enumerate(block.lines):
                for span_index, span in enumerate(raw_line.spans):
                    normalized_span_text = _normalized_projection(span.text)
                    if not normalized_span_text:
                        continue
                    bbox = (span.bbox.x0, span.bbox.y0, span.bbox.x1, span.bbox.y1)
                    digest = hashlib.sha256(span.text.encode("utf-8")).hexdigest()
                    key = (digest, bbox)
                    counts[key] = counts.get(key, 0) + 1
                    candidate = ParserLine(
                        raw_text=span.text,
                        normalized_text=normalized_span_text,
                        bbox=bbox,
                        confidence=min(raw_line.confidence, span.confidence),
                        font=span.font,
                        size=span.size,
                        source_block_index=block_index,
                        source_line_index=line_index,
                        source_span_index=span_index,
                        semantic_hint=span.semantic_hint,
                        raw_text_sha256=digest,
                    )
                    existing = deduped.get(key)
                    if existing is None or _line_order_key(candidate) < _line_order_key(
                        existing
                    ):
                        deduped[key] = candidate
        lines = tuple(
            sorted(
                (
                    line.model_copy(update={"duplicate_count": counts[key]})
                    for key, line in deduped.items()
                ),
                key=_line_order_key,
            )
        )
        role = page_role_hint
        if page_role_policy is not None:
            policy_role = _policy_role_for_envelope(
                page_role_policy,
                doc_id=raw_page.doc_id,
                edition_year=raw_page.edition_year,
                extraction_source=raw_page.extraction_source,
                pdf_page_index=raw_page.pdf_page_index,
                source_sha256=source_sha256,
            )
            if role is not None and policy_role != role:
                raise ParserContractError(
                    "page role hint does not match verified policy"
                )
            role = policy_role
        return ParserPage(
            doc_id=raw_page.doc_id,
            edition_year=raw_page.edition_year,
            extraction_source=raw_page.extraction_source,
            source_sha256=source_sha256,
            pdf_page_index=raw_page.pdf_page_index,
            page_label=raw_page.page_label,
            page_width=raw_page.page_width,
            page_height=raw_page.page_height,
            render_sha256=raw_page.render_sha256,
            lines=lines,
            normalized_text=normalized_text,
            page_role_hint=role,
            quality_flags=quality_flags,
            upstream_review_status=upstream_review_status,
            critical_review_policy=critical_review_policy,
            critical_fields=critical_fields,
            layout_evidence=raw_page.layout_evidence,
            layout_segment_provenance=layout_segment_provenance,
        )
    except ParserContractError:
        raise
    except (ValueError, TypeError):
        pass
    raise ParserContractError("raw page cannot form positive area parser lines")


def _source_span(page: ParserPage, line: ParserLine) -> SourceSpan:
    return SourceSpan(
        pdf_page_index=page.pdf_page_index,
        page_label=page.page_label,
        bbox=line.bbox,
        text_sha256=line.raw_text_sha256,
    )


def _bbox_overlap(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> bool:
    return max(left[0], right[0]) < min(left[2], right[2]) and max(
        left[1], right[1]
    ) < min(left[3], right[3])


def _contains(
    outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]
) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


@dataclass
class _OpenCase:
    doc_id: str
    edition_year: int
    extraction_source: Literal["native", "ocr"]
    domain: str | None
    part: str | None
    subtopic: str | None
    case_no: str
    upstream_review_status: ReviewStatus
    case_type: Literal["qa", "audit"] = "qa"
    lines: list[tuple[ParserPage, ParserLine]] = field(default_factory=list)
    fragments: list[RoleFragment] = field(default_factory=list)
    implicit_role: FragmentRole | None = None
    force_quarantine: bool = False
    question_fallback_blocked: bool = False
    title_question_fallback: bool = False


def _line_role(
    page: ParserPage, line: ParserLine, opened: _OpenCase
) -> tuple[FragmentRole, int | None, str, bool] | None:
    value = line.normalized_text
    if opened.edition_year <= 2022:
        marker_page, marker = opened.lines[0]
        strict_header_geometry = bool(
            page.pdf_page_index == marker_page.pdf_page_index
            and line.source_block_index == marker.source_block_index
            and line.source_line_index != marker.source_line_index
            and line.bbox[0] > marker.bbox[2]
            and _same_native_header_row(marker_page, marker.bbox, line.bbox)
            and 9.0 <= line.size <= 12.0
            and not re.fullmatch(r"[0-9]{1,4}(?:-[0-9]+)?", value)
        )
        if strict_header_geometry:
            title_fragments = [
                fragment for fragment in opened.fragments if fragment.role == "title"
            ]
            if not title_fragments:
                return "title", None, value, False
            last_title = title_fragments[-1]
            last_title_line = next(
                (
                    source_line
                    for source_page, source_line in opened.lines
                    if source_page.pdf_page_index
                    == last_title.source_span.pdf_page_index
                    and source_line.bbox == last_title.source_span.bbox
                    and source_line.raw_text_sha256
                    == last_title.source_span.text_sha256
                ),
                None,
            )
            if (
                last_title_line is not None
                and line.source_block_index == last_title_line.source_block_index
                and line.source_line_index == last_title_line.source_line_index
                and line.source_span_index > last_title_line.source_span_index
                and 0.0
                <= line.bbox[0] - last_title_line.bbox[2]
                <= (page.page_width or 1.0) * 0.002
                and _same_visual_row(last_title_line.bbox, line.bbox)
            ):
                return "title", None, value, False
    if match := _TITLE_RE.match(value):
        return "title", None, match.group("text").strip(), False
    if "title" in line.font.lower() and not any(
        f.role == "title" for f in opened.fragments
    ):
        return "title", None, value, False
    if opened.edition_year in (2023, 2024, 2025) and not any(
        fragment.role in {"basis", "answer", "facts", "target", "situation"}
        for fragment in opened.fragments
    ):
        marker = opened.lines[0][1]
        title_fragments = [
            fragment for fragment in opened.fragments if fragment.role == "title"
        ]
        if line.bbox[0] > marker.bbox[2]:
            if not title_fragments and line.bbox[1] <= marker.bbox[3] + 12.0:
                return "title", None, value, False
            if title_fragments and _same_visual_row(
                title_fragments[-1].source_span.bbox, line.bbox
            ):
                return "title", None, value, False
            if title_fragments:
                return "question", None, value, False
    if match := _CONTINUED_ANSWER_RE.match(value):
        ordinals = [
            f.ordinal
            for f in opened.fragments
            if f.role == "answer" and not f.continuation
        ]
        return (
            "answer",
            ordinals[-1] if ordinals else None,
            match.group("text").strip(),
            True,
        )
    if match := _QUESTION_RE.match(value):
        if match.group("text").startswith("제목"):
            return None
        if opened.edition_year in (2021, 2022):
            if page.page_width is None:
                return None
            x_ratio = line.bbox[0] / page.page_width
            if opened.implicit_role == "answer" and x_ratio <= 0.17:
                ordinals = [
                    fragment.ordinal
                    for fragment in opened.fragments
                    if fragment.role == "answer" and fragment.ordinal is not None
                ]
                return (
                    "answer",
                    ordinals[-1] if ordinals else None,
                    value,
                    True,
                )
            if x_ratio < 0.19:
                return None
        ordinal = int(match.group("ordinal")) if match.group("ordinal") else None
        return "question", ordinal, match.group("text").strip(), False
    if line.semantic_hint == "question":
        return "question", None, re.sub(r"^질문\s*[:：]?\s*", "", value).strip(), False
    if match := _ANSWER_RE.match(value):
        ordinal = int(match.group("ordinal")) if match.group("ordinal") else None
        return "answer", ordinal, match.group("text").strip(), False
    if opened.edition_year in (2021, 2022) and (
        match := _UNLABELED_ORDINAL_RE.match(value)
    ):
        ordinal = int(match.group("ordinal"))
        if opened.edition_year == 2022 and opened.implicit_role == "answer":
            return "answer", ordinal, match.group("text").strip(), False
        marker_page = opened.lines[0][0]
        if marker_page.page_width is None:
            return None
        x_ratio = line.bbox[0] / marker_page.page_width
        if x_ratio >= 0.19:
            role: FragmentRole = "question"
        elif x_ratio <= 0.17:
            role = "answer"
        else:
            return None
        return role, ordinal, match.group("text").strip(), False
    if (
        opened.edition_year in (2021, 2022)
        and opened.implicit_role not in {"basis", "facts", "situation", "target"}
        and not re.fullmatch(r"[0-9]{1,2}[.)]", value)
        and _BULLET_RE.match(value) is None
        and not value.endswith("?")
        and _BASIS_RE.match(value) is None
        and _TARGET_RE.match(value) is None
        and _SITUATION_RE.match(value) is None
        and _FACTS_RE.match(value) is None
    ):
        marker_page = opened.lines[0][0]
        if (
            marker_page.page_width is not None
            and line.bbox[0] / marker_page.page_width <= 0.17
            and 9.0 <= line.size <= 13.0
            and (
                any(fragment.role == "question" for fragment in opened.fragments)
                or (
                    any(fragment.role == "title" for fragment in opened.fragments)
                    and not opened.question_fallback_blocked
                )
            )
            and not any(fragment.role == "answer" for fragment in opened.fragments)
        ):
            return "answer", None, value, False
    if match := _BULLET_RE.match(value):
        return "answer", None, match.group("text").strip(), False
    if match := _BASIS_RE.match(value):
        return "basis", None, match.group("text").strip(), False
    if match := _TARGET_RE.match(value):
        return "target", None, match.group("text").strip(), False
    if match := _SITUATION_RE.match(value):
        return "situation", None, match.group("text").strip(), False
    if match := _FACTS_RE.match(value):
        return "facts", None, match.group("text").strip(), False
    if value.endswith("?") and not any(f.role == "question" for f in opened.fragments):
        return "question", None, value, False
    if opened.edition_year in (2021, 2022) and not any(
        fragment.role in {"question", "answer"} for fragment in opened.fragments
    ):
        marker_page, _marker = opened.lines[0]
        title_fragments = [
            fragment for fragment in opened.fragments if fragment.role == "title"
        ]
        title = title_fragments[-1] if title_fragments else None
        follows_title = bool(
            title is not None
            and (
                page.pdf_page_index > title.source_span.pdf_page_index
                or (
                    page.pdf_page_index == title.source_span.pdf_page_index
                    and line.bbox[1] > title.source_span.bbox[3]
                )
            )
        )
        if (
            marker_page.page_width is not None
            and line.bbox[0] / marker_page.page_width >= 0.19
            and follows_title
            and 9.0 <= line.size <= 13.0
        ):
            return "question", None, value, False
    if opened.edition_year <= 2022 and not any(
        fragment.role == "title" for fragment in opened.fragments
    ):
        marker = opened.lines[0][1]
        minimum_title_size = 10.0 if opened.edition_year <= 2022 else 14.0
        marker_page = opened.lines[0][0]
        if (
            line.bbox[0] > marker.bbox[2]
            and _same_native_header_row(marker_page, marker.bbox, line.bbox)
            and (opened.edition_year == 2020 or line.bbox[1] <= marker.bbox[1])
            and line.size >= minimum_title_size
        ):
            return "title", None, value, False
    return None


def _metadata(
    line: ParserLine,
) -> tuple[Literal["domain", "part", "subtopic"], str] | None:
    patterns: tuple[
        tuple[Literal["domain", "part", "subtopic"], re.Pattern[str]], ...
    ] = (
        ("domain", _DOMAIN_RE),
        ("part", _PART_RE),
        ("subtopic", _SUBTOPIC_RE),
    )
    for role, pattern in patterns:
        match = pattern.match(line.normalized_text)
        if match:
            return role, match.group("text").strip()
    return None


def _same_visual_row(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    overlap = min(left[3], right[3]) - max(left[1], right[1])
    shortest = min(left[3] - left[1], right[3] - right[1])
    return overlap > 0 and overlap / shortest >= 0.50


def _same_native_header_row(
    page: ParserPage,
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    if _same_visual_row(left, right):
        return True
    if page.page_height is None:
        return False
    return abs(left[1] - right[1]) <= page.page_height * 0.015


def _line_identity(line: ParserLine) -> tuple[int, int, int]:
    return (
        line.source_block_index,
        line.source_line_index,
        line.source_span_index,
    )


def _inside_detected_card(page: ParserPage, line: ParserLine) -> bool:
    if page.layout_evidence.status != "detected":
        return False
    return any(
        region.region_type == "card"
        and region.evidence == "raster-border"
        and _contains(
            (region.bbox.x0, region.bbox.y0, region.bbox.x1, region.bbox.y1),
            line.bbox,
        )
        for region in page.layout_evidence.regions
    )


def _actual_ocr_hierarchy(
    page: ParserPage, lines: Sequence[ParserLine]
) -> tuple[
    dict[str, tuple[ParserLine, str]],
    set[tuple[int, int, int]],
]:
    """Infer only reviewed top-of-page OCR hierarchy geometry."""
    if page.page_width is None or page.page_height is None:
        return {}, set()
    if page.edition_year in (2020, 2021, 2022):
        native_found: dict[str, tuple[ParserLine, str]] = {}
        native_consumed: set[tuple[int, int, int]] = set()
        top_lines = [line for line in lines if line.bbox[1] / page.page_height <= 0.25]
        for line in top_lines:
            match = _CENTERED_PART_RE.match(line.normalized_text)
            center = (line.bbox[0] + line.bbox[2]) / 2.0
            center_ratio = center / page.page_width
            width_ratio = (line.bbox[2] - line.bbox[0]) / page.page_width
            narrow_2020_top_heading = bool(
                page.edition_year == 2020
                and line.bbox[1] / page.page_height <= 0.08
                and 0.72 <= center_ratio <= 0.92
                and 0.03 <= width_ratio <= 0.22
                and 8.0 <= line.size <= 11.0
                and (line.bbox[3] - line.bbox[1]) / page.page_height <= 0.04
            )
            reviewed_2021_2022_part = bool(
                page.edition_year in (2021, 2022)
                and 0.10 <= line.bbox[1] / page.page_height <= 0.18
                and 0.40 <= center_ratio <= 0.60
                and 0.08 <= width_ratio <= 0.45
                and 16.0 <= line.size <= 20.0
            )
            reviewed_2020_part = bool(
                page.edition_year == 2020
                and line.bbox[1] / page.page_height <= 0.18
                and center_ratio >= 0.55
                and (width_ratio >= 0.20 or narrow_2020_top_heading)
            )
            if match is not None and (reviewed_2020_part or reviewed_2021_2022_part):
                native_found["part"] = (line, match.group("text").strip())
                native_consumed.add(_line_identity(line))
                if reviewed_2021_2022_part:
                    for overlay in top_lines:
                        if (
                            overlay is not line
                            and overlay.normalized_text == line.normalized_text
                            and abs(overlay.bbox[1] - line.bbox[1])
                            <= page.page_height * 0.01
                            and abs(
                                (overlay.bbox[0] + overlay.bbox[2])
                                - (line.bbox[0] + line.bbox[2])
                            )
                            <= page.page_width * 0.04
                            and _bbox_overlap(line.bbox, overlay.bbox)
                            and 16.0 <= overlay.size <= 20.0
                        ):
                            native_consumed.add(_line_identity(overlay))
                break
        if page.edition_year in (2021, 2022):
            for ordinal in top_lines:
                if not _ROMAN_TAB_RE.fullmatch(ordinal.normalized_text):
                    continue
                if not (
                    0.05 <= ordinal.bbox[1] / page.page_height <= 0.08
                    and 0.75 <= ordinal.bbox[0] / page.page_width <= 0.90
                    and ordinal.bbox[2] / page.page_width <= 0.98
                    and 9.5 <= ordinal.size <= 11.5
                ):
                    continue
                same_raw_line = sorted(
                    (
                        line
                        for line in top_lines
                        if line is not ordinal
                        and line.source_block_index == ordinal.source_block_index
                        and line.source_line_index == ordinal.source_line_index
                        and line.source_span_index > ordinal.source_span_index
                        and _same_native_header_row(page, ordinal.bbox, line.bbox)
                        and line.bbox[0] / page.page_width >= 0.75
                        and line.bbox[2] / page.page_width <= 0.98
                        and 9.5 <= line.size <= 11.5
                        and not _ROMAN_TAB_RE.fullmatch(line.normalized_text)
                        and not re.fullmatch(r"[0-9]{1,4}", line.normalized_text)
                    ),
                    key=lambda line: (
                        line.source_span_index,
                        line.bbox[0],
                        line.raw_text_sha256,
                    ),
                )
                if not same_raw_line:
                    continue
                combined_left = min(
                    ordinal.bbox[0], *(line.bbox[0] for line in same_raw_line)
                )
                combined_right = max(
                    ordinal.bbox[2], *(line.bbox[2] for line in same_raw_line)
                )
                combined_width = (combined_right - combined_left) / page.page_width
                if not (
                    combined_right / page.page_width <= 0.98
                    and 0.04 <= combined_width <= 0.25
                ):
                    continue
                native_found["domain"] = (
                    same_raw_line[0],
                    " ".join(line.normalized_text for line in same_raw_line),
                )
                native_consumed.update(
                    {_line_identity(ordinal)}
                    | {_line_identity(line) for line in same_raw_line}
                )
                break
            if "domain" not in native_found:
                for line in top_lines:
                    domain_match = _ROMAN_DOMAIN_RE.fullmatch(line.normalized_text)
                    width_ratio = (line.bbox[2] - line.bbox[0]) / page.page_width
                    if (
                        domain_match is not None
                        and line.bbox[1] / page.page_height <= 0.10
                        and line.bbox[0] / page.page_width >= 0.75
                        and line.bbox[2] / page.page_width <= 0.98
                        and 0.04 <= width_ratio <= 0.25
                        and 9.5 <= line.size <= 11.5
                    ):
                        native_found["domain"] = (
                            line,
                            domain_match.group("text").strip(),
                        )
                        native_consumed.add(_line_identity(line))
                        break
            for ordinal in top_lines:
                if not re.fullmatch(r"[0-9]{1,2}", ordinal.normalized_text):
                    continue
                if (
                    ordinal.bbox[0] / page.page_width > 0.20
                    or not 30.0 <= ordinal.size <= 40.0
                    or not 0.035
                    <= (ordinal.bbox[3] - ordinal.bbox[1]) / page.page_height
                    <= 0.08
                ):
                    continue
                adjacent = [
                    line
                    for line in top_lines
                    if line is not ordinal
                    and _same_native_header_row(page, ordinal.bbox, line.bbox)
                    and 14.0 <= line.size <= 18.0
                    and 0.0 <= line.bbox[0] - ordinal.bbox[2] <= page.page_width * 0.06
                    and not re.fullmatch(r"[0-9]{1,2}", line.normalized_text)
                ]
                if adjacent:
                    label = min(adjacent, key=_line_order_key)
                    native_found["subtopic"] = (label, label.normalized_text)
                    native_consumed.update(
                        {_line_identity(ordinal), _line_identity(label)}
                    )
                    break
        return native_found, native_consumed
    if page.edition_year == 2023:
        for line in lines:
            match = _CENTERED_PART_RE.match(line.normalized_text)
            center = (line.bbox[0] + line.bbox[2]) / 2.0
            if (
                match is not None
                and line.bbox[1] / page.page_height <= 0.18
                and 0.55 <= center / page.page_width <= 0.92
                and line.bbox[2] - line.bbox[0] >= page.page_width * 0.20
            ):
                return (
                    {"part": (line, match.group("text").strip())},
                    {_line_identity(line)},
                )
        return {}, set()
    if (
        page.edition_year not in (2024, 2025)
        or page.layout_evidence.status != "detected"
    ):
        return {}, set()
    card_tops = [
        region.bbox.y0
        for region in page.layout_evidence.regions
        if region.region_type == "card" and region.evidence == "raster-border"
    ]
    if not card_tops:
        return {}, set()
    first_card_top = min(card_tops)
    candidates = [
        line
        for line in lines
        if not _inside_detected_card(page, line)
        and line.bbox[3] <= first_card_top
        and line.bbox[1] / page.page_height < 0.92
    ]
    found: dict[str, tuple[ParserLine, str]] = {}
    consumed: set[tuple[int, int, int]] = set()
    if page.edition_year == 2025:
        for line in candidates:
            match = _CENTERED_PART_RE.match(line.normalized_text)
            center = (line.bbox[0] + line.bbox[2]) / 2.0
            if (
                match is not None
                and 0.35 <= center / page.page_width <= 0.70
                and line.bbox[2] - line.bbox[0] >= page.page_width * 0.20
            ):
                found["part"] = (line, match.group("text").strip())
                consumed.add(_line_identity(line))
                break
        tab_lines = sorted(
            (line for line in candidates if line.bbox[0] / page.page_width >= 0.90),
            key=_line_order_key,
        )
        for index, ordinal in enumerate(tab_lines):
            if not _ROMAN_TAB_RE.fullmatch(ordinal.normalized_text):
                continue
            label_lines: list[ParserLine] = []
            previous_bottom = ordinal.bbox[3]
            for line in tab_lines[index + 1 :]:
                if line.bbox[1] < previous_bottom:
                    continue
                if line.bbox[1] - previous_bottom > page.page_height * 0.03:
                    break
                if _ROMAN_TAB_RE.fullmatch(line.normalized_text) or re.fullmatch(
                    r"[0-9]{1,4}", line.normalized_text
                ):
                    break
                label_lines.append(line)
                previous_bottom = line.bbox[3]
                if len(label_lines) == 3:
                    break
            if label_lines:
                found["domain"] = (
                    label_lines[0],
                    " ".join(line.normalized_text for line in label_lines),
                )
                consumed.update(
                    _line_identity(line) for line in (ordinal, *label_lines)
                )
                break
    for ordinal in candidates:
        if not _HEADING_ORDINAL_RE.fullmatch(ordinal.normalized_text):
            continue
        adjacent = [
            line
            for line in candidates
            if line is not ordinal
            and not _HEADING_ORDINAL_RE.fullmatch(line.normalized_text)
            and _same_visual_row(ordinal.bbox, line.bbox)
            and 0.0 <= line.bbox[0] - ordinal.bbox[2] <= page.page_width * 0.04
        ]
        if not adjacent:
            continue
        label = min(adjacent, key=lambda line: (line.bbox[0], _line_order_key(line)))
        left_ratio = ordinal.bbox[0] / page.page_width
        ordinal_height = ordinal.bbox[3] - ordinal.bbox[1]
        role: Literal["domain", "part", "subtopic"] | None = None
        if page.edition_year == 2024:
            if left_ratio >= 0.65:
                role = "domain"
            elif left_ratio <= 0.20 and ordinal_height >= page.page_height * 0.04:
                role = "part"
        elif left_ratio <= 0.20 and ordinal_height >= page.page_height * 0.04:
            role = "subtopic"
        if role is None or role in found:
            continue
        found[role] = (label, label.normalized_text)
        consumed.update({_line_identity(ordinal), _line_identity(label)})
    return found, consumed


def _is_right_navigation(page: ParserPage, line: ParserLine) -> bool:
    if page.page_width is None or page.page_height is None:
        return False
    if page.edition_year in (2024, 2025):
        return bool(
            (line.bbox[0] / page.page_width >= 0.90 and _metadata(line) is None)
            or (
                line.bbox[1] / page.page_height >= 0.92
                and re.fullmatch(r"[0-9]{1,4}", line.normalized_text)
            )
        )
    return bool(
        page.edition_year == 2020
        and line.bbox[0] / page.page_width >= 0.90
        and line.size <= 8.5
        and "편" in line.normalized_text
    )


def _native_qa_number_geometry(page: ParserPage, line: ParserLine) -> bool:
    """Return whether a native number has the reviewed QA number-box geometry."""
    if page.page_width is None or page.page_height is None:
        return False
    x0_ratio = line.bbox[0] / page.page_width
    width_ratio = (line.bbox[2] - line.bbox[0]) / page.page_width
    height_ratio = (line.bbox[3] - line.bbox[1]) / page.page_height
    if page.edition_year == 2020:
        return bool(
            0.10 <= x0_ratio <= 0.18
            and 0.015 <= width_ratio <= 0.04
            and 0.015 <= height_ratio <= 0.04
            and 14.0 <= line.size <= 20.0
        )
    if page.edition_year in (2021, 2022):
        return bool(
            0.11 <= x0_ratio <= 0.18
            and 0.01 <= width_ratio <= 0.045
            and 0.012 <= height_ratio <= 0.04
            and 14.5 <= line.size <= 17.5
        )
    return True


def _case_number_at(
    page: ParserPage, lines: Sequence[ParserLine], index: int
) -> str | None:
    line = lines[index]
    if match := _CASE_LABEL_RE.match(line.normalized_text):
        return match.group(1)
    if not re.fullmatch(r"[0-9]{1,4}(?:-[0-9]+)?", line.normalized_text):
        return None
    if page.edition_year == 2023:
        if (
            page.page_width is None
            or page.page_height is None
            or line.bbox[0] / page.page_width > 0.25
            or not 0.10 <= line.bbox[1] / page.page_height <= 0.85
            or not 0.03 <= (line.bbox[3] - line.bbox[1]) / page.page_height <= 0.08
        ):
            return None
        nearby_header = any(
            candidate.bbox[0] > line.bbox[2]
            and abs(candidate.bbox[1] - line.bbox[1]) <= page.page_height * 0.04
            and _NUMBERED_ROLE_RE.match(candidate.normalized_text) is None
            for candidate in lines[index + 1 : index + 4]
        )
        return line.normalized_text if nearby_header else None
    if page.edition_year in (2024, 2025):
        if page.layout_evidence.status != "detected":
            return None
        for region in page.layout_evidence.regions:
            region_bbox = (
                region.bbox.x0,
                region.bbox.y0,
                region.bbox.x1,
                region.bbox.y1,
            )
            width = region_bbox[2] - region_bbox[0]
            height = region_bbox[3] - region_bbox[1]
            if (
                _contains(region_bbox, line.bbox)
                and (line.bbox[0] - region_bbox[0]) / width <= 0.20
                and (line.bbox[1] - region_bbox[1]) / height <= 0.30
            ):
                return line.normalized_text
        return None
    if page.edition_year > 2022:
        return None
    if (
        page.page_width is None
        or line.bbox[0] / page.page_width > 0.35
        or not _native_qa_number_geometry(page, line)
    ):
        return None
    if line.size < 14.0:
        return None
    if page.edition_year == 2020:
        title_candidates = [
            candidate
            for candidate in lines
            if candidate is not line
            and candidate.bbox[0] > line.bbox[2]
            and _same_visual_row(line.bbox, candidate.bbox)
            and 9.0 <= candidate.size <= 12.0
            and not re.fullmatch(r"[0-9]{1,4}(?:-[0-9]+)?", candidate.normalized_text)
        ]
        if not title_candidates:
            return None
        return line.normalized_text
    title_candidates = [
        candidate
        for candidate in lines
        if candidate is not line
        and candidate.bbox[0] > line.bbox[2]
        and 0.0 <= (candidate.bbox[0] - line.bbox[2]) / page.page_width <= 0.08
        and _same_native_header_row(page, line.bbox, candidate.bbox)
        and 10.0 <= candidate.size <= 12.0
        and not re.fullmatch(r"[0-9]{1,4}(?:-[0-9]+)?", candidate.normalized_text)
    ]
    if not title_candidates:
        return None
    return line.normalized_text


def _native_audit_title_at(
    page: ParserPage, line: ParserLine
) -> tuple[str, str] | None:
    """Recognize only the reviewed native audit-title boundary geometry."""
    if (
        page.edition_year not in (2020, 2021, 2022)
        or page.page_width is None
        or page.page_height is None
    ):
        return None
    match = _AUDIT_TITLE_RE.fullmatch(line.normalized_text)
    if match is None:
        return None
    x0_ratio = line.bbox[0] / page.page_width
    width_ratio = (line.bbox[2] - line.bbox[0]) / page.page_width
    height_ratio = (line.bbox[3] - line.bbox[1]) / page.page_height
    if not (
        0.09 <= x0_ratio <= 0.12
        and 0.04 <= width_ratio <= 0.80
        and 0.01 <= height_ratio <= 0.04
        and (
            12.0 <= line.size <= 14.0
            if page.edition_year == 2020
            else 13.0 <= line.size <= 15.0
        )
    ):
        return None
    return match.group("number"), match.group("title").strip()


def _card_body_order_key(
    page: ParserPage, line: ParserLine
) -> tuple[float, float, float, float, int, int, int, str]:
    base = _line_order_key(line)
    if (
        page.edition_year <= 2022
        and page.page_width is not None
        and page.page_height is not None
        and re.fullmatch(r"[0-9]{1,4}(?:-[0-9]+)?", line.normalized_text)
        and line.bbox[0] / page.page_width <= 0.35
        and line.size >= 14.0
        and _native_qa_number_geometry(page, line)
    ):
        return (
            line.bbox[1] - page.page_height * 0.02,
            line.bbox[0],
            line.bbox[3],
            line.bbox[2],
            line.source_block_index,
            line.source_line_index,
            line.source_span_index,
            line.raw_text_sha256,
        )
    if (
        page.edition_year == 2023
        and page.page_width is not None
        and page.page_height is not None
        and re.fullmatch(r"[0-9]{1,4}", line.normalized_text)
        and line.bbox[0] / page.page_width <= 0.25
        and 0.10 <= line.bbox[1] / page.page_height <= 0.85
        and 0.03 <= (line.bbox[3] - line.bbox[1]) / page.page_height <= 0.08
    ):
        return (
            line.bbox[1] - page.page_height * 0.04,
            line.bbox[0],
            line.bbox[3],
            line.bbox[2],
            line.source_block_index,
            line.source_line_index,
            line.source_span_index,
            line.raw_text_sha256,
        )
    if page.edition_year not in (2024, 2025) or not re.fullmatch(
        r"[0-9]{1,4}(?:-[0-9]+)?", line.normalized_text
    ):
        return base
    for region in page.layout_evidence.regions:
        region_bbox = (
            region.bbox.x0,
            region.bbox.y0,
            region.bbox.x1,
            region.bbox.y1,
        )
        width = region_bbox[2] - region_bbox[0]
        height = region_bbox[3] - region_bbox[1]
        if (
            _contains(region_bbox, line.bbox)
            and (line.bbox[0] - region_bbox[0]) / width <= 0.20
            and (line.bbox[1] - region_bbox[1]) / height <= 0.30
        ):
            return (
                region_bbox[1] - 1.0,
                line.bbox[0],
                line.bbox[3],
                line.bbox[2],
                line.source_block_index,
                line.source_line_index,
                line.source_span_index,
                line.raw_text_sha256,
            )
    return base


def _hierarchy_copy(
    domain: str | None, part: str | None, subtopic: str | None
) -> HierarchyState:
    return HierarchyState(domain=domain, part=part, subtopic=subtopic)


def _quarantine(located: Sequence[tuple[ParserPage, ParserLine]]) -> BoundaryQuarantine:
    unique: dict[tuple[int, tuple[float, float, float, float], str], SourceSpan] = {}
    for page, line in located:
        span = _source_span(page, line)
        unique[(span.pdf_page_index, span.bbox, span.text_sha256)] = span
    spans = tuple(unique[key] for key in sorted(unique))
    if not spans:
        raise ParserContractError("ambiguous boundary lacks locatable provenance")
    seed = "|".join(
        f"{span.pdf_page_index}:{','.join(map(str, span.bbox))}:{span.text_sha256}"
        for span in spans
    )
    return BoundaryQuarantine(
        location_id=f"loc-{hashlib.sha256(seed.encode()).hexdigest()[:32]}",
        page_ids=tuple(sorted({span.pdf_page_index for span in spans})),
        source_spans=spans,
        span_count=len(spans),
    )


def _upstream_quarantine(page: ParserPage) -> UpstreamPageQuarantine:
    reason = page.upstream_reason_code
    if reason not in {
        "page-extraction-failed",
        "page-render-failed",
        "ocr-adapter-failed",
        "ocr-provenance-invalid",
    }:
        raise ParserContractError("upstream page quarantine reason is invalid")
    seed = (
        f"{page.edition_year}:{page.pdf_page_index}:"
        f"{page.extraction_source}:{reason}:{page.render_sha256 or 'none'}"
    )
    return UpstreamPageQuarantine(
        location_id=f"loc-{hashlib.sha256(seed.encode()).hexdigest()[:32]}",
        reason_code=cast(
            Literal[
                "page-extraction-failed",
                "page-render-failed",
                "ocr-adapter-failed",
                "ocr-provenance-invalid",
            ],
            reason,
        ),
        page_ids=(page.pdf_page_index,),
    )


def _ordinal_pairs_are_valid(fragments: Sequence[RoleFragment]) -> bool:
    questions = [
        (i, f.ordinal)
        for i, f in enumerate(fragments)
        if f.role == "question" and not f.continuation and f.ordinal is not None
    ]
    answers = [
        (i, f.ordinal)
        for i, f in enumerate(fragments)
        if f.role == "answer" and not f.continuation and f.ordinal is not None
    ]
    if not questions and not answers:
        return True
    q_values = [value for _, value in questions]
    a_values = [value for _, value in answers]
    if not questions:
        unnumbered_questions = [
            fragment
            for fragment in fragments
            if fragment.role == "question"
            and not fragment.continuation
            and fragment.ordinal is None
        ]
        return bool(
            len(unnumbered_questions) == 1
            and len(a_values) == len(set(a_values))
            and a_values == list(range(1, len(a_values) + 1))
        )
    if (
        len(q_values) != len(set(q_values))
        or len(a_values) != len(set(a_values))
        or q_values != a_values
    ):
        return False
    positions = {value: index for index, value in questions}
    return all(positions[value] < index for index, value in answers)


def _2022_role_phases_are_valid(opened: _OpenCase) -> bool:
    """Validate measured Q-band then A-band phase order, not list ordinals."""
    lines_by_location = {
        (page.pdf_page_index, line.bbox, line.raw_text_sha256): (page, line)
        for page, line in opened.lines
    }
    seen_question = False
    seen_answer = False
    explicit_numbered: list[RoleFragment] = []
    for fragment in opened.fragments:
        if fragment.role not in {"question", "answer"}:
            continue
        if fragment.role == "question":
            if seen_answer:
                return False
            seen_question = True
        else:
            if not seen_question:
                return False
            first_answer = not seen_answer
            seen_answer = True
        if fragment.continuation:
            continue
        location = (
            fragment.source_span.pdf_page_index,
            fragment.source_span.bbox,
            fragment.source_span.text_sha256,
        )
        located = lines_by_location.get(location)
        if located is None:
            return False
        page, line = located
        explicit_role = (
            _QUESTION_RE.match(line.normalized_text)
            if fragment.role == "question"
            else _ANSWER_RE.match(line.normalized_text)
        )
        if explicit_role is not None or line.semantic_hint == fragment.role:
            if fragment.ordinal is not None:
                explicit_numbered.append(fragment)
            continue
        if page.page_width is None:
            return False
        x_ratio = fragment.source_span.bbox[0] / page.page_width
        if fragment.role == "question" and x_ratio < 0.19:
            return False
        if fragment.role == "answer" and first_answer and x_ratio > 0.17:
            return False
    return bool(
        seen_question
        and seen_answer
        and (not explicit_numbered or _ordinal_pairs_are_valid(explicit_numbered))
    )


def _joined(
    fragments: Sequence[RoleFragment], role: FragmentRole, *, inline: bool = False
) -> str | None:
    values = [fragment.text for fragment in fragments if fragment.role == role]
    return (" " if inline else "\n").join(values) if values else None


def _finalize(opened: _OpenCase) -> ParsedCaseCandidate | BoundaryQuarantine:
    if opened.force_quarantine:
        return _quarantine(opened.lines)
    if not opened.domain or not opened.part:
        return _quarantine(opened.lines)
    if opened.edition_year in (2024, 2025) and any(
        page.layout_evidence.status != "detected"
        or not any(
            region.region_type == "card" and region.evidence == "raster-border"
            for region in page.layout_evidence.regions
        )
        for page, _ in opened.lines
    ):
        return _quarantine(opened.lines)
    title_fragments = [
        fragment for fragment in opened.fragments if fragment.role == "title"
    ]
    if not title_fragments:
        return _quarantine(opened.lines)
    if len(title_fragments) > 1:
        if not all(
            _same_visual_row(
                title_fragments[0].source_span.bbox,
                fragment.source_span.bbox,
            )
            for fragment in title_fragments[1:]
        ):
            return _quarantine(opened.lines)
        if opened.edition_year in (2021, 2022):
            lines_by_location = {
                (page.pdf_page_index, line.bbox, line.raw_text_sha256): line
                for page, line in opened.lines
            }
            ordered_titles = sorted(
                title_fragments,
                key=lambda fragment: (
                    fragment.source_span.pdf_page_index,
                    fragment.source_span.bbox[0],
                    fragment.source_span.bbox[1],
                    fragment.source_span.text_sha256,
                ),
            )
            title_lines = [
                lines_by_location.get(
                    (
                        fragment.source_span.pdf_page_index,
                        fragment.source_span.bbox,
                        fragment.source_span.text_sha256,
                    )
                )
                for fragment in ordered_titles
            ]
            if (
                any(line is None for line in title_lines)
                or len(
                    {
                        (line.source_block_index, line.source_line_index)
                        for line in title_lines
                        if line is not None
                    }
                )
                != 1
                or any(
                    not 0.0
                    <= right.source_span.bbox[0] - left.source_span.bbox[2]
                    <= 1.2
                    for left, right in pairwise(ordered_titles)
                )
            ):
                return _quarantine(opened.lines)
            opened.fragments[:] = ordered_titles + [
                fragment for fragment in opened.fragments if fragment.role != "title"
            ]
            title_fragments = ordered_titles
        elif opened.edition_year not in (2023, 2024, 2025):
            return _quarantine(opened.lines)
    if (
        (
            opened.edition_year == 2020
            or (opened.edition_year == 2021 and opened.title_question_fallback)
        )
        and opened.case_type == "qa"
        and not any(fragment.role == "question" for fragment in opened.fragments)
    ):
        fallback_fragments = [
            fragment.model_copy(update={"role": "question"})
            for fragment in title_fragments
        ]
        if opened.edition_year in (2021, 2022):
            insertion = (
                max(
                    index
                    for index, fragment in enumerate(opened.fragments)
                    if fragment.role == "title"
                )
                + 1
            )
            opened.fragments[insertion:insertion] = fallback_fragments
        else:
            opened.fragments.extend(fallback_fragments)
    roles = {fragment.role for fragment in opened.fragments}
    required_roles = (
        {"facts", "answer"} if opened.case_type == "audit" else {"question", "answer"}
    )
    if not required_roles.issubset(roles):
        return _quarantine(opened.lines)
    if opened.case_type != "audit":
        roles_are_valid = (
            _2022_role_phases_are_valid(opened)
            if opened.edition_year == 2022
            else _ordinal_pairs_are_valid(opened.fragments)
        )
        if not roles_are_valid:
            return _quarantine(opened.lines)
    layout_by_page = {
        page.pdf_page_index: page.layout_segment_provenance
        for page, _ in opened.lines
        if page.layout_segment_provenance is not None
    }
    layout_provenances = tuple(
        layout_by_page[index] for index in sorted(layout_by_page)
    )
    if opened.edition_year in (2024, 2025):
        parsed_pages = {page.pdf_page_index for page, _ in opened.lines}
        if set(layout_by_page) != parsed_pages:
            return _quarantine(opened.lines)
        registry_bindings = {
            (
                item.segment_id,
                item.segment_key,
                item.segment_start_pdf_page,
                item.segment_end_pdf_page,
                item.registry_policy_version,
                item.registry_sha256,
                item.detector_version,
                item.sampling_status,
                item.doc_id,
                item.edition_year,
                item.source_sha256,
            )
            for item in layout_provenances
        }
        if len(registry_bindings) != 1:
            return _quarantine(opened.lines)
    source_spans: list[SourceSpan] = []
    seen: set[tuple[int, tuple[float, float, float, float], str]] = set()
    for page, line in sorted(
        opened.lines,
        key=lambda item: (item[0].pdf_page_index, _line_order_key(item[1])),
    ):
        span = _source_span(page, line)
        key = (span.pdf_page_index, span.bbox, span.text_sha256)
        if key not in seen:
            seen.add(key)
            source_spans.append(span)
    year = opened.edition_year
    critical: CriticalReview = (
        "not_applicable"
        if year <= 2022
        else "unverified"
        if year <= 2024
        else "sampling_required"
    )
    upstream_review_status: ReviewStatus = (
        "needs_review"
        if any(
            page.upstream_review_status == "needs_review" for page, _ in opened.lines
        )
        else "machine_extracted"
    )
    review: ReviewStatus = (
        "needs_review"
        if year in (2023, 2024) or upstream_review_status == "needs_review"
        else "machine_extracted"
    )
    return ParsedCaseCandidate(
        doc_id=opened.doc_id,
        edition_year=year,
        case_type=opened.case_type,
        domain=opened.domain,
        part=opened.part,
        subtopic=opened.subtopic,
        case_no=opened.case_no,
        fragments=tuple(opened.fragments),
        title=_joined(opened.fragments, "title", inline=True) or "",
        question=_joined(
            opened.fragments,
            "question",
            inline=opened.edition_year in (2023, 2024, 2025),
        ),
        answer=_joined(opened.fragments, "answer"),
        facts=_joined(opened.fragments, "facts"),
        basis_text=_joined(opened.fragments, "basis"),
        target_text=_joined(opened.fragments, "target"),
        situation_text=_joined(opened.fragments, "situation"),
        source_spans=tuple(source_spans),
        extraction_source=opened.extraction_source,
        extraction_confidence=min(line.confidence for _, line in opened.lines),
        layout_segment_id=(
            layout_provenances[0].segment_id if layout_provenances else None
        ),
        layout_segment_provenances=layout_provenances,
        upstream_review_status=upstream_review_status,
        critical_field_review=critical,
        review_status=review,
    )


def _has_confirmed_card_close(page: ParserPage, opened: _OpenCase) -> bool:
    if page.layout_evidence.status != "detected":
        return False
    current = [
        line
        for source_page, line in opened.lines
        if source_page.pdf_page_index == page.pdf_page_index
    ]
    if not current:
        return False
    regions = [
        (region.bbox.x0, region.bbox.y0, region.bbox.x1, region.bbox.y1)
        for region in page.layout_evidence.regions
        if region.region_type == "card" and region.evidence == "raster-border"
    ]
    fragment_roles = {
        (
            fragment.source_span.pdf_page_index,
            fragment.source_span.bbox,
            fragment.source_span.text_sha256,
        ): fragment.role
        for fragment in opened.fragments
    }
    for region in regions:
        trailing_limit = (
            region[3] + page.page_height * 0.10
            if page.page_height is not None
            else region[3]
        )
        accepted = True
        for line in current:
            if _contains(region, line.bbox):
                continue
            role = fragment_roles.get(
                (page.pdf_page_index, line.bbox, line.raw_text_sha256)
            )
            if not (
                role == "basis"
                and line.bbox[1] >= region[3]
                and line.bbox[1] <= trailing_limit
                and line.bbox[0] >= region[0]
                and line.bbox[2] <= region[2]
            ):
                accepted = False
                break
        if accepted:
            return True
    return False


def _can_close_at_page_end(page: ParserPage, opened: _OpenCase) -> bool:
    if page.edition_year <= 2023:
        return False
    if opened.force_quarantine:
        return False
    if not opened.fragments:
        return False
    last = opened.fragments[-1]
    terminal = bool(_SENTENCE_END_RE.search(last.text))
    return terminal and _has_confirmed_card_close(page, opened)


def _validate_pages(pages: Sequence[ParserPage], edition_year: int) -> None:
    if not pages:
        return
    if any(not _is_revalidated_parser_page(page) for page in pages):
        raise ParserContractError("parser page contract is invalid")
    if any(page.edition_year != edition_year for page in pages):
        raise ParserContractError("pages do not match requested edition")
    if (
        len({page.doc_id for page in pages}) != 1
        or len({page.extraction_source for page in pages}) != 1
    ):
        raise ParserContractError("pages must belong to a single document and source")
    source_sha256s = {page.source_sha256 for page in pages}
    if None in source_sha256s or len(source_sha256s) != 1:
        raise ParserContractError("pages must share one non-null source SHA")
    indexes = [page.pdf_page_index for page in pages]
    if indexes != sorted(set(indexes)):
        raise ParserContractError("page indexes must be strictly monotonic")
    if any(right != left + 1 for left, right in pairwise(indexes)):
        raise ParserContractError(
            "page indexes must be contiguous or explicitly quarantined"
        )
    expected_source = "native" if edition_year <= 2022 else "ocr"
    if pages[0].extraction_source != expected_source:
        raise ParserContractError("edition extraction source does not match policy")


def _is_revalidated_parser_page(value: object) -> bool:
    if type(value) is not ParserPage:
        return False
    try:
        fields = {name: getattr(value, name) for name in ParserPage.model_fields}
        rebuilt = ParserPage.model_validate(fields)
    except (TypeError, ValueError, AttributeError):
        return False
    return rebuilt == value


def parse_pages(pages: Sequence[ParserPage], *, edition_year: int) -> ParseResult:
    """Parse one complete, ordered document slice using annual fail-closed policy."""
    _validate_pages(pages, edition_year)
    domain: str | None = None
    part: str | None = None
    subtopic: str | None = None
    case_type_mode: Literal["qa", "audit"] = "qa"
    opened: _OpenCase | None = None
    cases: list[ParsedCaseCandidate] = []
    quarantines: list[ParserQuarantine] = []
    transitions: list[MetadataTransition] = []
    pending_unclaimed_2022: list[tuple[ParserPage, ParserLine]] = []

    def close_open() -> None:
        nonlocal opened
        if opened is None:
            return
        result = _finalize(opened)
        if isinstance(result, BoundaryQuarantine):
            quarantines.append(result)
        else:
            cases.append(result)
        opened = None

    def quarantine_pending_2022() -> None:
        if not pending_unclaimed_2022:
            return
        quarantines.append(_quarantine(pending_unclaimed_2022))
        pending_unclaimed_2022.clear()

    for page in pages:
        if page.page_status == "quarantined":
            if opened is not None:
                quarantines.append(_quarantine(opened.lines))
                opened = None
            quarantine_pending_2022()
            quarantines.append(_upstream_quarantine(page))
            continue
        if page.page_role_hint is None:
            raise ParserContractError(
                "extracted page requires an explicit verified role"
            )
        role = page.page_role_hint
        if role != "body":
            close_open()
            quarantine_pending_2022()
            if page.lines:
                span = _source_span(page, page.lines[0])
                state = _hierarchy_copy(domain, part, subtopic)
                transitions.append(
                    MetadataTransition(
                        pdf_page_index=page.pdf_page_index,
                        role=role,
                        value=None,
                        source_span=span,
                        hierarchy_before=state,
                        hierarchy_after=state,
                    )
                )
            continue

        actual_found, actual_metadata_lines = _actual_ocr_hierarchy(page, page.lines)
        usable = [line for line in page.lines if not _is_right_navigation(page, line)]
        by_bbox: dict[tuple[float, float, float, float], str] = {}
        conflict = False
        for line in usable:
            previous = by_bbox.setdefault(line.bbox, line.raw_text_sha256)
            if previous != line.raw_text_sha256:
                conflict = True
        if conflict:
            quarantine_pending_2022()
            located = ([] if opened is None else opened.lines) + [
                (page, line) for line in usable
            ]
            quarantines.append(_quarantine(located))
            opened = None
            continue

        found = actual_found
        metadata_lines = actual_metadata_lines
        for line in usable:
            item = _metadata(line)
            if item:
                found[item[0]] = (line, item[1])
                metadata_lines.add(_line_identity(line))
        if edition_year == 2020 and "part" in found and "domain" not in found:
            found["domain"] = found["part"]
        new_part = found.get("part", (None, part))[1]
        if new_part != part:
            close_open()
            quarantine_pending_2022()
            case_type_mode = "qa"
        for metadata_role in ("domain", "part", "subtopic"):
            if metadata_role not in found:
                continue
            line, value = found[metadata_role]
            before = _hierarchy_copy(domain, part, subtopic)
            if metadata_role == "domain":
                domain = value
            elif metadata_role == "part":
                part = value
            else:
                subtopic = value
            after = _hierarchy_copy(domain, part, subtopic)
            transitions.append(
                MetadataTransition(
                    pdf_page_index=page.pdf_page_index,
                    role=metadata_role,
                    value=value,
                    source_span=_source_span(page, line),
                    hierarchy_before=before,
                    hierarchy_after=after,
                )
            )

        body = [line for line in usable if _line_identity(line) not in metadata_lines]
        body.sort(key=lambda line: _card_body_order_key(page, line))
        discard_page = False
        index = 0
        while index < len(body):
            line = body[index]
            if edition_year <= 2022 and _AUDIT_RUN_RE.fullmatch(line.normalized_text):
                close_open()
                quarantine_pending_2022()
                case_type_mode = "audit"
                if edition_year <= 2022:
                    # Native audit pages do not expose a trustworthy facts/answer
                    # split.  Preserve the run marker and any pre-title body in a
                    # locatable quarantine instead of silently dropping it.
                    opened = _OpenCase(
                        doc_id=page.doc_id,
                        edition_year=edition_year,
                        extraction_source=page.extraction_source,
                        domain=domain,
                        part=part,
                        subtopic=subtopic,
                        case_no="0",
                        case_type="audit",
                        upstream_review_status=page.upstream_review_status,
                        lines=[(page, line)],
                        force_quarantine=True,
                    )
                index += 1
                continue
            audit_title = _native_audit_title_at(page, line)
            if audit_title is not None and (
                edition_year in (2020, 2022) or case_type_mode == "audit"
            ):
                close_open()
                audit_case_no, audit_title_text = audit_title
                audit_lines = pending_unclaimed_2022 + [(page, line)]
                pending_unclaimed_2022.clear()
                opened = _OpenCase(
                    doc_id=page.doc_id,
                    edition_year=edition_year,
                    extraction_source=page.extraction_source,
                    domain=domain,
                    part=part,
                    subtopic=subtopic,
                    case_no=audit_case_no,
                    case_type="audit",
                    upstream_review_status=page.upstream_review_status,
                    lines=audit_lines,
                    fragments=[
                        RoleFragment(
                            role="title",
                            ordinal=None,
                            text=audit_title_text,
                            source_span=_source_span(page, line),
                            confidence=line.confidence,
                            continuation=False,
                        )
                    ],
                    force_quarantine=True,
                )
                index += 1
                continue
            case_no = _case_number_at(page, body, index)
            if case_no is not None:
                if opened is not None and any(
                    source_page.pdf_page_index == page.pdf_page_index
                    and _bbox_overlap(source_line.bbox, line.bbox)
                    for source_page, source_line in opened.lines
                ):
                    quarantines.append(
                        _quarantine(
                            opened.lines + [(page, item) for item in body[index:]]
                        )
                    )
                    opened = None
                    discard_page = True
                    break
                close_open()
                pending_unclaimed_2022.clear()
                opened = _OpenCase(
                    doc_id=page.doc_id,
                    edition_year=edition_year,
                    extraction_source=page.extraction_source,
                    domain=domain,
                    part=part,
                    subtopic=subtopic,
                    case_no=case_no,
                    case_type=case_type_mode,
                    upstream_review_status=page.upstream_review_status,
                    lines=[(page, line)],
                )
                index += 1
                continue
            if opened is None:
                if edition_year == 2022:
                    pending_unclaimed_2022.append((page, line))
                index += 1
                continue
            if line.normalized_text == "사례 유형: 감사":
                opened.case_type = "audit"
                opened.lines.append((page, line))
                index += 1
                continue
            if edition_year == 2020 and _BULLET_ONLY_RE.fullmatch(line.normalized_text):
                opened.implicit_role = "answer"
                opened.lines.append((page, line))
                index += 1
                continue
            if line.normalized_text in {"관련 근거", "관련근거", "참고자료"}:
                opened.implicit_role = "basis"
                opened.lines.append((page, line))
                index += 1
                continue
            numbered_role = _NUMBERED_ROLE_RE.match(line.normalized_text)
            if numbered_role is not None:
                role_name = numbered_role.group("role")
                numbered_roles: dict[str, FragmentRole] = {
                    "질문": "question",
                    "답변": "answer",
                    "근거": "basis",
                    "참고자료": "basis",
                    "대상": "target",
                    "상황": "situation",
                }
                opened.implicit_role = numbered_roles[role_name]
                opened.lines.append((page, line))
                index += 1
                continue
            parsed_role = _line_role(page, line, opened)
            if parsed_role is None and opened.implicit_role is not None:
                parsed_role = (
                    opened.implicit_role,
                    None,
                    line.normalized_text,
                    True,
                )
            if parsed_role is not None:
                fragment_role, ordinal, text_value, continuation = parsed_role
                if (
                    edition_year == 2021
                    and fragment_role == "answer"
                    and ordinal is None
                    and not continuation
                    and not opened.question_fallback_blocked
                    and any(fragment.role == "title" for fragment in opened.fragments)
                    and not any(
                        fragment.role in {"question", "answer"}
                        for fragment in opened.fragments
                    )
                    and page.page_width is not None
                    and line.bbox[0] / page.page_width <= 0.17
                ):
                    opened.title_question_fallback = True
                if edition_year in (2021, 2022) and fragment_role in {
                    "question",
                    "answer",
                }:
                    opened.implicit_role = fragment_role
                elif not continuation:
                    opened.implicit_role = None
                opened.lines.append((page, line))
                opened.fragments.append(
                    RoleFragment(
                        role=fragment_role,
                        ordinal=ordinal,
                        text=text_value,
                        source_span=_source_span(page, line),
                        confidence=line.confidence,
                        continuation=continuation,
                    )
                )
            elif opened.force_quarantine:
                opened.lines.append((page, line))
            elif any(
                fragment.role == "title" for fragment in opened.fragments
            ) and not any(
                fragment.role in {"question", "answer"} for fragment in opened.fragments
            ):
                opened.question_fallback_blocked = True
            index += 1
        if discard_page:
            continue
        if opened is not None and _can_close_at_page_end(page, opened):
            close_open()

    close_open()
    quarantine_pending_2022()
    return ParseResult(
        cases=tuple(cases),
        quarantines=tuple(quarantines),
        transitions=tuple(transitions),
    )


def _validated_native_record(record: object) -> NativePageRecord:
    try:
        checked = validate_native_page_record(record)
    except NativeExtractionError:
        pass
    else:
        return checked
    raise ParserContractError("native page record is invalid")


def _validated_ocr_record(record: object) -> OcrPageRecord:
    try:
        checked = validate_ocr_page_record(record)
    except OcrExtractionError:
        pass
    else:
        return checked
    raise ParserContractError("OCR page record is invalid")


def _parser_layout_segment(
    record: ExtractedOcrPageRecord,
) -> LayoutSegmentProvenance | None:
    segment = record.layout_segment_provenance
    if segment is None:
        return None
    segment_year: Literal[2024, 2025]
    if record.edition_year == 2024:
        segment_year = 2024
    elif record.edition_year == 2025:
        segment_year = 2025
    else:
        raise ParserContractError("layout segment edition is invalid")
    return LayoutSegmentProvenance(
        segment_id=segment.segment_id,
        segment_key=segment.segment_key,
        segment_start_pdf_page=segment.segment_start_pdf_page,
        segment_end_pdf_page=segment.segment_end_pdf_page,
        registry_policy_version=segment.registry_policy_version,
        registry_sha256=segment.registry_sha256,
        detector_version=segment.detector_version,
        region_count=segment.region_count,
        sampling_status=segment.sampling_status,
        doc_id=record.doc_id,
        edition_year=segment_year,
        source_sha256=record.source_sha256,
        pdf_page_index=record.pdf_page_index,
        render_sha256=record.render_sha256,
    )


def parser_page_from_native_record(
    record: object,
    *,
    page_role_policy: VerifiedPageRolePolicy | None = None,
) -> ParserPage:
    """Adapt a native extractor record while enforcing its bound raw projection."""
    checked = _validated_native_record(record)
    if isinstance(checked, QuarantinedPageRecord):
        role = (
            _policy_role_for_envelope(
                page_role_policy,
                doc_id=checked.doc_id,
                edition_year=checked.edition_year,
                extraction_source="native",
                pdf_page_index=checked.pdf_page_index,
                source_sha256=checked.source_sha256,
                document_pdf_page_count=checked.document_pdf_page_count,
            )
            if page_role_policy is not None
            else None
        )
        return ParserPage(
            doc_id=checked.doc_id,
            edition_year=checked.edition_year,
            extraction_source="native",
            source_sha256=checked.source_sha256,
            pdf_page_index=checked.pdf_page_index,
            page_label=checked.page_label,
            page_width=None,
            page_height=None,
            render_sha256=None,
            lines=(),
            page_status="quarantined",
            page_role_hint=role,
            upstream_review_status="needs_review",
            critical_review_policy="not_applicable",
            layout_evidence=LayoutEvidence(),
            upstream_reason_code=checked.reason_code,
        )
    if not isinstance(checked, ExtractedPageRecord):
        raise ParserContractError("native page record status is invalid")
    if page_role_policy is not None:
        _policy_role_for_envelope(
            page_role_policy,
            doc_id=checked.doc_id,
            edition_year=checked.edition_year,
            extraction_source="native",
            pdf_page_index=checked.pdf_page_index,
            source_sha256=checked.source_sha256,
            document_pdf_page_count=checked.document_pdf_page_count,
        )
    return parser_page_from_raw_page(
        checked.raw_page,
        normalized_text=checked.normalized_text,
        page_role_policy=page_role_policy,
        retained_raw_block_indexes=checked.retained_raw_block_indexes,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
        source_sha256=checked.source_sha256,
    )


def parser_page_from_ocr_record(
    record: object,
    *,
    page_role_policy: VerifiedPageRolePolicy | None = None,
) -> ParserPage:
    """Adapt an OCR extractor record without inventing missing card evidence."""
    checked = _validated_ocr_record(record)
    if isinstance(checked, QuarantinedOcrPageRecord):
        role = (
            _policy_role_for_envelope(
                page_role_policy,
                doc_id=checked.doc_id,
                edition_year=checked.edition_year,
                extraction_source="ocr",
                pdf_page_index=checked.pdf_page_index,
                source_sha256=checked.source_sha256,
            )
            if page_role_policy is not None
            else None
        )
        policy: Literal[
            "all-fields-human-verification",
            "stratified-sample-with-layout-escalation",
        ] = (
            "stratified-sample-with-layout-escalation"
            if checked.edition_year == 2025
            else "all-fields-human-verification"
        )
        return ParserPage(
            doc_id=checked.doc_id,
            edition_year=checked.edition_year,
            extraction_source="ocr",
            source_sha256=checked.source_sha256,
            pdf_page_index=checked.pdf_page_index,
            page_label=checked.page_label,
            page_width=None,
            page_height=None,
            render_sha256=checked.render_sha256,
            lines=(),
            page_status="quarantined",
            page_role_hint=role,
            quality_flags=checked.quality_flags,
            upstream_review_status="needs_review",
            critical_review_policy=policy,
            layout_evidence=LayoutEvidence(status="unavailable"),
            upstream_reason_code=checked.reason_code,
        )
    if not isinstance(checked, ExtractedOcrPageRecord):
        raise ParserContractError("OCR page record status is invalid")
    critical = tuple(
        f"{item.field_type}:{item.status}" for item in checked.critical_fields
    )
    return parser_page_from_raw_page(
        checked.raw_page,
        normalized_text=None,
        page_role_policy=page_role_policy,
        quality_flags=checked.quality_flags,
        upstream_review_status=checked.review_status,
        critical_review_policy=checked.critical_review_policy,
        critical_fields=critical,
        layout_segment_provenance=_parser_layout_segment(checked),
        source_sha256=checked.source_sha256,
    )
