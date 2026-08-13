"""Deterministic native text extraction for the approved 2020-2022 PDFs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Final, Literal, Self, TypeAlias

import pymupdf
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingestion.extract_common import (
    APPROVED_PAGE_POLICIES,
    PAGE_RECORD_SCHEMA_VERSION,
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
    exact_declared_field_mapping,
    printed_page_label,
    revalidate_raw_page,
    revalidate_source_document,
)
from src.ingestion.manifest import SourceDocument

RIGHT_MARGIN_REPETITION_FRACTION = 0.40
"""2020 navigation requires occurrence on at least 40% of body pages."""

HEADER_FOOTER_REPETITION_FRACTION = 0.50
"""Conservative 2021-22 template threshold: a majority of body pages."""

_WHITESPACE_RE = re.compile(r"\s+")
_TEXT_BLOCK_TYPE = 0
_RENDER_HASH_PREFIX = b"sen-qa-native-render-v1\0rgb8\0"
_REMOVAL_SIGNATURE_HASH_PREFIX = b"sen-qa-native-removal-signature-v1\0"
REMOVAL_ALGORITHM_VERSION: Final = "native-template-filter-v1"


def _supported_pymupdf_errors() -> tuple[type[BaseException], ...]:
    """Return the installed PyMuPDF exception family without a broad RuntimeError catch."""
    mupdf_module = getattr(pymupdf, "mupdf", None)
    candidates = (
        getattr(pymupdf, "FileDataError", None),
        getattr(pymupdf, "EmptyFileError", None),
        getattr(mupdf_module, "FzErrorBase", None),
    )
    return tuple(
        candidate
        for candidate in candidates
        if isinstance(candidate, type) and issubclass(candidate, BaseException)
    )


PYMUPDF_ERRORS = _supported_pymupdf_errors()
DOCUMENT_IO_ERRORS = PYMUPDF_ERRORS + (OSError, ValueError)
PAGE_EXTRACTION_ERRORS = PYMUPDF_ERRORS + (
    OSError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)


class NativeExtractionError(Exception):
    """Raised for safe, document-level native extraction failures."""


class RemovedRawBlockEvidence(BaseModel):
    """Privacy-safe evidence explaining exactly why one raw block was omitted."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    raw_block_index: int = Field(ge=0)
    reason_code: Literal["blank-block", "repeated-template"]
    candidate_kind: Literal["right-margin", "header-footer"] | None
    signature_sha256: str | None
    observed_page_count: int | None
    body_page_count: int | None
    threshold_count: int | None
    algorithm_version: Literal["native-template-filter-v1"]

    @model_validator(mode="after")
    def has_complete_reason_evidence(self) -> Self:
        repeated_values = (
            self.candidate_kind,
            self.signature_sha256,
            self.observed_page_count,
            self.body_page_count,
            self.threshold_count,
        )
        if self.algorithm_version != REMOVAL_ALGORITHM_VERSION:
            raise ValueError("raw block removal algorithm is not approved")
        if self.reason_code == "blank-block":
            if any(value is not None for value in repeated_values):
                raise ValueError("blank block removal cannot claim repetition evidence")
            return self
        if any(value is None for value in repeated_values):
            raise ValueError("repeated block removal requires complete evidence")
        if self.signature_sha256 is None or not re.fullmatch(
            r"[0-9a-f]{64}", self.signature_sha256
        ):
            raise ValueError("repeated block signature digest is invalid")
        if (
            self.observed_page_count is None
            or self.body_page_count is None
            or self.threshold_count is None
            or self.observed_page_count < self.threshold_count
            or self.threshold_count < 1
            or self.observed_page_count > self.body_page_count
        ):
            raise ValueError("repeated block counts do not meet the approved threshold")
        return self


