"""Fail-closed bridge from parsed candidates to the independent review store."""

from __future__ import annotations

import hashlib
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
from typing import NoReturn, cast

from src.corpus.chunking import ChunkRole, RoleSource, role_source_manifest_bytes
from src.corpus.ids import make_case_id
from src.corpus.models import Case, Document, DocumentPageCounts, SourceSpan
from src.corpus.storage import (
    VerifiedPromotionEnvelope,
    load_promotion_envelope,
    load_review_decision_snapshot,
)
from src.ingestion.extract_common import revalidate_source_document
from src.ingestion.manifest import SourceDocument, load_manifest
from src.ingestion.parse_common import (
    ParsedCaseCandidate,
    ParseResult,
    ParserLine,
    ParserPage,
    RoleFragment,
    canonical_result_bytes,
)
from src.ingestion.parse_metadata import VerifiedParseRun, build_parse_run
from src.ingestion.privacy import classify_privacy, scan_text
from src.ingestion.quality import QualityAssessment, QualityFinding, assess_case
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewError,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    VerifiedCanonicalReviewRegistry,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_INGESTION_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MAX_CASES = 10_000
_MAX_LINES = 250_000
_MAX_REVIEW_FILE_BYTES = 16 * 1024 * 1024


class StagingError(ValueError):
    """A value-free corpus review staging failure."""


def _raise(code: str) -> NoReturn:
    raise StagingError(code) from None


@dataclass(frozen=True, slots=True, init=False)
class PreparedReviewBatch:
    """Sealed, internally derived inputs for one review package."""

    documents: tuple[Document, ...]
    cases: tuple[Case, ...]
    envelopes: tuple[VerifiedPromotionEnvelope, ...]
    assessments: tuple[QualityAssessment, ...]
    registry: VerifiedCanonicalReviewRegistry
    parser_authority_sha256: str
    raw_authority_sha256: str
    manifest_sha256: str
    document_page_counts: dict[str, DocumentPageCounts]
    quarantine_count: int


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


