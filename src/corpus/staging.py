"""Fail-closed bridge from parsed candidates to the independent review store."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, NoReturn, cast

from src.corpus.chunking import ChunkRole, RoleSource, role_source_manifest_bytes
from src.corpus.ids import make_case_id
from src.corpus.models import Case, Document, DocumentPageCounts, SourceSpan
from src.corpus.storage import (
    VerifiedPromotionEnvelope,
    load_promotion_envelope,
    load_review_decision_snapshot,
)
from src.ingestion.extract_common import revalidate_source_document
from src.ingestion.manifest import (
    NativeReviewLayoutSegment,
    SourceDocument,
    SourceManifest,
    load_manifest,
)
from src.ingestion.ocr_authority import (
    OcrAuthorityEntry,
    OcrAuthorityLock,
    OcrAuthorityLockError,
    canonical_ocr_authority_bytes,
    load_ocr_authority_lock,
)
from src.ingestion.parse_common import (
    BoundaryQuarantine,
    LayoutSegmentProvenance,
    ParsedCaseCandidate,
    ParseResult,
    ParserLine,
    ParserPage,
    ParserQuarantine,
    RoleFragment,
    UpstreamPageQuarantine,
    canonical_result_bytes,
)
from src.ingestion.parse_metadata import VerifiedParseRun, build_parse_run
from src.ingestion.privacy import classify_privacy, scan_text
from src.ingestion.quality import QualityAssessment, QualityFinding, assess_case
from src.ingestion.quarantine_review import (
    VerifiedQuarantineResolutionAuthority,
    load_resolution_authority,
    reparse_with_resolution,
)
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewError,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    VerifiedCanonicalReviewRegistry,
)
from src.ingestion.review_sampling import SamplingCandidate, build_sampling_authority

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_INGESTION_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MAX_CASES = 10_000
_MAX_LINES = 250_000
_MAX_REVIEW_FILE_BYTES = 16 * 1024 * 1024


class StagingError(ValueError):
    """A value-free corpus review staging failure."""


class _DuplicateJsonKey(ValueError):
    pass


def _raise(code: str) -> NoReturn:
    raise StagingError(code) from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise _DuplicateJsonKey
        output[key] = value
    return output


def _json_object(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return None
    return value if type(value) is dict else None


def _canonical_json_bytes(payload: object) -> bytes | None:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError, UnicodeError):
        return None


@dataclass(frozen=True, slots=True, init=False)
class PreparedReviewBatch:
    """Sealed, internally derived inputs for one review package."""

    documents: tuple[Document, ...]
    source_documents: tuple[SourceDocument, ...]
    cases: tuple[Case, ...]
    envelopes: tuple[VerifiedPromotionEnvelope, ...]
    assessments: tuple[QualityAssessment, ...]
    sampling_candidates: tuple[SamplingCandidate, ...]
    registry: VerifiedCanonicalReviewRegistry
    parser_authority_sha256: str
    raw_authority_sha256: str
    manifest_sha256: str
    manifest_bytes: bytes
    ocr_authority_lock: OcrAuthorityLock | None
    ocr_authority_lock_bytes: bytes | None
    ocr_authority_lock_sha256: str | None
    ocr_authority_self_sha256: str | None
    document_page_counts: dict[str, DocumentPageCounts]
    parser_quarantines_bytes: bytes
    parser_quarantines_sha256: str
    quarantine_count: int
    resolution_authority: VerifiedQuarantineResolutionAuthority | None
    resolution_authority_bytes: bytes | None
    resolution_authority_sha256: str | None


@dataclass(frozen=True, slots=True)
class _QuarantineOnlyUnit:
    document: Document
    source_document: SourceDocument
    page_count: DocumentPageCounts
    parser_quarantines_bytes: bytes
    quarantine_count: int


def assign_native_review_segment(
    document: SourceDocument,
    candidate: ParsedCaseCandidate,
) -> NativeReviewLayoutSegment:
    """Resolve every native source span to one explicit manifest segment."""
    if (
        type(document) is not SourceDocument
        or type(candidate) is not ParsedCaseCandidate
        or document.extraction_method != "native"
        or candidate.extraction_source != "native"
        or candidate.doc_id != document.doc_id
        or candidate.edition_year != document.edition_year
        or not candidate.source_spans
    ):
        _raise("staging_input_invalid")
    resolved: list[NativeReviewLayoutSegment] = []
    for span in candidate.source_spans:
        matches = tuple(
            segment
            for segment in document.native_review_layout_segments
            if segment.start_pdf_page <= span.pdf_page_index <= segment.end_pdf_page
        )
        if len(matches) != 1:
            _raise("staging_input_invalid")
        resolved.append(matches[0])
    if len({segment.segment_id for segment in resolved}) != 1:
        _raise("staging_input_invalid")
    return resolved[0]


def _exact_fields(
    value: object, expected_type: type[object]
) -> dict[str, object] | None:
    if type(value) is not expected_type:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
        model_fields = type.__getattribute__(expected_type, "model_fields")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict or set(fields) != set(model_fields):
        return None
    return dict(fields)


def _revalidate_span(value: object) -> SourceSpan | None:
    fields = _exact_fields(value, SourceSpan)
    if fields is None:
        return None
    try:
        return SourceSpan.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_fragment(value: object) -> RoleFragment | None:
    fields = _exact_fields(value, RoleFragment)
    if fields is None:
        return None
    span = _revalidate_span(fields.get("source_span"))
    if span is None:
        return None
    fields["source_span"] = span
    try:
        return RoleFragment.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_candidate(value: object) -> ParsedCaseCandidate | None:
    fields = _exact_fields(value, ParsedCaseCandidate)
    if fields is None:
        return None
    raw_fragments = fields.get("fragments")
    raw_spans = fields.get("source_spans")
    if type(raw_fragments) is not tuple or type(raw_spans) is not tuple:
        return None
    fragments = tuple(_revalidate_fragment(item) for item in raw_fragments)
    spans = tuple(_revalidate_span(item) for item in raw_spans)
    if any(item is None for item in (*fragments, *spans)):
        return None
    fields["fragments"] = fragments
    fields["source_spans"] = spans
    try:
        return ParsedCaseCandidate.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_sampling_candidate(value: object) -> SamplingCandidate | None:
    if type(value) is not SamplingCandidate:
        return None
    try:
        fields = {
            field.name: object.__getattribute__(value, field.name)
            for field in dataclass_fields(SamplingCandidate)
        }
    except (AttributeError, TypeError):
        return None
    if set(fields) != {
        "reference",
        "edition_year",
        "extraction_source",
        "pii_class",
        "review_status",
        "layout_segment_provenances",
        "native_layout_segment",
        "doc_id",
        "source_sha256",
        "quarantined",
    }:
        return None
    raw_provenances = fields["layout_segment_provenances"]
    if type(raw_provenances) is not tuple:
        return None
    provenances: list[LayoutSegmentProvenance] = []
    for raw_provenance in raw_provenances:
        provenance_fields = _exact_fields(raw_provenance, LayoutSegmentProvenance)
        if provenance_fields is None:
            return None
        try:
            provenances.append(
                LayoutSegmentProvenance.model_validate(provenance_fields)
            )
        except (TypeError, ValueError):
            return None
    raw_native = fields["native_layout_segment"]
    native: NativeReviewLayoutSegment | None = None
    if raw_native is not None:
        native_fields = _exact_fields(raw_native, NativeReviewLayoutSegment)
        if native_fields is None:
            return None
        try:
            native = NativeReviewLayoutSegment.model_validate(native_fields)
        except (TypeError, ValueError):
            return None
    try:
        return SamplingCandidate(
            reference=cast(ReviewReference, fields["reference"]),
            edition_year=cast(int, fields["edition_year"]),
            extraction_source=cast(Any, fields["extraction_source"]),
            pii_class=cast(Any, fields["pii_class"]),
            review_status=cast(Any, fields["review_status"]),
            layout_segment_provenances=tuple(provenances),
            native_layout_segment=native,
            doc_id=cast(str | None, fields["doc_id"]),
            source_sha256=cast(str | None, fields["source_sha256"]),
            quarantined=cast(bool, fields["quarantined"]),
        )
    except (TypeError, ValueError):
        return None


def _revalidate_line(value: object) -> ParserLine | None:
    fields = _exact_fields(value, ParserLine)
    if fields is None:
        return None
    try:
        return ParserLine.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_page(value: object) -> ParserPage | None:
    fields = _exact_fields(value, ParserPage)
    if fields is None:
        return None
    raw_lines = fields.get("lines")
    if type(raw_lines) is not tuple:
        return None
    lines = tuple(_revalidate_line(item) for item in raw_lines)
    if any(item is None for item in lines):
        return None
    fields["lines"] = lines
    try:
        return ParserPage.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_document(value: object) -> Document | None:
    fields = _exact_fields(value, Document)
    if fields is None:
        return None
    try:
        return Document.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_case(value: object) -> Case | None:
    fields = _exact_fields(value, Case)
    if fields is None:
        return None
    raw_spans = fields.get("source_spans")
    if type(raw_spans) is not tuple:
        return None
    spans = tuple(_revalidate_span(span) for span in raw_spans)
    if any(span is None for span in spans):
        return None
    fields["source_spans"] = spans
    try:
        return Case.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_assessment(value: object) -> QualityAssessment | None:
    fields = _exact_fields(value, QualityAssessment)
    if fields is None:
        return None
    raw_findings = fields.get("findings")
    if type(raw_findings) is not tuple:
        return None
    findings: list[QualityFinding] = []
    for raw_finding in raw_findings:
        finding_fields = _exact_fields(raw_finding, QualityFinding)
        if finding_fields is None:
            return None
        try:
            findings.append(QualityFinding.model_validate(finding_fields))
        except (TypeError, ValueError):
            return None
    fields["findings"] = tuple(findings)
    try:
        return QualityAssessment.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_parser_quarantine(value: object) -> ParserQuarantine | None:
    expected_type: type[BoundaryQuarantine | UpstreamPageQuarantine]
    if type(value) is BoundaryQuarantine:
        expected_type = BoundaryQuarantine
    elif type(value) is UpstreamPageQuarantine:
        expected_type = UpstreamPageQuarantine
    else:
        return None
    fields = _exact_fields(value, expected_type)
    if fields is None:
        return None
    raw_spans = fields.get("source_spans")
    if type(raw_spans) is not tuple:
        return None
    spans = tuple(_revalidate_span(span) for span in raw_spans)
    if any(span is None for span in spans):
        return None
    fields["source_spans"] = spans
    try:
        return expected_type.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _quarantine_row(
    *,
    doc_id: str,
    edition_year: int,
    quarantine: ParserQuarantine,
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "edition_year": edition_year,
        **quarantine.model_dump(mode="json"),
    }


def _canonical_parser_quarantines_bytes(
    rows: tuple[tuple[str, int, ParserQuarantine], ...],
) -> bytes | None:
    encoded: list[bytes] = []
    for doc_id, edition_year, quarantine in rows:
        raw = _canonical_json_bytes(
            _quarantine_row(
                doc_id=doc_id,
                edition_year=edition_year,
                quarantine=quarantine,
            )
        )
        if raw is None:
            return None
        encoded.append(raw)
    result = b"".join(sorted(encoded))
    return result if len(result) <= _MAX_REVIEW_FILE_BYTES else None


def _revalidate_parser_quarantines_bytes(
    raw: object,
    *,
    expected_count: object,
    documents: tuple[SourceDocument | Document, ...],
) -> tuple[ParserQuarantine, ...] | None:
    if (
        type(raw) is not bytes
        or type(expected_count) is not int
        or expected_count < 0
        or len(raw) > _MAX_REVIEW_FILE_BYTES
    ):
        return None
    if expected_count == 0:
        return () if raw == b"" else None
    if not raw or not raw.endswith(b"\n"):
        return None
    lines = raw.splitlines(keepends=True)
    if len(lines) != expected_count or b"".join(sorted(lines)) != raw:
        return None
    document_by_id = {document.doc_id: document for document in documents}
    if len(document_by_id) != len(documents):
        return None
    output: list[ParserQuarantine] = []
    for line in lines:
        row = _json_object(line)
        if row is None:
            return None
        doc_id = row.get("doc_id")
        edition_year = row.get("edition_year")
        reason_code = row.get("reason_code")
        document = document_by_id.get(doc_id) if type(doc_id) is str else None
        if (
            document is None
            or type(edition_year) is not int
            or edition_year != document.edition_year
            or type(reason_code) is not str
        ):
            return None
        approved_doc_id = cast(str, doc_id)
        approved_edition_year = edition_year
        model_type: type[BoundaryQuarantine | UpstreamPageQuarantine]
        expected_fields: set[str]
        if reason_code == "ambiguous_boundary":
            model_type = BoundaryQuarantine
            expected_fields = {
                "location_id",
                "page_ids",
                "reason_code",
                "source_spans",
                "span_count",
            }
        else:
            model_type = UpstreamPageQuarantine
            expected_fields = {
                "location_id",
                "occurrence_count",
                "page_ids",
                "reason_code",
                "source_spans",
                "span_count",
            }
        if set(row) != expected_fields | {"doc_id", "edition_year"}:
            return None
        payload = {field: row[field] for field in expected_fields}
        payload_raw = _canonical_json_bytes(payload)
        try:
            quarantine = (
                model_type.model_validate_json(payload_raw)
                if payload_raw is not None
                else None
            )
        except (TypeError, ValueError):
            return None
        if quarantine is None:
            return None
        if (
            any(page_id > document.pdf_page_count for page_id in quarantine.page_ids)
            or (
                type(quarantine) is BoundaryQuarantine
                and tuple(
                    sorted({span.pdf_page_index for span in quarantine.source_spans})
                )
                != quarantine.page_ids
            )
            or _canonical_json_bytes(
                _quarantine_row(
                    doc_id=approved_doc_id,
                    edition_year=approved_edition_year,
                    quarantine=quarantine,
                )
            )
            != line
        ):
            return None
        output.append(quarantine)
    return tuple(output)


def _canonical_document(source: SourceDocument, ingestion_version: str) -> Document:
    page_numbering = source.page_numbering
    return Document(
        doc_id=source.doc_id,
        edition_year=source.edition_year,
        title=source.official_title,
        publisher=source.publisher,
        registration_no=source.registration_no,
        source_period_start=source.source_period_start,
        source_period_end=source.source_period_end,
        source_filename=source.source_filename,
        sha256=source.sha256,
        pdf_page_count=source.pdf_page_count,
        extraction_method=source.extraction_method,
        source_dpi=source.source_dpi,
        public_url=source.official_public_url,
        redistribution_status=source.redistribution_status,
        access_level=source.access_level,
        page_numbering_rule=(
            f"{page_numbering.mode}:body="
            f"{page_numbering.body_start_pdf_page}-{page_numbering.body_end_pdf_page}:"
            f"offset={page_numbering.offset}"
        ),
        ingestion_version=ingestion_version,
    )


def _document_manifest_bytes(source: SourceDocument) -> bytes:
    payload = json.dumps(
        source.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return b"sen-qa-source-document-v1\0" + payload


def _document_manifest_sha256(source: SourceDocument) -> str:
    return hashlib.sha256(_document_manifest_bytes(source)).hexdigest()


def _span_key(span: SourceSpan) -> tuple[object, ...]:
    return (span.pdf_page_index, span.page_label, span.bbox, span.text_sha256)


def _role_sources(
    candidate: ParsedCaseCandidate,
    case: Case,
    raw_text_by_span: dict[tuple[object, ...], str],
) -> tuple[RoleSource, ...]:
    span_indexes = {
        _span_key(span): index for index, span in enumerate(case.source_spans)
    }
    role_names = {"question", "answer", "basis", "facts", "target", "situation"}
    sources: list[RoleSource] = []
    for fragment in candidate.fragments:
        if fragment.role not in role_names:
            continue
        key = _span_key(fragment.source_span)
        index = span_indexes.get(key)
        raw_text = raw_text_by_span.get(key)
        if index is None or raw_text is None:
            _raise("staging_input_invalid")
        sources.append(
            RoleSource(
                role=(
                    "question"
                    if fragment.role in {"target", "situation"}
                    else cast(ChunkRole, fragment.role)
                ),
                text=fragment.text,
                raw_text=raw_text,
                source_span_index=index,
            )
        )
    if not sources:
        _raise("staging_input_invalid")
    try:
        role_source_manifest_bytes(case, tuple(sources))
    except (TypeError, ValueError):
        _raise("staging_input_invalid")
    return tuple(sources)


def _promotion_envelope(
    case: Case,
    role_sources: tuple[RoleSource, ...],
    *,
    parser_authority_sha256: str,
    raw_authority_sha256: str,
) -> VerifiedPromotionEnvelope:
    payload = {
        "candidate_case": case.model_dump(mode="json"),
        "corrections": [],
        "parser_authority_sha256": parser_authority_sha256,
        "raw_authority_sha256": raw_authority_sha256,
        "role_sources": [
            {
                "raw_text": source.raw_text,
                "role": source.role,
                "source_span_index": source.source_span_index,
                "table_evidence_sha256": source.table_evidence_sha256,
                "table_header": source.table_header,
                "table_header_raw_text": source.table_header_raw_text,
                "table_header_source_span_index": source.table_header_source_span_index,
                "text": source.text,
            }
            for source in role_sources
        ],
        "schema_version": "sen-qa-promotion-envelope/v1",
    }
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return load_promotion_envelope(
        raw,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _case_id(candidate: ParsedCaseCandidate, *, duplicate: bool) -> str:
    if duplicate:
        first_page = candidate.source_spans[0].pdf_page_index
        return make_case_id(
            candidate.edition_year,
            candidate.domain,
            candidate.part,
            candidate.case_no,
            first_page,
            candidate.title,
            duplicate=True,
        )
    return make_case_id(
        candidate.edition_year,
        candidate.domain,
        candidate.part,
        candidate.case_no,
    )


def _canonical_question(candidate: ParsedCaseCandidate) -> str | None:
    fragments = tuple(
        fragment.text
        for fragment in candidate.fragments
        if fragment.role in {"target", "situation", "question"}
    )
    if not fragments:
        return None
    return "\n".join(fragments)


def _review_locations(
    assessment: QualityAssessment, case: Case
) -> tuple[ReviewSourceLocation, ...]:
    locations: list[ReviewSourceLocation] = []
    findings = assessment.findings or (None,)
    for finding in findings:
        page_id = (
            finding.page_id
            if finding is not None and finding.page_id is not None
            else None
        ) or case.source_spans[0].pdf_page_index
        span = next(
            (item for item in case.source_spans if item.pdf_page_index == page_id),
            case.source_spans[0],
        )
        locations.append(
            ReviewSourceLocation(
                page_id=page_id,
                bbox=span.bbox,
                reason_code=(
                    finding.reason_code
                    if finding is not None
                    else "human-review-required"
                ),
                count=finding.count if finding is not None else 1,
            )
        )
    return tuple(
        sorted(
            set(locations),
            key=lambda item: (item.page_id, item.bbox, item.reason_code, item.count),
        )
    )


def _prepare_review_batch(
    *,
    document: object,
    result: object,
    pages: object,
    parser_authority_sha256: str,
    raw_authority_sha256: str,
    ingestion_version: str,
) -> PreparedReviewBatch:
    """Derive review candidates solely from verified parser and raw authorities."""
    approved_document = revalidate_source_document(document)
    if (
        approved_document is None
        or type(result) is not ParseResult
        or type(pages) is not tuple
        or not 1 <= len(result.cases) <= _MAX_CASES
        or not pages
        or not _SHA256_RE.fullmatch(parser_authority_sha256)
        or not _SHA256_RE.fullmatch(raw_authority_sha256)
        or not _INGESTION_VERSION_RE.fullmatch(ingestion_version)
    ):
        _raise("staging_input_invalid")

    checked_pages = tuple(_revalidate_page(page) for page in pages)
    checked_candidates = tuple(_revalidate_candidate(case) for case in result.cases)
    checked_quarantines = tuple(
        _revalidate_parser_quarantine(item) for item in result.quarantines
    )
    if any(
        item is None
        for item in (*checked_pages, *checked_candidates, *checked_quarantines)
    ):
        _raise("staging_input_invalid")
    approved_pages = cast(tuple[ParserPage, ...], checked_pages)
    candidates = cast(tuple[ParsedCaseCandidate, ...], checked_candidates)
    quarantines = cast(tuple[ParserQuarantine, ...], checked_quarantines)
    if sum(len(page.lines) for page in approved_pages) > _MAX_LINES:
        _raise("staging_input_invalid")
    page_indexes = tuple(page.pdf_page_index for page in approved_pages)
    if page_indexes != tuple(range(1, approved_document.pdf_page_count + 1)):
        _raise("staging_input_invalid")
    if any(
        page.doc_id != approved_document.doc_id
        or page.edition_year != approved_document.edition_year
        or page.extraction_source != approved_document.extraction_method
        or page.source_sha256 != approved_document.sha256
        or page.pdf_page_index > approved_document.pdf_page_count
        for page in approved_pages
    ):
        _raise("staging_input_invalid")

    raw_text_by_span: dict[tuple[object, ...], str] = {}
    for page in approved_pages:
        for line in page.lines:
            span = SourceSpan(
                pdf_page_index=page.pdf_page_index,
                page_label=page.page_label,
                bbox=line.bbox,
                text_sha256=line.raw_text_sha256,
            )
            key = _span_key(span)
            if key in raw_text_by_span and raw_text_by_span[key] != line.raw_text:
                _raise("staging_input_invalid")
            raw_text_by_span[key] = line.raw_text

    canonical_document = _canonical_document(approved_document, ingestion_version)
    quarantine_bytes = _canonical_parser_quarantines_bytes(
        tuple(
            (approved_document.doc_id, approved_document.edition_year, quarantine)
            for quarantine in quarantines
        )
    )
    if quarantine_bytes is None:
        _raise("staging_input_invalid")
    base_ids: list[str] = []
    try:
        for candidate in candidates:
            if (
                candidate.doc_id != approved_document.doc_id
                or candidate.edition_year != approved_document.edition_year
                or candidate.extraction_source != approved_document.extraction_method
            ):
                _raise("staging_input_invalid")
            base_ids.append(_case_id(candidate, duplicate=False))
    except (IndexError, TypeError, ValueError):
        _raise("staging_input_invalid")

    cases: list[Case] = []
    envelopes: list[VerifiedPromotionEnvelope] = []
    assessments: list[QualityAssessment] = []
    references: list[ReviewReference] = []
    sampling_candidates: list[SamplingCandidate] = []
    base_id_counts = Counter(base_ids)
    for candidate, base_id in zip(candidates, base_ids, strict=True):
        try:
            case_id = _case_id(candidate, duplicate=base_id_counts[base_id] > 1)
            if any(case.case_id == case_id for case in cases):
                _raise("staging_input_invalid")
            source_text_by_span: dict[int, str] = {}
            for index, span in enumerate(candidate.source_spans):
                raw_text = raw_text_by_span.get(_span_key(span))
                if raw_text is None:
                    _raise("staging_input_invalid")
                source_text_by_span[index] = raw_text
            title_raw = " ".join(
                raw_text_by_span[_span_key(fragment.source_span)]
                for fragment in candidate.fragments
                if fragment.role == "title"
            ).strip()
            if not title_raw:
                _raise("staging_input_invalid")
            findings = tuple(
                finding
                for index, raw_text in source_text_by_span.items()
                for finding in scan_text(
                    raw_text,
                    location_id=f"case-{case_id}:span-{index}",
                    case_type=candidate.case_type,
                )
            )
            privacy = classify_privacy(
                findings,
                case_type=candidate.case_type,
                audit_masked=any(
                    finding.kind == "anonymization_mark" for finding in findings
                ),
            )
            review_status = (
                "needs_review"
                if privacy.pii_class == "restricted"
                else candidate.review_status
            )
            case = Case(
                case_id=case_id,
                legacy_ids=(),
                doc_id=candidate.doc_id,
                case_type=candidate.case_type,
                domain=candidate.domain,
                part=candidate.part,
                subtopic=candidate.subtopic,
                case_no=candidate.case_no,
                title_raw=title_raw,
                title_normalized=candidate.title,
                question=_canonical_question(candidate),
                answer=candidate.answer,
                facts=candidate.facts,
                basis_text=candidate.basis_text,
                law_ref_ids=(),
                source_spans=candidate.source_spans,
                extraction_source=candidate.extraction_source,
                extraction_confidence=candidate.extraction_confidence,
                critical_field_review="pending",
                pii_class=privacy.pii_class,
                anonymization_status=(
                    "masked"
                    if any(finding.kind == "anonymization_mark" for finding in findings)
                    else "not_detected"
                ),
                currency_status="unverified",
                search_eligible=False,
                answer_eligible=False,
                review_status=review_status,
            )
            role_sources = _role_sources(candidate, case, raw_text_by_span)
            envelope = _promotion_envelope(
                case,
                role_sources,
                parser_authority_sha256=parser_authority_sha256,
                raw_authority_sha256=raw_authority_sha256,
            )
            assessment = assess_case(
                case,
                canonical_document,
                source_text_by_span,
                ocr_layout_review=(
                    "not_applicable"
                    if candidate.extraction_source == "native"
                    else "unreviewed"
                ),
            )
        except StagingError:
            raise
        except (KeyError, TypeError, ValueError):
            _raise("staging_input_invalid")
        cases.append(case)
        envelopes.append(envelope)
        assessments.append(assessment)
        reference = ReviewReference(
            case_id=case.case_id,
            content_sha256=envelope.fingerprint_sha256,
            source_locations=_review_locations(assessment, case),
        )
        references.append(reference)
        native_segment = (
            assign_native_review_segment(approved_document, candidate)
            if candidate.extraction_source == "native"
            and candidate.edition_year in {2020, 2021, 2022}
            else None
        )
        sampling_candidates.append(
            SamplingCandidate(
                reference=reference,
                edition_year=candidate.edition_year,
                extraction_source=candidate.extraction_source,
                pii_class=case.pii_class,
                review_status=case.review_status,
                layout_segment_provenances=candidate.layout_segment_provenances,
                native_layout_segment=native_segment,
                doc_id=approved_document.doc_id,
                source_sha256=approved_document.sha256,
            )
        )

    try:
        registry = CanonicalReviewRegistry.create(cases=references)
        raw_registry = registry.to_bytes()
        verified_registry = CanonicalReviewRegistry.from_bytes(
            raw_registry,
            expected_sha256=hashlib.sha256(raw_registry).hexdigest(),
        )
    except (TypeError, ValueError):
        _raise("staging_input_invalid")

    batch = object.__new__(PreparedReviewBatch)
    object.__setattr__(batch, "documents", (canonical_document,))
    object.__setattr__(batch, "source_documents", (approved_document,))
    object.__setattr__(batch, "cases", tuple(cases))
    object.__setattr__(batch, "envelopes", tuple(envelopes))
    object.__setattr__(batch, "assessments", tuple(assessments))
    object.__setattr__(batch, "sampling_candidates", tuple(sampling_candidates))
    object.__setattr__(batch, "registry", verified_registry)
    object.__setattr__(batch, "parser_authority_sha256", parser_authority_sha256)
    object.__setattr__(batch, "raw_authority_sha256", raw_authority_sha256)
    object.__setattr__(batch, "ocr_authority_lock", None)
    object.__setattr__(batch, "ocr_authority_lock_bytes", None)
    object.__setattr__(batch, "ocr_authority_lock_sha256", None)
    object.__setattr__(batch, "ocr_authority_self_sha256", None)
    object.__setattr__(
        batch, "manifest_sha256", _document_manifest_sha256(approved_document)
    )
    object.__setattr__(
        batch, "manifest_bytes", _document_manifest_bytes(approved_document)
    )
    object.__setattr__(
        batch,
        "document_page_counts",
        {
            approved_document.doc_id: DocumentPageCounts(
                succeeded=sum(
                    page.page_status == "extracted" for page in approved_pages
                ),
                quarantined=sum(
                    page.page_status == "quarantined" for page in approved_pages
                ),
                failed=0,
            )
        },
    )
    object.__setattr__(batch, "parser_quarantines_bytes", quarantine_bytes)
    object.__setattr__(
        batch,
        "parser_quarantines_sha256",
        hashlib.sha256(quarantine_bytes).hexdigest(),
    )
    object.__setattr__(batch, "quarantine_count", len(quarantines))
    object.__setattr__(batch, "resolution_authority", None)
    object.__setattr__(batch, "resolution_authority_bytes", None)
    object.__setattr__(batch, "resolution_authority_sha256", None)
    return batch


def prepare_review_batch(
    *,
    document: object,
    result: object,
    pages: object,
    parser_authority_sha256: str,
    raw_authority_sha256: str,
    ingestion_version: str,
) -> PreparedReviewBatch:
    """Derive review candidates through one cause-free public boundary."""
    code: str | None = None
    try:
        return _prepare_review_batch(
            document=document,
            result=result,
            pages=pages,
            parser_authority_sha256=parser_authority_sha256,
            raw_authority_sha256=raw_authority_sha256,
            ingestion_version=ingestion_version,
        )
    except StagingError:
        code = "staging_input_invalid"
    except (KeyError, OSError, RecursionError, OverflowError, TypeError, ValueError):
        code = "staging_input_invalid"
    _raise(code or "staging_input_invalid")


def _prepare_quarantine_only_unit(
    run: VerifiedParseRun,
    *,
    ingestion_version: str,
) -> _QuarantineOnlyUnit:
    approved_document = revalidate_source_document(run.document)
    result = run.result
    pages = run.pages
    if (
        approved_document is None
        or type(result) is not ParseResult
        or result.cases
        or not result.quarantines
        or type(pages) is not tuple
        or not pages
        or not _INGESTION_VERSION_RE.fullmatch(ingestion_version)
    ):
        _raise("staging_input_invalid")
    checked_pages = tuple(_revalidate_page(page) for page in pages)
    checked_quarantines = tuple(
        _revalidate_parser_quarantine(item) for item in result.quarantines
    )
    if any(item is None for item in (*checked_pages, *checked_quarantines)):
        _raise("staging_input_invalid")
    approved_pages = cast(tuple[ParserPage, ...], checked_pages)
    quarantines = cast(tuple[ParserQuarantine, ...], checked_quarantines)
    if (
        sum(len(page.lines) for page in approved_pages) > _MAX_LINES
        or tuple(page.pdf_page_index for page in approved_pages)
        != tuple(range(1, approved_document.pdf_page_count + 1))
        or any(
            page.doc_id != approved_document.doc_id
            or page.edition_year != approved_document.edition_year
            or page.extraction_source != approved_document.extraction_method
            or page.source_sha256 != approved_document.sha256
            or page.pdf_page_index > approved_document.pdf_page_count
            for page in approved_pages
        )
    ):
        _raise("staging_input_invalid")
    canonical_document = _canonical_document(approved_document, ingestion_version)
    quarantine_bytes = _canonical_parser_quarantines_bytes(
        tuple(
            (approved_document.doc_id, approved_document.edition_year, quarantine)
            for quarantine in quarantines
        )
    )
    if (
        quarantine_bytes is None
        or _revalidate_parser_quarantines_bytes(
            quarantine_bytes,
            expected_count=len(quarantines),
            documents=(canonical_document,),
        )
        is None
    ):
        _raise("staging_input_invalid")
    return _QuarantineOnlyUnit(
        document=canonical_document,
        source_document=approved_document,
        page_count=DocumentPageCounts(
            succeeded=sum(page.page_status == "extracted" for page in approved_pages),
            quarantined=sum(
                page.page_status == "quarantined" for page in approved_pages
            ),
            failed=0,
        ),
        parser_quarantines_bytes=quarantine_bytes,
        quarantine_count=len(quarantines),
    )


def _prepare_review_corpus(
    runs: object,
    *,
    ingestion_version: str,
    parser_authority_override: str | None = None,
    raw_authority_override: str | None = None,
) -> PreparedReviewBatch:
    """Combine manifest-bound annual parse runs under two corpus-wide authorities."""
    if (
        type(runs) is not tuple
        or not 1 <= len(runs) <= 64
        or any(type(run) is not VerifiedParseRun for run in runs)
        or not _INGESTION_VERSION_RE.fullmatch(ingestion_version)
        or ((parser_authority_override is None) != (raw_authority_override is None))
        or (
            parser_authority_override is not None
            and (
                _SHA256_RE.fullmatch(parser_authority_override) is None
                or raw_authority_override is None
                or _SHA256_RE.fullmatch(raw_authority_override) is None
            )
        )
    ):
        _raise("staging_input_invalid")
    checked_runs = cast(tuple[VerifiedParseRun, ...], runs)
    total_case_count = sum(len(run.result.cases) for run in checked_runs)
    if not 1 <= total_case_count <= _MAX_CASES:
        _raise("staging_input_invalid")
    ordered_runs = tuple(sorted(checked_runs, key=lambda run: run.document.doc_id))
    document_ids = tuple(run.document.doc_id for run in ordered_runs)
    if document_ids != tuple(sorted(set(document_ids))):
        _raise("staging_input_invalid")
    manifest_bytes = ordered_runs[0].manifest_bytes
    if any(run.manifest_bytes != manifest_bytes for run in ordered_runs):
        _raise("staging_input_invalid")
    try:
        authority_rows = [
            {
                "doc_id": run.document.doc_id,
                "input_sha256": hashlib.sha256(run.input_bytes).hexdigest(),
                "parse_sha256": hashlib.sha256(
                    canonical_result_bytes(run.result)
                ).hexdigest(),
            }
            for run in ordered_runs
        ]
        authority_bytes = json.dumps(
            authority_rows,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        derived_parser_authority_sha256 = hashlib.sha256(
            b"sen-qa-parser-corpus-authority-v1\0" + authority_bytes
        ).hexdigest()
        derived_raw_authority_sha256 = hashlib.sha256(
            b"sen-qa-raw-corpus-authority-v1\0" + authority_bytes
        ).hexdigest()
        parser_authority_sha256 = (
            derived_parser_authority_sha256
            if parser_authority_override is None
            else parser_authority_override
        )
        raw_authority_sha256 = (
            derived_raw_authority_sha256
            if raw_authority_override is None
            else raw_authority_override
        )
        case_runs = tuple(run for run in ordered_runs if run.result.cases)
        quarantine_only_runs = tuple(
            run for run in ordered_runs if not run.result.cases
        )
        batches = tuple(
            prepare_review_batch(
                document=run.document,
                result=run.result,
                pages=run.pages,
                parser_authority_sha256=parser_authority_sha256,
                raw_authority_sha256=raw_authority_sha256,
                ingestion_version=ingestion_version,
            )
            for run in case_runs
        )
        quarantine_only_units = tuple(
            _prepare_quarantine_only_unit(
                run,
                ingestion_version=ingestion_version,
            )
            for run in quarantine_only_runs
        )
    except (RecursionError, TypeError, ValueError):
        _raise("staging_input_invalid")
    rows = sorted(
        (
            (case, envelope, assessment, sampling_candidate)
            for batch in batches
            for case, envelope, assessment, sampling_candidate in zip(
                batch.cases,
                batch.envelopes,
                batch.assessments,
                batch.sampling_candidates,
                strict=True,
            )
        ),
        key=lambda row: row[0].case_id,
    )
    if len(rows) != len({case.case_id for case, _, _, _ in rows}):
        _raise("staging_input_invalid")
    references = tuple(
        reference for batch in batches for reference in batch.registry.cases
    )
    try:
        registry = CanonicalReviewRegistry.create(cases=references)
        registry_bytes = registry.to_bytes()
        verified_registry = CanonicalReviewRegistry.from_bytes(
            registry_bytes,
            expected_sha256=hashlib.sha256(registry_bytes).hexdigest(),
        )
    except (TypeError, ValueError):
        _raise("staging_input_invalid")
    batch = object.__new__(PreparedReviewBatch)
    combined_documents = tuple(
        sorted(
            (
                *(document for item in batches for document in item.documents),
                *(item.document for item in quarantine_only_units),
            ),
            key=lambda document: document.doc_id,
        )
    )
    object.__setattr__(
        batch,
        "documents",
        combined_documents,
    )
    object.__setattr__(
        batch,
        "source_documents",
        tuple(
            sorted(
                (
                    *(
                        document
                        for item in batches
                        for document in item.source_documents
                    ),
                    *(item.source_document for item in quarantine_only_units),
                ),
                key=lambda document: document.doc_id,
            )
        ),
    )
    object.__setattr__(batch, "cases", tuple(row[0] for row in rows))
    object.__setattr__(batch, "envelopes", tuple(row[1] for row in rows))
    object.__setattr__(batch, "assessments", tuple(row[2] for row in rows))
    object.__setattr__(batch, "sampling_candidates", tuple(row[3] for row in rows))
    object.__setattr__(batch, "registry", verified_registry)
    object.__setattr__(batch, "parser_authority_sha256", parser_authority_sha256)
    object.__setattr__(batch, "raw_authority_sha256", raw_authority_sha256)
    object.__setattr__(batch, "ocr_authority_lock", None)
    object.__setattr__(batch, "ocr_authority_lock_bytes", None)
    object.__setattr__(batch, "ocr_authority_lock_sha256", None)
    object.__setattr__(batch, "ocr_authority_self_sha256", None)
    object.__setattr__(
        batch,
        "manifest_sha256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    object.__setattr__(batch, "manifest_bytes", manifest_bytes)
    object.__setattr__(
        batch,
        "document_page_counts",
        {
            doc_id: count
            for item in batches
            for doc_id, count in item.document_page_counts.items()
        }
        | {item.document.doc_id: item.page_count for item in quarantine_only_units},
    )
    quarantine_bytes = b"".join(
        sorted(
            line
            for raw in (
                *(item.parser_quarantines_bytes for item in batches),
                *(item.parser_quarantines_bytes for item in quarantine_only_units),
            )
            for line in raw.splitlines(keepends=True)
        )
    )
    quarantine_count = sum(item.quarantine_count for item in batches) + sum(
        item.quarantine_count for item in quarantine_only_units
    )
    if (
        _revalidate_parser_quarantines_bytes(
            quarantine_bytes,
            expected_count=quarantine_count,
            documents=combined_documents,
        )
        is None
    ):
        _raise("staging_input_invalid")
    object.__setattr__(batch, "parser_quarantines_bytes", quarantine_bytes)
    object.__setattr__(
        batch,
        "parser_quarantines_sha256",
        hashlib.sha256(quarantine_bytes).hexdigest(),
    )
    object.__setattr__(
        batch,
        "quarantine_count",
        quarantine_count,
    )
    object.__setattr__(batch, "resolution_authority", None)
    object.__setattr__(batch, "resolution_authority_bytes", None)
    object.__setattr__(batch, "resolution_authority_sha256", None)
    return batch


def prepare_review_corpus(
    runs: object,
    *,
    ingestion_version: str,
) -> PreparedReviewBatch:
    """Combine annual runs through one cause-free public boundary."""
    code: str | None = None
    try:
        return _prepare_review_corpus(runs, ingestion_version=ingestion_version)
    except StagingError:
        code = "staging_input_invalid"
    except (KeyError, OSError, RecursionError, OverflowError, TypeError, ValueError):
        code = "staging_input_invalid"
    _raise(code or "staging_input_invalid")


def _resolved_parser_authority_sha256(
    runs: tuple[VerifiedParseRun, ...],
    results: tuple[ParseResult, ...],
    *,
    source_batch: PreparedReviewBatch,
    resolution_authority_sha256: str,
) -> str:
    rows = tuple(
        {
            "doc_id": run.document.doc_id,
            "input_sha256": hashlib.sha256(run.input_bytes).hexdigest(),
            "resolved_parse_sha256": hashlib.sha256(
                canonical_result_bytes(result)
            ).hexdigest(),
        }
        for run, result in zip(runs, results, strict=True)
    )
    payload = {
        "manifest_sha256": source_batch.manifest_sha256,
        "raw_authority_sha256": source_batch.raw_authority_sha256,
        "resolution_authority_sha256": resolution_authority_sha256,
        "resolved_from_parser_authority_sha256": (source_batch.parser_authority_sha256),
        "resolved_from_parser_quarantines_sha256": (
            source_batch.parser_quarantines_sha256
        ),
        "resolved_from_registry_sha256": source_batch.registry.fingerprint_sha256,
        "runs": rows,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(b"sen-qa-parser-corpus-authority-v2\0" + raw).hexdigest()


def _prepare_resolved_review_corpus(
    runs: object,
    resolved_results: object,
    *,
    source_batch: object,
    resolution_authority: object,
    expected_resolution_authority_sha256: str,
    ingestion_version: str,
) -> PreparedReviewBatch:
    if (
        type(runs) is not tuple
        or not runs
        or any(type(run) is not VerifiedParseRun for run in runs)
        or type(resolved_results) is not tuple
        or any(type(result) is not ParseResult for result in resolved_results)
        or type(resolution_authority) is not VerifiedQuarantineResolutionAuthority
        or type(expected_resolution_authority_sha256) is not str
        or _SHA256_RE.fullmatch(expected_resolution_authority_sha256) is None
        or not _INGESTION_VERSION_RE.fullmatch(ingestion_version)
    ):
        _raise("staging_input_invalid")
    approved_source = _revalidate_batch(source_batch)
    if approved_source is None or approved_source.quarantine_count <= 0:
        _raise("staging_input_invalid")
    checked_authority = resolution_authority
    authority_bytes = checked_authority.to_bytes()
    if (
        not hmac.compare_digest(
            hashlib.sha256(authority_bytes).hexdigest(),
            expected_resolution_authority_sha256,
        )
        or not hmac.compare_digest(
            checked_authority.external_sha256,
            expected_resolution_authority_sha256,
        )
        or not hmac.compare_digest(
            checked_authority.registry_sha256,
            approved_source.registry.fingerprint_sha256,
        )
        or not hmac.compare_digest(
            checked_authority.manifest_sha256, approved_source.manifest_sha256
        )
        or not hmac.compare_digest(
            checked_authority.raw_authority_sha256,
            approved_source.raw_authority_sha256,
        )
        or not hmac.compare_digest(
            checked_authority.parser_authority_sha256,
            approved_source.parser_authority_sha256,
        )
        or not hmac.compare_digest(
            checked_authority.parser_quarantines_sha256,
            approved_source.parser_quarantines_sha256,
        )
        or checked_authority.quarantine_count != approved_source.quarantine_count
        or any(
            item.disposition == "unresolved" for item in checked_authority.resolutions
        )
    ):
        _raise("staging_input_invalid")
    ordered_runs = tuple(
        sorted(
            cast(tuple[VerifiedParseRun, ...], runs),
            key=lambda run: run.document.doc_id,
        )
    )
    regenerated_source = _prepare_review_corpus(
        ordered_runs,
        ingestion_version=ingestion_version,
    )
    if (
        regenerated_source.documents != approved_source.documents
        or regenerated_source.source_documents != approved_source.source_documents
        or regenerated_source.cases != approved_source.cases
        or regenerated_source.registry.to_bytes() != approved_source.registry.to_bytes()
        or regenerated_source.parser_authority_sha256
        != approved_source.parser_authority_sha256
        or regenerated_source.raw_authority_sha256
        != approved_source.raw_authority_sha256
        or regenerated_source.manifest_bytes != approved_source.manifest_bytes
        or regenerated_source.parser_quarantines_bytes
        != approved_source.parser_quarantines_bytes
        or regenerated_source.parser_quarantines_sha256
        != approved_source.parser_quarantines_sha256
    ):
        _raise("staging_input_invalid")
    authority_document_keys = {
        (item.doc_id, item.edition_year) for item in checked_authority.resolutions
    }
    affected_runs = tuple(
        run
        for run in ordered_runs
        if (run.document.doc_id, run.document.edition_year) in authority_document_keys
    )
    if (
        {(run.document.doc_id, run.document.edition_year) for run in affected_runs}
        != authority_document_keys
        or any(not run.result.quarantines for run in affected_runs)
        or any(
            run.result.quarantines
            for run in ordered_runs
            if (run.document.doc_id, run.document.edition_year)
            not in authority_document_keys
        )
    ):
        _raise("staging_input_invalid")
    reparsed_affected = reparse_with_resolution(
        tuple(run.pages for run in affected_runs),
        authority=checked_authority,
        expected_registry_sha256=approved_source.registry.fingerprint_sha256,
        expected_manifest_sha256=approved_source.manifest_sha256,
        expected_raw_authority_sha256=approved_source.raw_authority_sha256,
        expected_parser_authority_sha256=approved_source.parser_authority_sha256,
        parser_quarantines_bytes=approved_source.parser_quarantines_bytes,
        expected_parser_quarantines_sha256=(approved_source.parser_quarantines_sha256),
    )
    reparsed_by_document = {
        (run.document.doc_id, run.document.edition_year): result
        for run, result in zip(affected_runs, reparsed_affected, strict=True)
    }
    internally_resolved = tuple(
        reparsed_by_document.get(
            (run.document.doc_id, run.document.edition_year), run.result
        )
        for run in ordered_runs
    )
    supplied_results = cast(tuple[ParseResult, ...], resolved_results)
    if (
        len(reparsed_affected) != len(affected_runs)
        or len(supplied_results) != len(ordered_runs)
        or any(result.quarantines for result in internally_resolved)
        or any(
            canonical_result_bytes(supplied) != canonical_result_bytes(internal)
            for supplied, internal in zip(
                supplied_results, internally_resolved, strict=True
            )
        )
    ):
        _raise("staging_input_invalid")
    parser_authority_sha256 = _resolved_parser_authority_sha256(
        ordered_runs,
        internally_resolved,
        source_batch=approved_source,
        resolution_authority_sha256=expected_resolution_authority_sha256,
    )
    resolved_runs: list[VerifiedParseRun] = []
    for run, result in zip(ordered_runs, internally_resolved, strict=True):
        resolved_run = object.__new__(VerifiedParseRun)
        for name in ("document", "records", "pages", "manifest_bytes", "input_bytes"):
            object.__setattr__(resolved_run, name, object.__getattribute__(run, name))
        object.__setattr__(resolved_run, "result", result)
        resolved_runs.append(resolved_run)
    output = _prepare_review_corpus(
        tuple(resolved_runs),
        ingestion_version=ingestion_version,
        parser_authority_override=parser_authority_sha256,
        raw_authority_override=approved_source.raw_authority_sha256,
    )
    if approved_source.ocr_authority_lock is not None:
        output = _bind_ocr_authority(
            output,
            (
                approved_source.ocr_authority_lock,
                cast(bytes, approved_source.ocr_authority_lock_bytes),
                cast(str, approved_source.ocr_authority_lock_sha256),
                cast(str, approved_source.ocr_authority_self_sha256),
            ),
        )
    object.__setattr__(output, "resolution_authority", checked_authority)
    object.__setattr__(output, "resolution_authority_bytes", authority_bytes)
    object.__setattr__(
        output,
        "resolution_authority_sha256",
        expected_resolution_authority_sha256,
    )
    return output


def prepare_resolved_review_corpus(
    runs: object,
    resolved_results: object,
    *,
    source_batch: object,
    resolution_authority: object,
    expected_resolution_authority_sha256: str,
    ingestion_version: str,
) -> PreparedReviewBatch:
    """Rebuild a quarantine-free corpus under one externally sealed resolution."""
    try:
        return _prepare_resolved_review_corpus(
            runs,
            resolved_results,
            source_batch=source_batch,
            resolution_authority=resolution_authority,
            expected_resolution_authority_sha256=expected_resolution_authority_sha256,
            ingestion_version=ingestion_version,
        )
    except StagingError:
        pass
    except (KeyError, OSError, RecursionError, OverflowError, TypeError, ValueError):
        pass
    _raise("staging_input_invalid")


def _authority_matches_documents(
    lock: OcrAuthorityLock,
    documents: tuple[SourceDocument | Document, ...],
) -> bool:
    ocr_documents = tuple(
        document for document in documents if document.extraction_method == "ocr"
    )
    if not ocr_documents or type(lock.entries) is not tuple:
        return False
    for document in ocr_documents:
        matches = tuple(
            entry
            for entry in lock.entries
            if type(entry) is OcrAuthorityEntry
            and type(entry.year) is int
            and entry.year == document.edition_year
        )
        if len(matches) != 1:
            return False
        entry = matches[0]
        if (
            type(entry.doc_id) is not str
            or type(entry.source_sha256) is not str
            or not hmac.compare_digest(entry.doc_id, document.doc_id)
            or not hmac.compare_digest(entry.source_sha256, document.sha256)
        ):
            return False
    return True


def _verified_authority_from_path(
    path: object,
    expected_sha256: object,
    *,
    documents: tuple[SourceDocument | Document, ...],
) -> tuple[OcrAuthorityLock, bytes, str, str] | None:
    lock: OcrAuthorityLock | None = None
    canonical: bytes | None = None
    try:
        lock = load_ocr_authority_lock(
            path,  # type: ignore[arg-type]
            expected_sha256=expected_sha256,  # type: ignore[arg-type]
        )
        canonical = canonical_ocr_authority_bytes(lock)
    except (
        OcrAuthorityLockError,
        OSError,
        RecursionError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        lock = None
        canonical = None
    if (
        type(lock) is not OcrAuthorityLock
        or type(canonical) is not bytes
        or type(expected_sha256) is not str
        or _SHA256_RE.fullmatch(expected_sha256) is None
        or not hmac.compare_digest(
            hashlib.sha256(canonical).hexdigest(),
            expected_sha256,
        )
        or type(lock.self_sha256) is not str
        or _SHA256_RE.fullmatch(lock.self_sha256) is None
        or not _authority_matches_documents(lock, documents)
    ):
        return None
    return lock, canonical, expected_sha256, lock.self_sha256


def _bind_ocr_authority(
    batch: PreparedReviewBatch,
    authority: tuple[OcrAuthorityLock, bytes, str, str],
) -> PreparedReviewBatch:
    lock, canonical, file_sha256, self_sha256 = authority
    if (
        type(batch) is not PreparedReviewBatch
        or batch.ocr_authority_lock is not None
        or batch.ocr_authority_lock_bytes is not None
        or batch.ocr_authority_lock_sha256 is not None
        or batch.ocr_authority_self_sha256 is not None
    ):
        _raise("staging_input_invalid")
    object.__setattr__(batch, "ocr_authority_lock", lock)
    object.__setattr__(batch, "ocr_authority_lock_bytes", canonical)
    object.__setattr__(batch, "ocr_authority_lock_sha256", file_sha256)
    object.__setattr__(batch, "ocr_authority_self_sha256", self_sha256)
    return batch


def _prepare_review_corpus_from_artifacts(
    input_root: Path,
    *,
    manifest_path: Path,
    ingestion_version: str,
    expected_image_digest: str | None,
    ocr_authority_lock_path: Path | None,
    expected_ocr_authority_lock_sha256: str | None,
) -> PreparedReviewBatch:
    """Load the managed annual extractor layout and derive one review corpus."""
    if (
        not isinstance(input_root, Path)
        or not input_root.is_dir()
        or input_root.is_symlink()
        or not isinstance(manifest_path, Path)
        or manifest_path.is_symlink()
    ):
        _raise("staging_input_invalid")
    try:
        documents = tuple(
            sorted(load_manifest(manifest_path), key=lambda item: item.doc_id)
        )
    except (OSError, TypeError, ValueError):
        _raise("staging_input_invalid")
    if (
        not documents
        or len(documents) > 64
        or len({document.doc_id for document in documents}) != len(documents)
    ):
        _raise("staging_input_invalid")
    has_ocr = any(document.extraction_method == "ocr" for document in documents)
    authority: tuple[OcrAuthorityLock, bytes, str, str] | None = None
    if has_ocr:
        if (
            expected_image_digest is not None
            or ocr_authority_lock_path is None
            or expected_ocr_authority_lock_sha256 is None
        ):
            _raise("staging_input_invalid")
        authority = _verified_authority_from_path(
            ocr_authority_lock_path,
            expected_ocr_authority_lock_sha256,
            documents=documents,
        )
        if authority is None:
            _raise("staging_input_invalid")
    elif (
        expected_image_digest is not None
        or ocr_authority_lock_path is not None
        or expected_ocr_authority_lock_sha256 is not None
    ):
        _raise("staging_input_invalid")
    expected_directories = (
        {"native"}
        if any(document.extraction_method == "native" for document in documents)
        else set()
    ) | {
        f"ocr-{document.edition_year}"
        for document in documents
        if document.extraction_method == "ocr"
    }
    try:
        actual_directories = {path.name for path in input_root.iterdir()}
    except OSError:
        _raise("staging_input_invalid")
    if actual_directories != expected_directories:
        _raise("staging_input_invalid")
    runs: list[VerifiedParseRun] = []
    for document in documents:
        directory = (
            input_root / "native"
            if document.extraction_method == "native"
            else input_root / f"ocr-{document.edition_year}"
        )
        input_path = directory / f"{document.doc_id}.jsonl"
        expected_names = {
            f"{item.doc_id}.jsonl"
            for item in documents
            if (
                (
                    document.extraction_method == "native"
                    and item.extraction_method == "native"
                )
                or item.doc_id == document.doc_id
            )
        }
        try:
            actual_names = {path.name for path in directory.iterdir()}
        except OSError:
            _raise("staging_input_invalid")
        if (
            directory.is_symlink()
            or input_path.is_symlink()
            or actual_names != expected_names
        ):
            _raise("staging_input_invalid")
        try:
            run = build_parse_run(
                input_path,
                manifest_path=manifest_path,
                edition_year=document.edition_year,
                pages=(
                    "all"
                    if document.extraction_method == "native"
                    else f"1-{document.pdf_page_count}"
                ),
                expected_image_digest=None,
                ocr_authority_lock_path=(
                    None
                    if document.extraction_method == "native"
                    else ocr_authority_lock_path
                ),
                expected_ocr_authority_lock_sha256=(
                    None
                    if document.extraction_method == "native"
                    else expected_ocr_authority_lock_sha256
                ),
            )
        except (OSError, RecursionError, OverflowError, TypeError, ValueError):
            _raise("staging_input_invalid")
        runs.append(run)
    batch = prepare_review_corpus(tuple(runs), ingestion_version=ingestion_version)
    return _bind_ocr_authority(batch, authority) if authority is not None else batch


def prepare_review_corpus_from_artifacts(
    input_root: Path,
    *,
    manifest_path: Path,
    ingestion_version: str,
    expected_image_digest: str | None = None,
    ocr_authority_lock_path: Path | None = None,
    expected_ocr_authority_lock_sha256: str | None = None,
) -> PreparedReviewBatch:
    """Load managed artifacts through one cause-free public boundary."""
    code: str | None = None
    try:
        return _prepare_review_corpus_from_artifacts(
            input_root,
            manifest_path=manifest_path,
            ingestion_version=ingestion_version,
            expected_image_digest=expected_image_digest,
            ocr_authority_lock_path=ocr_authority_lock_path,
            expected_ocr_authority_lock_sha256=expected_ocr_authority_lock_sha256,
        )
    except StagingError:
        code = "staging_input_invalid"
    except (KeyError, OSError, RecursionError, OverflowError, TypeError, ValueError):
        code = "staging_input_invalid"
    _raise(code or "staging_input_invalid")


def _write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _read_private(
    path: Path,
    *,
    max_bytes: int = _MAX_REVIEW_FILE_BYTES,
    required_mode: int | None = None,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > max_bytes
            or (
                required_mode is not None
                and stat.S_IMODE(before.st_mode) != required_mode
            )
        ):
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > max_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            stat.S_IMODE(before.st_mode),
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            stat.S_IMODE(after.st_mode),
        ):
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _review_package_authority_sha256(
    package: Path,
    *,
    evidence_raw: bytes,
    documents_raw: bytes,
    expected_release_id: str,
    expected_registry_sha256: str,
) -> tuple[bool, str | None]:
    evidence = _json_object(evidence_raw)
    document_payload = _json_object(documents_raw)
    summary_raw = _read_private(package / "summary.json")
    summary = _json_object(summary_raw) if summary_raw is not None else None
    base_evidence_fields = {
        "document_page_counts",
        "manifest_sha256",
        "parser_quarantine_count",
        "parser_authority_sha256",
        "raw_authority_sha256",
        "schema_version",
    }
    base_summary_fields = {
        "case_count",
        "document_count",
        "manifest_sha256",
        "parser_authority_sha256",
        "quarantine_count",
        "raw_authority_sha256",
        "registry_sha256",
        "release_id",
        "schema_version",
    }
    if (
        evidence is None
        or document_payload is None
        or summary is None
        or summary_raw is None
        or set(document_payload) != {"documents", "schema_version"}
        or document_payload["schema_version"] != "sen-qa-review-documents/v1"
        or type(document_payload["documents"]) is not list
        or not document_payload["documents"]
        or _canonical_json_bytes(evidence) != evidence_raw
        or _canonical_json_bytes(document_payload) != documents_raw
        or _canonical_json_bytes(summary) != summary_raw
    ):
        return False, None
    try:
        documents = tuple(
            Document.model_validate(item)
            for item in cast(list[object], document_payload["documents"])
        )
    except (TypeError, ValueError):
        return False, None
    document_ids = tuple(document.doc_id for document in documents)
    page_counts = evidence.get("document_page_counts")
    if (
        document_ids != tuple(sorted(set(document_ids)))
        or type(page_counts) is not dict
        or set(page_counts) != set(document_ids)
        or type(evidence.get("parser_quarantine_count")) is not int
        or cast(int, evidence["parser_quarantine_count"]) < 0
        or any(
            type(evidence.get(field)) is not str
            or _SHA256_RE.fullmatch(cast(str, evidence[field])) is None
            for field in (
                "manifest_sha256",
                "parser_authority_sha256",
                "raw_authority_sha256",
            )
        )
    ):
        return False, None
    for document in documents:
        count = cast(dict[object, object], page_counts).get(document.doc_id)
        if (
            type(count) is not dict
            or set(count) != {"failed", "quarantined", "succeeded"}
            or any(type(count.get(field)) is not int for field in count)
            or any(cast(int, count[field]) < 0 for field in count)
            or sum(cast(int, count[field]) for field in count)
            != document.pdf_page_count
        ):
            return False, None
    if (
        type(summary.get("case_count")) is not int
        or cast(int, summary["case_count"]) < 0
        or type(summary.get("document_count")) is not int
        or summary["document_count"] != len(documents)
        or type(summary.get("quarantine_count")) is not int
        or summary["quarantine_count"] != evidence["parser_quarantine_count"]
        or summary.get("release_id") != expected_release_id
        or summary.get("registry_sha256") != expected_registry_sha256
        or any(
            summary.get(field) != evidence[field]
            for field in (
                "manifest_sha256",
                "parser_authority_sha256",
                "raw_authority_sha256",
            )
        )
    ):
        return False, None
    authority_path = package / "ocr-authority-lock.json"
    quarantine_path = package / "parser-quarantines.jsonl"
    resolution_path = package / "parser-quarantine-resolutions.json"
    quarantine_count = cast(int, evidence["parser_quarantine_count"])
    if evidence.get("schema_version") == "sen-qa-ingestion-evidence/v1":
        return (
            set(evidence) == base_evidence_fields
            and set(summary) == base_summary_fields
            and summary.get("schema_version") == "sen-qa-review-package/v1"
            and quarantine_count == 0
            and all(document.extraction_method == "native" for document in documents)
            and not authority_path.exists()
            and not authority_path.is_symlink()
            and not quarantine_path.exists()
            and not quarantine_path.is_symlink()
            and not resolution_path.exists()
            and not resolution_path.is_symlink(),
            None,
        )
    ocr_evidence_fields = {
        "ocr_authority_lock_sha256",
        "ocr_authority_self_sha256",
    }
    has_ocr = any(document.extraction_method == "ocr" for document in documents)
    schema_version = evidence.get("schema_version")
    resolution_fields = {
        "resolution_authority_sha256",
        "resolved_from_parser_authority_sha256",
        "resolved_from_parser_quarantines_sha256",
        "resolved_from_registry_sha256",
    }
    if schema_version == "sen-qa-ingestion-evidence/v4":
        expected_v4_evidence = base_evidence_fields | resolution_fields
        expected_v4_summary = base_summary_fields | {"resolution_authority_sha256"}
        if has_ocr:
            expected_v4_evidence |= ocr_evidence_fields
            expected_v4_summary.add("ocr_authority_lock_sha256")
        resolution_sha256 = evidence.get("resolution_authority_sha256")
        try:
            resolution = load_resolution_authority(
                resolution_path,
                expected_sha256=cast(str, resolution_sha256),
            )
        except (TypeError, ValueError):
            resolution = None
        if (
            quarantine_count != 0
            or quarantine_path.exists()
            or quarantine_path.is_symlink()
            or set(evidence) != expected_v4_evidence
            or set(summary) != expected_v4_summary
            or summary.get("schema_version") != "sen-qa-review-package/v4"
            or summary.get("resolution_authority_sha256") != resolution_sha256
            or type(resolution_sha256) is not str
            or _SHA256_RE.fullmatch(resolution_sha256) is None
            or resolution is None
            or getattr(resolution, "release_id", None) != expected_release_id
            or resolution.quarantine_count <= 0
            or any(item.disposition == "unresolved" for item in resolution.resolutions)
            or resolution.registry_sha256
            != evidence.get("resolved_from_registry_sha256")
            or resolution.registry_sha256 == expected_registry_sha256
            or resolution.parser_authority_sha256
            != evidence.get("resolved_from_parser_authority_sha256")
            or resolution.parser_quarantines_sha256
            != evidence.get("resolved_from_parser_quarantines_sha256")
            or resolution.manifest_sha256 != evidence.get("manifest_sha256")
            or resolution.raw_authority_sha256 != evidence.get("raw_authority_sha256")
            or resolution.parser_authority_sha256
            == evidence.get("parser_authority_sha256")
            or (
                not has_ocr and (authority_path.exists() or authority_path.is_symlink())
            )
        ):
            return False, None
        if not has_ocr:
            return True, None
    elif schema_version == "sen-qa-ingestion-evidence/v3":
        quarantine_sha256 = evidence.get("parser_quarantines_sha256")
        expected_v3_evidence = base_evidence_fields | {"parser_quarantines_sha256"}
        expected_v3_summary = base_summary_fields | {"parser_quarantines_sha256"}
        if has_ocr:
            expected_v3_evidence |= ocr_evidence_fields
            expected_v3_summary.add("ocr_authority_lock_sha256")
        quarantine_raw = _read_private(quarantine_path, required_mode=0o600)
        if (
            quarantine_count <= 0
            or set(evidence) != expected_v3_evidence
            or set(summary) != expected_v3_summary
            or summary.get("schema_version") != "sen-qa-review-package/v3"
            or summary.get("parser_quarantines_sha256") != quarantine_sha256
            or type(quarantine_sha256) is not str
            or _SHA256_RE.fullmatch(quarantine_sha256) is None
            or quarantine_raw is None
            or not hmac.compare_digest(
                hashlib.sha256(quarantine_raw).hexdigest(),
                quarantine_sha256,
            )
            or _revalidate_parser_quarantines_bytes(
                quarantine_raw,
                expected_count=quarantine_count,
                documents=documents,
            )
            is None
            or (
                not has_ocr and (authority_path.exists() or authority_path.is_symlink())
            )
            or resolution_path.exists()
            or resolution_path.is_symlink()
        ):
            return False, None
        if not has_ocr:
            return True, None
    elif (
        schema_version != "sen-qa-ingestion-evidence/v2"
        or quarantine_count != 0
        or quarantine_path.exists()
        or quarantine_path.is_symlink()
        or resolution_path.exists()
        or resolution_path.is_symlink()
    ):
        return False, None
    expected_fields = base_evidence_fields | ocr_evidence_fields
    expected_summary_fields = base_summary_fields | {"ocr_authority_lock_sha256"}
    expected_summary_version = "sen-qa-review-package/v2"
    if schema_version == "sen-qa-ingestion-evidence/v3":
        expected_fields.add("parser_quarantines_sha256")
        expected_summary_fields.add("parser_quarantines_sha256")
        expected_summary_version = "sen-qa-review-package/v3"
    elif schema_version == "sen-qa-ingestion-evidence/v4":
        expected_fields |= resolution_fields
        expected_summary_fields.add("resolution_authority_sha256")
        expected_summary_version = "sen-qa-review-package/v4"
    lock_sha256 = evidence.get("ocr_authority_lock_sha256")
    self_sha256 = evidence.get("ocr_authority_self_sha256")
    if (
        set(evidence) != expected_fields
        or set(summary) != expected_summary_fields
        or summary.get("schema_version") != expected_summary_version
        or summary.get("ocr_authority_lock_sha256") != lock_sha256
        or not has_ocr
        or type(lock_sha256) is not str
        or type(self_sha256) is not str
        or _SHA256_RE.fullmatch(lock_sha256) is None
        or _SHA256_RE.fullmatch(self_sha256) is None
    ):
        return False, None
    lock_raw = _read_private(authority_path)
    if lock_raw is None or not hmac.compare_digest(
        hashlib.sha256(lock_raw).hexdigest(), lock_sha256
    ):
        return False, None
    verified = _verified_authority_from_path(
        authority_path,
        lock_sha256,
        documents=documents,
    )
    if (
        verified is None
        or not hmac.compare_digest(verified[1], lock_raw)
        or not hmac.compare_digest(verified[3], self_sha256)
    ):
        return False, None
    return True, lock_sha256


def _revalidate_batch(
    value: object,
    *,
    require_ocr_authority: bool = True,
) -> PreparedReviewBatch | None:
    if type(value) is not PreparedReviewBatch:
        return None
    try:
        fields = {
            field.name: object.__getattribute__(value, field.name)
            for field in dataclass_fields(PreparedReviewBatch)
        }
    except (AttributeError, TypeError):
        return None
    expected_fields = {
        "assessments",
        "cases",
        "document_page_counts",
        "documents",
        "source_documents",
        "envelopes",
        "manifest_sha256",
        "manifest_bytes",
        "ocr_authority_lock",
        "ocr_authority_lock_bytes",
        "ocr_authority_lock_sha256",
        "ocr_authority_self_sha256",
        "parser_authority_sha256",
        "parser_quarantines_bytes",
        "parser_quarantines_sha256",
        "quarantine_count",
        "raw_authority_sha256",
        "registry",
        "resolution_authority",
        "resolution_authority_bytes",
        "resolution_authority_sha256",
        "sampling_candidates",
    }
    if set(fields) != expected_fields:
        return None
    raw_cases = fields["cases"]
    raw_envelopes = fields["envelopes"]
    raw_assessments = fields["assessments"]
    raw_sampling_candidates = fields["sampling_candidates"]
    if (
        type(raw_cases) is not tuple
        or type(raw_envelopes) is not tuple
        or type(raw_assessments) is not tuple
        or type(raw_sampling_candidates) is not tuple
        or not 1 <= len(raw_cases) <= _MAX_CASES
        or len(raw_cases) != len(raw_envelopes)
        or len(raw_cases) != len(raw_assessments)
        or len(raw_cases) != len(raw_sampling_candidates)
        or type(fields["documents"]) is not tuple
        or not fields["documents"]
        or type(fields["source_documents"]) is not tuple
        or not fields["source_documents"]
        or type(fields["manifest_bytes"]) is not bytes
        or type(fields["parser_quarantines_bytes"]) is not bytes
        or type(fields["parser_quarantines_sha256"]) is not str
        or type(fields["quarantine_count"]) is not int
        or fields["quarantine_count"] < 0
        or not isinstance(fields["parser_authority_sha256"], str)
        or not isinstance(fields["raw_authority_sha256"], str)
        or not isinstance(fields["manifest_sha256"], str)
        or type(fields["document_page_counts"]) is not dict
        or _SHA256_RE.fullmatch(fields["parser_authority_sha256"]) is None
        or _SHA256_RE.fullmatch(fields["raw_authority_sha256"]) is None
        or _SHA256_RE.fullmatch(fields["manifest_sha256"]) is None
        or not hmac.compare_digest(
            hashlib.sha256(fields["manifest_bytes"]).hexdigest(),
            fields["manifest_sha256"],
        )
        or _SHA256_RE.fullmatch(fields["parser_quarantines_sha256"]) is None
        or not hmac.compare_digest(
            hashlib.sha256(fields["parser_quarantines_bytes"]).hexdigest(),
            fields["parser_quarantines_sha256"],
        )
    ):
        return None
    resolution_values = (
        fields["resolution_authority"],
        fields["resolution_authority_bytes"],
        fields["resolution_authority_sha256"],
    )
    resolution_all_none = all(value is None for value in resolution_values)
    resolution_all_present = all(value is not None for value in resolution_values)
    checked_resolution: VerifiedQuarantineResolutionAuthority | None = None
    if not resolution_all_none:
        raw_resolution, resolution_bytes, resolution_sha256 = resolution_values
        if (
            not resolution_all_present
            or type(raw_resolution) is not VerifiedQuarantineResolutionAuthority
            or type(resolution_bytes) is not bytes
            or type(resolution_sha256) is not str
            or _SHA256_RE.fullmatch(resolution_sha256) is None
            or not hmac.compare_digest(
                hashlib.sha256(resolution_bytes).hexdigest(), resolution_sha256
            )
            or not hmac.compare_digest(raw_resolution.to_bytes(), resolution_bytes)
            or not hmac.compare_digest(
                raw_resolution.external_sha256, resolution_sha256
            )
            or fields["quarantine_count"] != 0
            or fields["parser_quarantines_bytes"] != b""
            or raw_resolution.quarantine_count <= 0
            or raw_resolution.manifest_sha256 != fields["manifest_sha256"]
            or raw_resolution.raw_authority_sha256 != fields["raw_authority_sha256"]
            or raw_resolution.parser_authority_sha256
            == fields["parser_authority_sha256"]
            or any(
                item.disposition == "unresolved" for item in raw_resolution.resolutions
            )
        ):
            return None
        checked_resolution = raw_resolution
    documents = tuple(
        _revalidate_document(document) for document in fields["documents"]
    )
    cases = tuple(_revalidate_case(case) for case in raw_cases)
    assessments = tuple(
        _revalidate_assessment(assessment) for assessment in raw_assessments
    )
    if any(item is None for item in (*documents, *cases, *assessments)):
        return None
    approved_documents = cast(tuple[Document, ...], documents)
    source_documents = tuple(
        revalidate_source_document(document) for document in fields["source_documents"]
    )
    if any(document is None for document in source_documents):
        return None
    approved_source_documents = cast(tuple[SourceDocument, ...], source_documents)
    try:
        manifest_bytes = fields["manifest_bytes"]
        parsed_source_documents: tuple[SourceDocument, ...]
        prefix = b"sen-qa-source-document-v1\0"
        if manifest_bytes.startswith(prefix):
            if len(approved_source_documents) != 1:
                return None
            parsed_source_documents = (
                SourceDocument.model_validate_json(manifest_bytes[len(prefix) :]),
            )
            if _document_manifest_bytes(parsed_source_documents[0]) != manifest_bytes:
                return None
        else:
            parsed_source_documents = SourceManifest.model_validate_json(
                manifest_bytes
            ).documents
    except (TypeError, ValueError):
        return None
    approved_source_ids = {document.doc_id for document in approved_source_documents}
    manifest_source_subset = tuple(
        document
        for document in parsed_source_documents
        if document.doc_id in approved_source_ids
    )
    if manifest_source_subset != approved_source_documents:
        return None
    approved_cases = cast(tuple[Case, ...], cases)
    approved_assessments = cast(tuple[QualityAssessment, ...], assessments)
    sampling_candidates = tuple(
        _revalidate_sampling_candidate(item) for item in raw_sampling_candidates
    )
    if any(item is None for item in sampling_candidates):
        return None
    approved_sampling_candidates = cast(
        tuple[SamplingCandidate, ...], sampling_candidates
    )
    raw_authority_fields = (
        fields["ocr_authority_lock"],
        fields["ocr_authority_lock_bytes"],
        fields["ocr_authority_lock_sha256"],
        fields["ocr_authority_self_sha256"],
    )
    authority_all_none = all(value is None for value in raw_authority_fields)
    authority_all_present = all(value is not None for value in raw_authority_fields)
    has_ocr = any(
        document.extraction_method == "ocr" for document in approved_documents
    )
    checked_authority_lock: OcrAuthorityLock | None = None
    checked_authority_bytes: bytes | None = None
    checked_authority_sha256: str | None = None
    checked_authority_self_sha256: str | None = None
    if not has_ocr:
        if not authority_all_none:
            return None
    elif authority_all_none:
        if require_ocr_authority:
            return None
    elif not authority_all_present:
        return None
    else:
        raw_lock = fields["ocr_authority_lock"]
        raw_bytes = fields["ocr_authority_lock_bytes"]
        raw_sha256 = fields["ocr_authority_lock_sha256"]
        raw_self_sha256 = fields["ocr_authority_self_sha256"]
        canonical: bytes | None = None
        try:
            if type(raw_lock) is OcrAuthorityLock:
                canonical = canonical_ocr_authority_bytes(raw_lock)
        except (OcrAuthorityLockError, TypeError, ValueError):
            canonical = None
        if (
            type(raw_lock) is not OcrAuthorityLock
            or type(raw_bytes) is not bytes
            or type(raw_sha256) is not str
            or type(raw_self_sha256) is not str
            or _SHA256_RE.fullmatch(raw_sha256) is None
            or _SHA256_RE.fullmatch(raw_self_sha256) is None
            or canonical is None
            or not hmac.compare_digest(canonical, raw_bytes)
            or not hmac.compare_digest(
                hashlib.sha256(raw_bytes).hexdigest(),
                raw_sha256,
            )
            or not hmac.compare_digest(raw_lock.self_sha256, raw_self_sha256)
            or not _authority_matches_documents(raw_lock, approved_documents)
        ):
            return None
        checked_authority_lock = raw_lock
        checked_authority_bytes = raw_bytes
        checked_authority_sha256 = raw_sha256
        checked_authority_self_sha256 = raw_self_sha256
    page_counts: dict[str, DocumentPageCounts] = {}
    for doc_id, raw_count in cast(
        dict[object, object], fields["document_page_counts"]
    ).items():
        count_fields = _exact_fields(raw_count, DocumentPageCounts)
        if not isinstance(doc_id, str) or count_fields is None:
            return None
        try:
            page_counts[doc_id] = DocumentPageCounts.model_validate(count_fields)
        except (TypeError, ValueError):
            return None
    envelopes: list[VerifiedPromotionEnvelope] = []
    for raw_envelope, case in zip(raw_envelopes, approved_cases, strict=True):
        if type(raw_envelope) is not VerifiedPromotionEnvelope:
            return None
        try:
            envelope_fields = {
                field.name: object.__getattribute__(raw_envelope, field.name)
                for field in dataclass_fields(VerifiedPromotionEnvelope)
            }
        except (AttributeError, TypeError):
            return None
        if (
            set(envelope_fields)
            != {
                "canonical_bytes",
                "candidate_case",
                "corrections",
                "fingerprint_sha256",
                "parser_authority_sha256",
                "raw_authority_sha256",
                "role_sources",
            }
            or type(envelope_fields["canonical_bytes"]) is not bytes
            or type(envelope_fields["fingerprint_sha256"]) is not str
        ):
            return None
        try:
            checked = load_promotion_envelope(
                envelope_fields["canonical_bytes"],
                expected_sha256=envelope_fields["fingerprint_sha256"],
            )
        except (TypeError, ValueError):
            return None
        if (
            checked.candidate_case != case
            or checked.parser_authority_sha256 != fields["parser_authority_sha256"]
            or checked.raw_authority_sha256 != fields["raw_authority_sha256"]
        ):
            return None
        envelopes.append(checked)
    registry = fields["registry"]
    if type(registry) is not VerifiedCanonicalReviewRegistry:
        return None
    try:
        checked_registry = CanonicalReviewRegistry.from_bytes(
            registry.to_bytes(),
            expected_sha256=registry.fingerprint_sha256,
        )
        expected_references = tuple(
            sorted(
                (
                    ReviewReference(
                        case_id=case.case_id,
                        content_sha256=envelope.fingerprint_sha256,
                        source_locations=_review_locations(assessment, case),
                    )
                    for case, envelope, assessment in zip(
                        approved_cases,
                        envelopes,
                        approved_assessments,
                        strict=True,
                    )
                ),
                key=lambda reference: reference.case_id,
            )
        )
        expected_reference_by_id = {
            reference.case_id: reference for reference in expected_references
        }
    except (TypeError, ValueError):
        return None
    document_ids = tuple(document.doc_id for document in approved_documents)
    source_by_id = {document.doc_id: document for document in approved_source_documents}
    checked_quarantines = _revalidate_parser_quarantines_bytes(
        fields["parser_quarantines_bytes"],
        expected_count=fields["quarantine_count"],
        documents=approved_documents,
    )
    if (
        document_ids != tuple(sorted(set(document_ids)))
        or tuple(source_by_id) != document_ids
        or any(
            _canonical_document(source, document.ingestion_version) != document
            for source, document in zip(
                approved_source_documents, approved_documents, strict=True
            )
        )
        or set(page_counts) != set(document_ids)
        or any(
            count.succeeded + count.quarantined + count.failed
            != document.pdf_page_count
            for document in approved_documents
            for count in (page_counts[document.doc_id],)
        )
        or any(
            sampling.doc_id != case.doc_id
            or sampling.source_sha256 != source_by_id[case.doc_id].sha256
            or (
                sampling.extraction_source == "ocr"
                and (
                    (
                        sampling.edition_year in {2024, 2025}
                        and (
                            not sampling.layout_segment_provenances
                            or {
                                item.pdf_page_index
                                for item in sampling.layout_segment_provenances
                            }
                            != {span.pdf_page_index for span in case.source_spans}
                            or any(
                                item.doc_id != case.doc_id
                                or item.edition_year != sampling.edition_year
                                or item.source_sha256 != sampling.source_sha256
                                for item in sampling.layout_segment_provenances
                            )
                        )
                    )
                    or (
                        sampling.edition_year not in {2024, 2025}
                        and bool(sampling.layout_segment_provenances)
                    )
                    or sampling.native_layout_segment is not None
                )
            )
            or (
                sampling.extraction_source == "native"
                and bool(sampling.layout_segment_provenances)
            )
            or (
                sampling.extraction_source == "native"
                and sampling.edition_year in {2020, 2021, 2022}
                and (
                    sampling.native_layout_segment is None
                    or any(
                        not (
                            sampling.native_layout_segment.start_pdf_page
                            <= span.pdf_page_index
                            <= sampling.native_layout_segment.end_pdf_page
                        )
                        for span in case.source_spans
                    )
                    or sampling.native_layout_segment
                    not in source_by_id[case.doc_id].native_review_layout_segments
                )
            )
            for case, sampling in zip(
                approved_cases, approved_sampling_candidates, strict=True
            )
        )
        or not {case.doc_id for case in approved_cases}.issubset(set(document_ids))
        or checked_registry.cases != expected_references
        or (
            checked_resolution is not None
            and checked_resolution.registry_sha256
            == checked_registry.fingerprint_sha256
        )
        or any(
            assessment.case_id != case.case_id
            for case, assessment in zip(
                approved_cases, approved_assessments, strict=True
            )
        )
        or any(
            sampling.reference != expected_reference_by_id.get(case.case_id)
            or sampling.reference.content_sha256 != envelope.fingerprint_sha256
            or sampling.edition_year
            != next(
                document.edition_year
                for document in approved_documents
                if document.doc_id == case.doc_id
            )
            or sampling.extraction_source != case.extraction_source
            or sampling.pii_class != case.pii_class
            or sampling.review_status != case.review_status
            for case, envelope, sampling in zip(
                approved_cases,
                envelopes,
                approved_sampling_candidates,
                strict=True,
            )
        )
        or checked_quarantines is None
    ):
        return None
    checked_batch = object.__new__(PreparedReviewBatch)
    object.__setattr__(checked_batch, "documents", approved_documents)
    object.__setattr__(checked_batch, "source_documents", approved_source_documents)
    object.__setattr__(checked_batch, "cases", approved_cases)
    object.__setattr__(checked_batch, "envelopes", tuple(envelopes))
    object.__setattr__(checked_batch, "assessments", approved_assessments)
    object.__setattr__(
        checked_batch, "sampling_candidates", approved_sampling_candidates
    )
    object.__setattr__(checked_batch, "registry", checked_registry)
    object.__setattr__(
        checked_batch,
        "parser_authority_sha256",
        fields["parser_authority_sha256"],
    )
    object.__setattr__(
        checked_batch,
        "raw_authority_sha256",
        fields["raw_authority_sha256"],
    )
    object.__setattr__(
        checked_batch,
        "ocr_authority_lock",
        checked_authority_lock,
    )
    object.__setattr__(
        checked_batch,
        "ocr_authority_lock_bytes",
        checked_authority_bytes,
    )
    object.__setattr__(
        checked_batch,
        "ocr_authority_lock_sha256",
        checked_authority_sha256,
    )
    object.__setattr__(
        checked_batch,
        "ocr_authority_self_sha256",
        checked_authority_self_sha256,
    )
    object.__setattr__(checked_batch, "manifest_sha256", fields["manifest_sha256"])
    object.__setattr__(checked_batch, "manifest_bytes", fields["manifest_bytes"])
    object.__setattr__(checked_batch, "document_page_counts", page_counts)
    object.__setattr__(
        checked_batch,
        "parser_quarantines_bytes",
        fields["parser_quarantines_bytes"],
    )
    object.__setattr__(
        checked_batch,
        "parser_quarantines_sha256",
        fields["parser_quarantines_sha256"],
    )
    object.__setattr__(checked_batch, "quarantine_count", fields["quarantine_count"])
    object.__setattr__(
        checked_batch, "resolution_authority", fields["resolution_authority"]
    )
    object.__setattr__(
        checked_batch,
        "resolution_authority_bytes",
        fields["resolution_authority_bytes"],
    )
    object.__setattr__(
        checked_batch,
        "resolution_authority_sha256",
        fields["resolution_authority_sha256"],
    )
    return checked_batch


def _write_review_package(root: Path, *, release_id: str, batch: object) -> Path:
    """Write one owner-only review package without replacing an existing package."""
    if (
        not isinstance(root, Path)
        or not root.is_dir()
        or root.is_symlink()
        or not _RELEASE_ID_RE.fullmatch(release_id)
    ):
        _raise("staging_write_invalid")
    approved_batch = _revalidate_batch(batch)
    if approved_batch is None or (
        approved_batch.resolution_authority is not None
        and getattr(approved_batch.resolution_authority, "release_id", None)
        != release_id
    ):
        _raise("staging_write_invalid")
    if approved_batch.resolution_authority is not None:
        try:
            if any(root.iterdir()):
                _raise("staging_write_invalid")
        except OSError:
            _raise("staging_write_invalid")
    package = root / "review"
    try:
        os.mkdir(package, 0o700)
    except FileExistsError:
        _raise("review_package_exists")
    except OSError:
        _raise("staging_write_failed")
    try:
        os.chmod(package, 0o700)
        candidates_dir = package / "candidates"
        os.mkdir(candidates_dir, 0o700)
        os.chmod(candidates_dir, 0o700)
        _write_private(package / "registry.json", approved_batch.registry.to_bytes())
        sampling_authority = build_sampling_authority(
            release_id=release_id,
            registry=approved_batch.registry,
            parser_authority_sha256=approved_batch.parser_authority_sha256,
            raw_authority_sha256=approved_batch.raw_authority_sha256,
            manifest_sha256=approved_batch.manifest_sha256,
            candidates=approved_batch.sampling_candidates,
        )
        _write_private(
            package / "sampling-authority.json",
            sampling_authority.to_bytes(),
        )
        document_payload = {
            "documents": [
                document.model_dump(mode="json")
                for document in approved_batch.documents
            ],
            "schema_version": "sen-qa-review-documents/v1",
        }
        _write_private(
            package / "documents.json",
            (
                json.dumps(
                    document_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii"),
        )
        has_ocr_authority = approved_batch.ocr_authority_lock_bytes is not None
        has_parser_quarantines = approved_batch.quarantine_count > 0
        has_resolution_authority = approved_batch.resolution_authority_bytes is not None
        if has_ocr_authority:
            _write_private(
                package / "ocr-authority-lock.json",
                cast(bytes, approved_batch.ocr_authority_lock_bytes),
            )
        if has_parser_quarantines:
            _write_private(
                package / "parser-quarantines.jsonl",
                approved_batch.parser_quarantines_bytes,
            )
        if has_resolution_authority:
            _write_private(
                package / "parser-quarantine-resolutions.json",
                cast(bytes, approved_batch.resolution_authority_bytes),
            )
        evidence_payload: dict[str, object] = {
            "document_page_counts": {
                doc_id: count.model_dump(mode="json")
                for doc_id, count in sorted(approved_batch.document_page_counts.items())
            },
            "manifest_sha256": approved_batch.manifest_sha256,
            "parser_quarantine_count": approved_batch.quarantine_count,
            "parser_authority_sha256": approved_batch.parser_authority_sha256,
            "raw_authority_sha256": approved_batch.raw_authority_sha256,
            "schema_version": (
                "sen-qa-ingestion-evidence/v4"
                if has_resolution_authority
                else (
                    "sen-qa-ingestion-evidence/v3"
                    if has_parser_quarantines
                    else (
                        "sen-qa-ingestion-evidence/v2"
                        if has_ocr_authority
                        else "sen-qa-ingestion-evidence/v1"
                    )
                )
            ),
        }
        if has_parser_quarantines:
            evidence_payload["parser_quarantines_sha256"] = (
                approved_batch.parser_quarantines_sha256
            )
        if has_ocr_authority:
            evidence_payload.update(
                {
                    "ocr_authority_lock_sha256": cast(
                        str,
                        approved_batch.ocr_authority_lock_sha256,
                    ),
                    "ocr_authority_self_sha256": cast(
                        str,
                        approved_batch.ocr_authority_self_sha256,
                    ),
                }
            )
        if has_resolution_authority:
            resolution = cast(
                VerifiedQuarantineResolutionAuthority,
                approved_batch.resolution_authority,
            )
            evidence_payload.update(
                {
                    "resolution_authority_sha256": cast(
                        str, approved_batch.resolution_authority_sha256
                    ),
                    "resolved_from_parser_authority_sha256": (
                        resolution.parser_authority_sha256
                    ),
                    "resolved_from_parser_quarantines_sha256": (
                        resolution.parser_quarantines_sha256
                    ),
                    "resolved_from_registry_sha256": resolution.registry_sha256,
                }
            )
        _write_private(
            package / "ingestion-evidence.json",
            (
                json.dumps(
                    evidence_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii"),
        )
        for case, envelope in zip(
            approved_batch.cases, approved_batch.envelopes, strict=True
        ):
            _write_private(
                candidates_dir / f"{case.case_id}.json",
                envelope.canonical_bytes,
            )
        queue_rows = []
        for assessment in approved_batch.assessments:
            queue_rows.append(
                {
                    "case_id": assessment.case_id,
                    "findings": [
                        {
                            "count": finding.count,
                            "page_id": finding.page_id,
                            "reason_code": finding.reason_code,
                        }
                        for finding in assessment.findings
                    ],
                    "page_ids": list(assessment.page_ids),
                    "target_review_status": assessment.target_review_status,
                }
            )
        queue_bytes = b"".join(
            (
                json.dumps(
                    row,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
            for row in queue_rows
        )
        _write_private(package / "review-queue.jsonl", queue_bytes)
        database_path = package / "review.sqlite3"
        _write_private(database_path, b"")
        with ReviewStore(
            database_path,
            canonical_registry=approved_batch.registry,
        ) as store:
            for assessment, envelope in zip(
                approved_batch.assessments,
                approved_batch.envelopes,
                strict=True,
            ):
                reason = (
                    assessment.findings[0].reason_code
                    if assessment.findings
                    else "human-review-required"
                )
                store.enqueue(
                    assessment.case_id,
                    content_sha256=envelope.fingerprint_sha256,
                    reason=reason,
                )
        os.chmod(database_path, 0o600)
        summary: dict[str, object] = {
            "case_count": len(approved_batch.cases),
            "document_count": len(approved_batch.documents),
            "manifest_sha256": approved_batch.manifest_sha256,
            "parser_authority_sha256": approved_batch.parser_authority_sha256,
            "quarantine_count": approved_batch.quarantine_count,
            "raw_authority_sha256": approved_batch.raw_authority_sha256,
            "registry_sha256": approved_batch.registry.fingerprint_sha256,
            "release_id": release_id,
            "schema_version": (
                "sen-qa-review-package/v4"
                if has_resolution_authority
                else (
                    "sen-qa-review-package/v3"
                    if has_parser_quarantines
                    else (
                        "sen-qa-review-package/v2"
                        if has_ocr_authority
                        else "sen-qa-review-package/v1"
                    )
                )
            ),
        }
        if has_parser_quarantines:
            summary["parser_quarantines_sha256"] = (
                approved_batch.parser_quarantines_sha256
            )
        if has_ocr_authority:
            summary["ocr_authority_lock_sha256"] = cast(
                str,
                approved_batch.ocr_authority_lock_sha256,
            )
        if has_resolution_authority:
            summary["resolution_authority_sha256"] = cast(
                str, approved_batch.resolution_authority_sha256
            )
        _write_private(
            package / "summary.json",
            (
                json.dumps(
                    summary,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii"),
        )
        directory_fd = os.open(package, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        shutil.rmtree(package, ignore_errors=True)
        _raise("staging_write_failed")
    return package


def write_review_package(root: Path, *, release_id: str, batch: object) -> Path:
    """Write a package through one cause-free, value-free public boundary."""
    code: str | None = None
    try:
        return _write_review_package(root, release_id=release_id, batch=batch)
    except StagingError as error:
        code = (
            str(error)
            if str(error)
            in {
                "review_package_exists",
                "staging_write_failed",
                "staging_write_invalid",
            }
            else "staging_write_failed"
        )
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        code = "staging_write_failed"
    _raise(code or "staging_write_failed")


def _export_review_ready(
    package: Path,
    *,
    release_id: str,
    expected_registry_sha256: str,
) -> Path:
    if (
        not isinstance(package, Path)
        or not package.is_dir()
        or package.is_symlink()
        or not _RELEASE_ID_RE.fullmatch(release_id)
        or not _SHA256_RE.fullmatch(expected_registry_sha256)
    ):
        _raise("review_export_invalid")
    attestation_path = package / "review-ready.attestation.json"
    snapshot_path = package / "review-decision-snapshot.json"
    if attestation_path.exists() or attestation_path.is_symlink():
        _raise("review_attestation_exists")
    registry_raw = _read_private(package / "registry.json")
    documents_raw = _read_private(package / "documents.json")
    evidence_raw = _read_private(package / "ingestion-evidence.json")
    if registry_raw is None or documents_raw is None or evidence_raw is None:
        _raise("review_export_invalid")
    authority_valid, ocr_authority_lock_sha256 = _review_package_authority_sha256(
        package,
        evidence_raw=evidence_raw,
        documents_raw=documents_raw,
        expected_release_id=release_id,
        expected_registry_sha256=expected_registry_sha256,
    )
    if not authority_valid:
        _raise("review_export_invalid")
    checked_evidence = _json_object(evidence_raw)
    if (
        checked_evidence is None
        or type(checked_evidence.get("parser_quarantine_count")) is not int
    ):
        _raise("review_export_invalid")
    if cast(int, checked_evidence["parser_quarantine_count"]) > 0:
        _raise("review_not_ready")
    resolution_authority_sha256 = checked_evidence.get("resolution_authority_sha256")
    if checked_evidence.get("schema_version") == "sen-qa-ingestion-evidence/v4" and (
        type(resolution_authority_sha256) is not str
        or _SHA256_RE.fullmatch(resolution_authority_sha256) is None
    ):
        _raise("review_export_invalid")
    try:
        registry = CanonicalReviewRegistry.from_bytes(
            registry_raw,
            expected_sha256=expected_registry_sha256,
        )
    except (TypeError, ValueError):
        _raise("review_export_invalid")
    candidate_directory = package / "candidates"
    if not candidate_directory.is_dir() or candidate_directory.is_symlink():
        _raise("review_export_invalid")
    expected_names = {f"{reference.case_id}.json" for reference in registry.cases}
    try:
        actual_names = {path.name for path in candidate_directory.iterdir()}
    except OSError:
        _raise("review_export_invalid")
    if actual_names != expected_names:
        _raise("review_export_invalid")
    envelope_hashes: dict[str, str] = {}
    for reference in registry.cases:
        raw = _read_private(candidate_directory / f"{reference.case_id}.json")
        if raw is None:
            _raise("review_export_invalid")
        try:
            envelope = load_promotion_envelope(
                raw,
                expected_sha256=reference.content_sha256,
            )
        except (TypeError, ValueError):
            _raise("review_export_invalid")
        if envelope.candidate_case.case_id != reference.case_id:
            _raise("review_export_invalid")
        envelope_hashes[reference.case_id] = envelope.fingerprint_sha256
    try:
        with ReviewStore(package / "review.sqlite3") as store:
            snapshot_raw = store.export_decision_snapshot()
    except (OSError, ReviewError, sqlite3.Error):
        _raise("review_not_ready")
    snapshot_sha256 = hashlib.sha256(snapshot_raw).hexdigest()
    try:
        snapshot = load_review_decision_snapshot(
            snapshot_raw,
            expected_sha256=snapshot_sha256,
        )
    except (TypeError, ValueError):
        _raise("review_export_invalid")
    snapshot_bindings = {
        case.case_id: case.promotion_envelope_sha256 for case in snapshot.cases
    }
    if (
        snapshot.registry_fingerprint_sha256 != expected_registry_sha256
        or snapshot_bindings != envelope_hashes
    ):
        _raise("review_export_invalid")
    existing_snapshot = _read_private(snapshot_path)
    if existing_snapshot is None:
        _write_private(snapshot_path, snapshot_raw)
    elif existing_snapshot != snapshot_raw:
        _raise("review_export_invalid")
    candidate_binding_bytes = json.dumps(
        envelope_hashes,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    statuses = tuple(
        cast(str, case.review_record["review_status"]) for case in snapshot.cases
    )
    attestation: dict[str, object] = {
        "approved_count": statuses.count("approved"),
        "candidate_binding_sha256": hashlib.sha256(candidate_binding_bytes).hexdigest(),
        "case_count": len(snapshot.cases),
        "documents_sha256": hashlib.sha256(documents_raw).hexdigest(),
        "ingestion_evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        "registry_sha256": expected_registry_sha256,
        "rejected_count": statuses.count("rejected"),
        "release_id": release_id,
        "schema_version": (
            "sen-qa-review-ready-attestation/v3"
            if resolution_authority_sha256 is not None
            else (
                "sen-qa-review-ready-attestation/v2"
                if ocr_authority_lock_sha256 is not None
                else "sen-qa-review-ready-attestation/v1"
            )
        ),
        "snapshot_sha256": snapshot_sha256,
    }
    if ocr_authority_lock_sha256 is not None:
        attestation["ocr_authority_lock_sha256"] = ocr_authority_lock_sha256
    if resolution_authority_sha256 is not None:
        attestation["resolution_authority_sha256"] = resolution_authority_sha256
    _write_private(
        attestation_path,
        (
            json.dumps(
                attestation,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii"),
    )
    os.chmod(snapshot_path, 0o600)
    os.chmod(attestation_path, 0o600)
    directory_fd = os.open(package, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return attestation_path


def export_review_ready(
    package: Path,
    *,
    release_id: str,
    expected_registry_sha256: str,
) -> Path:
    """Export the terminal review snapshot and its final commit marker."""
    code: str | None = None
    try:
        return _export_review_ready(
            package,
            release_id=release_id,
            expected_registry_sha256=expected_registry_sha256,
        )
    except StagingError as error:
        code = (
            str(error)
            if str(error)
            in {
                "review_attestation_exists",
                "review_export_invalid",
                "review_not_ready",
            }
            else "review_export_invalid"
        )
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        code = "review_export_invalid"
    _raise(code or "review_export_invalid")