class CleanedPage(BaseModel):
    """A derived normalized projection while retaining the original raw page exactly."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    raw_page: RawPage
    normalized_text: str
    retained_raw_block_indexes: tuple[int, ...]
    removed_raw_block_evidence: tuple[RemovedRawBlockEvidence, ...]

    @model_validator(mode="after")
    def has_exact_raw_block_projection(self) -> Self:
        _validate_retained_projection(
            self.raw_page,
            self.retained_raw_block_indexes,
            self.normalized_text,
            self.removed_raw_block_evidence,
        )
        return self

    @property
    def raw_blocks(self) -> tuple[RawBlock, ...]:
        """Compatibility/readability projection; never a mutable cleaned copy."""
        return self.raw_page.raw_blocks


class ExtractedPageRecord(BaseModel):
    """A successfully extracted page, including raw provenance and clean projection."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_version: Literal[2]
    status: Literal["extracted"] = "extracted"
    doc_id: str
    edition_year: int
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_pdf_page_count: int = Field(ge=1)
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    raw_page: RawPage
    normalized_text: str
    retained_raw_block_indexes: tuple[int, ...]
    removed_raw_block_evidence: tuple[RemovedRawBlockEvidence, ...]

    @model_validator(mode="after")
    def has_matching_envelope_and_projection(self) -> Self:
        if (
            self.doc_id != self.raw_page.doc_id
            or self.edition_year != self.raw_page.edition_year
            or self.pdf_page_index != self.raw_page.pdf_page_index
            or self.page_label != self.raw_page.page_label
            or self.raw_page.extraction_source != "native"
            or self.pdf_page_index > self.document_pdf_page_count
        ):
            raise ValueError("native page envelope does not match raw provenance")
        _validate_retained_projection(
            self.raw_page,
            self.retained_raw_block_indexes,
            self.normalized_text,
            self.removed_raw_block_evidence,
        )
        return self