def _document_manifest_sha256(source: SourceDocument) -> str:
    payload = json.dumps(
        source.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(b"sen-qa-source-document-v1\0" + payload).hexdigest()


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
    if any(item is None for item in (*checked_pages, *checked_candidates)):
        _raise("staging_input_invalid")
    approved_pages = cast(tuple[ParserPage, ...], checked_pages)
    candidates = cast(tuple[ParsedCaseCandidate, ...], checked_candidates)
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
        references.append(
            ReviewReference(
                case_id=case.case_id,
                content_sha256=envelope.fingerprint_sha256,
                source_locations=_review_locations(assessment, case),
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
    object.__setattr__(batch, "cases", tuple(cases))
    object.__setattr__(batch, "envelopes", tuple(envelopes))
    object.__setattr__(batch, "assessments", tuple(assessments))
    object.__setattr__(batch, "registry", verified_registry)
    object.__setattr__(batch, "parser_authority_sha256", parser_authority_sha256)
    object.__setattr__(batch, "raw_authority_sha256", raw_authority_sha256)
    object.__setattr__(
        batch, "manifest_sha256", _document_manifest_sha256(approved_document)
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
    object.__setattr__(batch, "quarantine_count", len(result.quarantines))
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


def _prepare_review_corpus(
    runs: object,
    *,
    ingestion_version: str,
) -> PreparedReviewBatch:
    """Combine manifest-bound annual parse runs under two corpus-wide authorities."""
    if (
        type(runs) is not tuple
        or not 1 <= len(runs) <= 64
        or any(type(run) is not VerifiedParseRun for run in runs)
        or not _INGESTION_VERSION_RE.fullmatch(ingestion_version)
    ):
        _raise("staging_input_invalid")
    checked_runs = cast(tuple[VerifiedParseRun, ...], runs)
    if sum(len(run.result.cases) for run in checked_runs) > _MAX_CASES:
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
        parser_authority_sha256 = hashlib.sha256(
            b"sen-qa-parser-corpus-authority-v1\0" + authority_bytes
        ).hexdigest()
        raw_authority_sha256 = hashlib.sha256(
            b"sen-qa-raw-corpus-authority-v1\0" + authority_bytes
        ).hexdigest()
        batches = tuple(
            prepare_review_batch(
                document=run.document,
                result=run.result,
                pages=run.pages,
                parser_authority_sha256=parser_authority_sha256,
                raw_authority_sha256=raw_authority_sha256,
                ingestion_version=ingestion_version,
            )
            for run in ordered_runs
        )
    except (RecursionError, TypeError, ValueError):
        _raise("staging_input_invalid")
    rows = sorted(
        (
            (case, envelope, assessment)
            for batch in batches
            for case, envelope, assessment in zip(
                batch.cases,
                batch.envelopes,
                batch.assessments,
                strict=True,
            )
        ),
        key=lambda row: row[0].case_id,
    )
    if len(rows) != len({case.case_id for case, _, _ in rows}):
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
    object.__setattr__(
        batch,
        "documents",
        tuple(document for item in batches for document in item.documents),
    )
    object.__setattr__(batch, "cases", tuple(row[0] for row in rows))
    object.__setattr__(batch, "envelopes", tuple(row[1] for row in rows))
    object.__setattr__(batch, "assessments", tuple(row[2] for row in rows))
    object.__setattr__(batch, "registry", verified_registry)
    object.__setattr__(batch, "parser_authority_sha256", parser_authority_sha256)
    object.__setattr__(batch, "raw_authority_sha256", raw_authority_sha256)
    object.__setattr__(
        batch,
        "manifest_sha256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    object.__setattr__(
        batch,
        "document_page_counts",
        {
            doc_id: count
            for item in batches
            for doc_id, count in item.document_page_counts.items()
        },
    )
    object.__setattr__(
        batch,
        "quarantine_count",
        sum(item.quarantine_count for item in batches),
    )
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


def _prepare_review_corpus_from_artifacts(
    input_root: Path,
    *,
    manifest_path: Path,
    ingestion_version: str,
    expected_image_digest: str | None,
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
                expected_image_digest=(
                    None
                    if document.extraction_method == "native"
                    else expected_image_digest
                ),
            )
        except (OSError, RecursionError, OverflowError, TypeError, ValueError):
            _raise("staging_input_invalid")
        runs.append(run)
    return prepare_review_corpus(tuple(runs), ingestion_version=ingestion_version)


def prepare_review_corpus_from_artifacts(
    input_root: Path,
    *,
    manifest_path: Path,
    ingestion_version: str,
    expected_image_digest: str | None,
) -> PreparedReviewBatch:
    """Load managed artifacts through one cause-free public boundary."""
    code: str | None = None
    try:
        return _prepare_review_corpus_from_artifacts(
            input_root,
            manifest_path=manifest_path,
            ingestion_version=ingestion_version,
            expected_image_digest=expected_image_digest,
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
    path: Path, *, max_bytes: int = _MAX_REVIEW_FILE_BYTES
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
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
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _revalidate_batch(value: object) -> PreparedReviewBatch | None:
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
        "envelopes",
        "manifest_sha256",
        "parser_authority_sha256",
        "quarantine_count",
        "raw_authority_sha256",
        "registry",
    }
    if set(fields) != expected_fields:
        return None
    raw_cases = fields["cases"]
    raw_envelopes = fields["envelopes"]
    raw_assessments = fields["assessments"]
    if (
        type(raw_cases) is not tuple
        or type(raw_envelopes) is not tuple
        or type(raw_assessments) is not tuple
        or not 1 <= len(raw_cases) <= _MAX_CASES
        or len(raw_cases) != len(raw_envelopes)
        or len(raw_cases) != len(raw_assessments)
        or type(fields["documents"]) is not tuple
        or not fields["documents"]
        or type(fields["quarantine_count"]) is not int
        or fields["quarantine_count"] < 0
        or not isinstance(fields["parser_authority_sha256"], str)
        or not isinstance(fields["raw_authority_sha256"], str)
        or not isinstance(fields["manifest_sha256"], str)
        or type(fields["document_page_counts"]) is not dict
        or _SHA256_RE.fullmatch(fields["parser_authority_sha256"]) is None
        or _SHA256_RE.fullmatch(fields["raw_authority_sha256"]) is None
        or _SHA256_RE.fullmatch(fields["manifest_sha256"]) is None
    ):
        return None
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
    approved_cases = cast(tuple[Case, ...], cases)
    approved_assessments = cast(tuple[QualityAssessment, ...], assessments)
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
    except (TypeError, ValueError):
        return None
    document_ids = tuple(document.doc_id for document in approved_documents)
    if (
        document_ids != tuple(sorted(set(document_ids)))
        or set(page_counts) != set(document_ids)
        or any(
            count.succeeded + count.quarantined + count.failed
            != document.pdf_page_count
            for document in approved_documents
            for count in (page_counts[document.doc_id],)
        )
        or {case.doc_id for case in approved_cases} != set(document_ids)
        or checked_registry.cases != expected_references
        or any(
            assessment.case_id != case.case_id
            for case, assessment in zip(
                approved_cases, approved_assessments, strict=True
            )
        )
    ):
        return None
    checked_batch = object.__new__(PreparedReviewBatch)
    object.__setattr__(checked_batch, "documents", approved_documents)
    object.__setattr__(checked_batch, "cases", approved_cases)
    object.__setattr__(checked_batch, "envelopes", tuple(envelopes))
    object.__setattr__(checked_batch, "assessments", approved_assessments)
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
    object.__setattr__(checked_batch, "manifest_sha256", fields["manifest_sha256"])
    object.__setattr__(checked_batch, "document_page_counts", page_counts)
    object.__setattr__(checked_batch, "quarantine_count", fields["quarantine_count"])
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
    if approved_batch is None:
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
        evidence_payload = {
            "document_page_counts": {
                doc_id: count.model_dump(mode="json")
                for doc_id, count in sorted(approved_batch.document_page_counts.items())
            },
            "manifest_sha256": approved_batch.manifest_sha256,
            "parser_authority_sha256": approved_batch.parser_authority_sha256,
            "raw_authority_sha256": approved_batch.raw_authority_sha256,
            "schema_version": "sen-qa-ingestion-evidence/v1",
        }
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
        summary = {
            "case_count": len(approved_batch.cases),
            "document_count": len(approved_batch.documents),
            "manifest_sha256": approved_batch.manifest_sha256,
            "parser_authority_sha256": approved_batch.parser_authority_sha256,
            "quarantine_count": approved_batch.quarantine_count,
            "raw_authority_sha256": approved_batch.raw_authority_sha256,
            "registry_sha256": approved_batch.registry.fingerprint_sha256,
            "release_id": release_id,
            "schema_version": "sen-qa-review-package/v1",
        }
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
    if registry_raw is None:
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
    attestation = {
        "approved_count": statuses.count("approved"),
        "candidate_binding_sha256": hashlib.sha256(candidate_binding_bytes).hexdigest(),
        "case_count": len(snapshot.cases),
        "registry_sha256": expected_registry_sha256,
        "rejected_count": statuses.count("rejected"),
        "release_id": release_id,
        "schema_version": "sen-qa-review-ready-attestation/v1",
        "snapshot_sha256": snapshot_sha256,
    }
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