class QuarantinedPageRecord(BaseModel):
    """A page that could not be extracted, without fabricated empty text."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_version: Literal[2]
    status: Literal["quarantined"] = "quarantined"
    doc_id: str
    edition_year: int
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_pdf_page_count: int = Field(ge=1)
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    reason_code: Literal["page-extraction-failed"]

    @model_validator(mode="after")
    def remains_inside_document(self) -> Self:
        if self.pdf_page_index > self.document_pdf_page_count:
            raise ValueError("native quarantined page is outside document")
        return self


NativePageRecord: TypeAlias = ExtractedPageRecord | QuarantinedPageRecord


def _revalidate_native_record(value: object) -> NativePageRecord | None:
    model: type[ExtractedPageRecord | QuarantinedPageRecord]
    if type(value) is ExtractedPageRecord:
        model = ExtractedPageRecord
    elif type(value) is QuarantinedPageRecord:
        model = QuarantinedPageRecord
    else:
        return None
    fields = exact_declared_field_mapping(value, model)
    if fields is None:
        return None
    if model is ExtractedPageRecord:
        raw_page = revalidate_raw_page(fields["raw_page"])
        raw_evidence = fields["removed_raw_block_evidence"]
        if raw_page is None or type(raw_evidence) is not tuple:
            return None
        evidence = tuple(_revalidate_removed_evidence(item) for item in raw_evidence)
        if any(item is None for item in evidence):
            return None
        fields["raw_page"] = raw_page
        fields["removed_raw_block_evidence"] = tuple(
            item for item in evidence if item is not None
        )
    try:
        return model.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_removed_evidence(value: object) -> RemovedRawBlockEvidence | None:
    fields = exact_declared_field_mapping(value, RemovedRawBlockEvidence)
    if fields is None:
        return None
    try:
        return RemovedRawBlockEvidence.model_validate(fields)
    except (TypeError, ValueError):
        return None


def validate_native_page_record(value: object) -> NativePageRecord:
    """Return a recursively rebuilt record or raise a value-free boundary error."""
    record = _revalidate_native_record(value)
    if record is None:
        raise NativeExtractionError("native page record is invalid")
    return record


def normalize_text(text: str) -> str:
    """Normalize only the derived view; raw spans are never altered."""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def _block_text(block: RawBlock) -> str:
    return "\n".join("".join(span.text for span in line.spans) for line in block.lines)


def _validate_retained_projection(
    page: RawPage,
    indexes: tuple[int, ...],
    normalized_text: str,
    removal_evidence: tuple[RemovedRawBlockEvidence, ...],
) -> None:
    if indexes != tuple(sorted(set(indexes))) or any(
        index < 0 or index >= len(page.raw_blocks) for index in indexes
    ):
        raise ValueError(
            "retained raw block indexes must be unique, sorted, and in range"
        )
    retained = tuple(
        normalize_text(_block_text(page.raw_blocks[index])) for index in indexes
    )
    if any(not text for text in retained) or normalized_text != "\n".join(retained):
        raise ValueError("normalized text must exactly join retained raw blocks")
    removed_indexes = tuple(item.raw_block_index for item in removal_evidence)
    if removed_indexes != tuple(sorted(set(removed_indexes))):
        raise ValueError("raw block removal evidence must be unique and sorted")
    if set(indexes).intersection(removed_indexes) or set(indexes).union(
        removed_indexes
    ) != set(range(len(page.raw_blocks))):
        raise ValueError(
            "retained indexes and removal evidence must form a total partition"
        )
    for evidence in removal_evidence:
        block = page.raw_blocks[evidence.raw_block_index]
        block_text = normalize_text(_block_text(block))
        if evidence.reason_code == "blank-block":
            if block_text:
                raise ValueError(
                    "blank block removal evidence does not match raw block"
                )
            continue
        signature = _candidate_signature(page, block)
        if signature is None or evidence.signature_sha256 != _signature_digest(
            signature
        ):
            raise ValueError("repeated block removal evidence does not match raw block")
        expected_kind: Literal["right-margin", "header-footer"] = (
            "right-margin" if signature.startswith("right-margin:") else "header-footer"
        )
        if evidence.candidate_kind != expected_kind:
            raise ValueError("repeated block removal kind does not match raw block")
        if (
            evidence.body_page_count is None
            or evidence.threshold_count
            != _repetition_threshold(signature, evidence.body_page_count)
        ):
            raise ValueError("repeated block removal threshold is not reproducible")


def _signature_digest(signature: str) -> str:
    return hashlib.sha256(
        _REMOVAL_SIGNATURE_HASH_PREFIX + signature.encode("utf-8")
    ).hexdigest()


def _candidate_signature(page: RawPage, block: RawBlock) -> str | None:
    text = normalize_text(_block_text(block))
    if not text:
        return None
    if page.edition_year == 2020:
        # The approved 2020 vertical navigation is present only on odd pages.
        if page.pdf_page_index % 2 == 1 and block.bbox.x0 / page.page_width >= 0.90:
            return f"right-margin:{text}"
        return None
    if page.edition_year in (2021, 2022):
        top_limit = page.page_height * 0.08
        bottom_limit = page.page_height * 0.92
        if block.bbox.y1 <= top_limit:
            return f"header-footer:top:{text}"
        if block.bbox.y0 >= bottom_limit:
            return f"header-footer:bottom:{text}"
    return None


def discover_repeated_signatures(
    pages: tuple[RawPage, ...], *, body_page_count: int
) -> tuple[frozenset[str], dict[str, int]]:
    """Find templates using the manifest body-page denominator and successful occurrences."""
    per_page_signatures: list[set[str]] = []
    for page in pages:
        if page.page_label is None:
            continue
        per_page_signatures.append(
            {
                signature
                for block in page.raw_blocks
                if (signature := _candidate_signature(page, block))
            }
        )
    counts = Counter(
        signature for signatures in per_page_signatures for signature in signatures
    )
    repeated = frozenset(
        signature
        for signature, count in counts.items()
        if count >= _repetition_threshold(signature, body_page_count)
    )
    return repeated, dict(counts)


def _repetition_threshold(signature: str, body_page_count: int) -> int:
    if body_page_count <= 0:
        return body_page_count + 1
    fraction = (
        RIGHT_MARGIN_REPETITION_FRACTION
        if signature.startswith("right-margin:")
        else HEADER_FOOTER_REPETITION_FRACTION
    )
    # Integer arithmetic retains an inclusive boundary (e.g. 2/5 is 40%).
    return max(1, -(-int(fraction * 100) * body_page_count // 100))


def remove_repeated_margin_blocks(
    page: RawPage,
    *,
    repeated_signatures: frozenset[str],
    body_page_count: int | None = None,
    signature_counts: dict[str, int] | None = None,
) -> CleanedPage:
    """Return a derived text projection after two-factor template filtering."""
    retained: list[str] = []
    retained_indexes: list[int] = []
    removal_evidence: list[RemovedRawBlockEvidence] = []
    for block_index, block in enumerate(page.raw_blocks):
        signature = _candidate_signature(page, block)
        text = normalize_text(_block_text(block))
        observed_count = (
            signature_counts.get(signature, 0)
            if signature is not None and signature_counts is not None
            else 0
        )
        threshold = (
            _repetition_threshold(signature, body_page_count)
            if signature is not None and body_page_count is not None
            else None
        )
        is_repeated = (
            signature is not None
            and signature in repeated_signatures
            and body_page_count is not None
            and signature_counts is not None
            and threshold is not None
            and observed_count >= threshold
        )
        if (
            is_repeated
            and signature is not None
            and body_page_count is not None
            and threshold is not None
        ):
            removal_evidence.append(
                RemovedRawBlockEvidence(
                    raw_block_index=block_index,
                    reason_code="repeated-template",
                    candidate_kind=(
                        "right-margin"
                        if signature.startswith("right-margin:")
                        else "header-footer"
                    ),
                    signature_sha256=_signature_digest(signature),
                    observed_page_count=observed_count,
                    body_page_count=body_page_count,
                    threshold_count=threshold,
                    algorithm_version=REMOVAL_ALGORITHM_VERSION,
                )
            )
        elif text:
            retained.append(text)
            retained_indexes.append(block_index)
        else:
            removal_evidence.append(
                RemovedRawBlockEvidence(
                    raw_block_index=block_index,
                    reason_code="blank-block",
                    candidate_kind=None,
                    signature_sha256=None,
                    observed_page_count=None,
                    body_page_count=None,
                    threshold_count=None,
                    algorithm_version=REMOVAL_ALGORITHM_VERSION,
                )
            )
    return CleanedPage(
        raw_page=page,
        normalized_text="\n".join(retained),
        retained_raw_block_indexes=tuple(retained_indexes),
        removed_raw_block_evidence=tuple(removal_evidence),
    )


def _render_sha256(page: Any) -> str:
    """Hash RGB8 raster samples plus explicit dimensions/colorspace/versioned format.

    The hash input is our own fixed v1 byte layout, not an encoded image: a
    1x identity-matrix RGB pixmap without alpha, followed by width, height,
    component count, and raw samples.  This avoids PNG metadata/compression
    and any filename or output-path influence. Renders are deliberately not
    persisted.
    """
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(1, 1),  # type: ignore[no-untyped-call]
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    header = _RENDER_HASH_PREFIX + f"{pixmap.width}:{pixmap.height}:{pixmap.n}:".encode(
        "ascii"
    )
    return hashlib.sha256(header + bytes(pixmap.samples)).hexdigest()


def _bbox(values: Any) -> BoundingBox:
    """Convert an untyped PyMuPDF rectangle only after enforcing its arity."""
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError("native bbox must have exactly four coordinates")
    return BoundingBox(
        x0=float(values[0]),
        y0=float(values[1]),
        x1=float(values[2]),
        y1=float(values[3]),
    )


def _raw_block(payload: dict[str, Any]) -> RawBlock:
    block_bbox = _bbox(payload["bbox"])
    lines: list[RawLine] = []
    for line_payload in payload.get("lines", []):
        line_bbox = _bbox(line_payload["bbox"])
        spans = tuple(
            RawSpan(
                text=str(span_payload["text"]),
                bbox=_bbox(span_payload["bbox"]),
                font=str(span_payload["font"]),
                size=float(span_payload["size"]),
                confidence=1.0,
            )
            for span_payload in line_payload.get("spans", [])
        )
        lines.append(RawLine(bbox=line_bbox, spans=spans))
    return RawBlock(bbox=block_bbox, lines=tuple(lines))


def _extract_raw_page(
    page: Any, *, document: SourceDocument, pdf_page_index: int
) -> RawPage:
    """Extract one page exclusively via PyMuPDF's sorted dict API."""
    text_dict: dict[str, Any] = page.get_text("dict", sort=True)
    blocks = tuple(
        _raw_block(block)
        for block in text_dict.get("blocks", [])
        if int(block.get("type", _TEXT_BLOCK_TYPE)) == _TEXT_BLOCK_TYPE
        and "lines" in block
    )
    rect = page.rect
    return RawPage(
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        pdf_page_index=pdf_page_index,
        page_label=printed_page_label(
            document.edition_year, pdf_page_index, policy=document.page_numbering
        ),
        page_width=float(rect.width),
        page_height=float(rect.height),
        render_sha256=_render_sha256(page),
        raw_blocks=blocks,
    )


def extract_document(
    source_path: Path, document: SourceDocument
) -> tuple[NativePageRecord, ...]:
    """Extract each page independently, retaining typed quarantine records on failure."""
    approved_document = revalidate_source_document(document)
    if approved_document is None:
        raise NativeExtractionError("approved document contract is invalid") from None
    document = approved_document
    if document.extraction_method != "native":
        raise NativeExtractionError("document is not approved for native extraction")
    try:
        pdf: Any = pymupdf.open(source_path)  # type: ignore[no-untyped-call]
    except DOCUMENT_IO_ERRORS:
        raise NativeExtractionError("cannot open approved source PDF") from None
    provisional_records: list[RawPage | QuarantinedPageRecord] = []
    raw_pages: list[RawPage] = []
    try:
        for pdf_page_index in range(1, document.pdf_page_count + 1):
            label = printed_page_label(
                document.edition_year, pdf_page_index, policy=document.page_numbering
            )
            try:
                raw_page = _extract_raw_page(
                    pdf[pdf_page_index - 1],
                    document=document,
                    pdf_page_index=pdf_page_index,
                )
            except PAGE_EXTRACTION_ERRORS:
                provisional_records.append(
                    QuarantinedPageRecord(
                        schema_version=PAGE_RECORD_SCHEMA_VERSION,
                        doc_id=document.doc_id,
                        edition_year=document.edition_year,
                        source_sha256=document.sha256,
                        document_pdf_page_count=document.pdf_page_count,
                        pdf_page_index=pdf_page_index,
                        page_label=label,
                        reason_code="page-extraction-failed",
                    )
                )
            else:
                raw_pages.append(raw_page)
                provisional_records.append(raw_page)
    finally:
        try:
            pdf.close()
        except DOCUMENT_IO_ERRORS:
            raise NativeExtractionError("cannot close approved source PDF") from None

    body_count = (
        document.page_numbering.body_end_pdf_page
        - document.page_numbering.body_start_pdf_page
        + 1
    )
    repeated, counts = discover_repeated_signatures(
        tuple(raw_pages), body_page_count=body_count
    )
    cleaned_by_page = {
        page.pdf_page_index: remove_repeated_margin_blocks(
            page,
            repeated_signatures=repeated,
            body_page_count=body_count,
            signature_counts=counts,
        )
        for page in raw_pages
    }
    return tuple(
        ExtractedPageRecord(
            schema_version=PAGE_RECORD_SCHEMA_VERSION,
            doc_id=record.doc_id,
            edition_year=record.edition_year,
            source_sha256=document.sha256,
            document_pdf_page_count=document.pdf_page_count,
            pdf_page_index=record.pdf_page_index,
            page_label=record.page_label,
            raw_page=record,
            normalized_text=cleaned_by_page[record.pdf_page_index].normalized_text,
            retained_raw_block_indexes=cleaned_by_page[
                record.pdf_page_index
            ].retained_raw_block_indexes,
            removed_raw_block_evidence=cleaned_by_page[
                record.pdf_page_index
            ].removed_raw_block_evidence,
        )
        if isinstance(record, RawPage)
        else record
        for record in provisional_records
    )


def write_document_jsonl(
    output_path: Path,
    records: tuple[NativePageRecord, ...],
    *,
    document: SourceDocument,
) -> None:
    """Atomically write one stable, complete document JSONL without stale lines."""
    approved_document = revalidate_source_document(document)
    if approved_document is None or approved_document.extraction_method != "native":
        raise NativeExtractionError("approved native document contract is invalid")
    records = tuple(validate_native_page_record(record) for record in records)
    ordered = sorted(records, key=lambda record: record.pdf_page_index)
    if any(
        record.doc_id != approved_document.doc_id
        or record.edition_year != approved_document.edition_year
        or record.source_sha256 != approved_document.sha256
        or record.document_pdf_page_count != approved_document.pdf_page_count
        or record.page_label
        != printed_page_label(
            approved_document.edition_year,
            record.pdf_page_index,
            policy=approved_document.page_numbering,
        )
        for record in ordered
    ):
        raise NativeExtractionError("page records do not match approved document")
    if len(ordered) != approved_document.pdf_page_count or any(
        record.pdf_page_index != expected_index
        for expected_index, record in enumerate(ordered, start=1)
    ):
        raise NativeExtractionError("page records must be complete and contiguous")
    occurrence_counts: Counter[str] = Counter()
    for record in ordered:
        if not isinstance(record, ExtractedPageRecord):
            continue
        page_digests = {
            _signature_digest(signature)
            for block in record.raw_page.raw_blocks
            if record.raw_page.page_label is not None
            and (signature := _candidate_signature(record.raw_page, block)) is not None
        }
        occurrence_counts.update(page_digests)
    if any(
        evidence.reason_code == "repeated-template"
        and (
            evidence.signature_sha256 is None
            or evidence.observed_page_count
            != occurrence_counts[evidence.signature_sha256]
            or evidence.body_page_count is None
            or evidence.body_page_count > len(ordered)
            or evidence.body_page_count
            != (
                APPROVED_PAGE_POLICIES[record.edition_year].body_end_pdf_page
                - APPROVED_PAGE_POLICIES[record.edition_year].body_start_pdf_page
                + 1
                if record.edition_year in APPROVED_PAGE_POLICIES
                else None
            )
        )
        for record in ordered
        if isinstance(record, ExtractedPageRecord)
        for evidence in record.removed_raw_block_evidence
    ):
        raise NativeExtractionError("native removal evidence is inconsistent")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in ordered
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output_path)
    except OSError:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise NativeExtractionError("cannot write extraction output") from None
