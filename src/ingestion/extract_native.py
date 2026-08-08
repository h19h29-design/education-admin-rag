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
from typing import Any, Literal, TypeAlias

import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from src.ingestion.extract_common import (
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
    printed_page_label,
)
from src.ingestion.manifest import SourceDocument

RIGHT_MARGIN_REPETITION_FRACTION = 0.40
"""2020 navigation requires occurrence on at least 40% of body pages."""

HEADER_FOOTER_REPETITION_FRACTION = 0.50
"""Conservative 2021-22 template threshold: a majority of body pages."""

_WHITESPACE_RE = re.compile(r"\s+")
_TEXT_BLOCK_TYPE = 0
_RENDER_HASH_PREFIX = b"sen-qa-native-render-v1\0rgb8\0"


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
PAGE_EXTRACTION_ERRORS = PYMUPDF_ERRORS + (OSError, IndexError, KeyError, TypeError, ValueError)


class NativeExtractionError(Exception):
    """Raised for safe, document-level native extraction failures."""


class CleanedPage(BaseModel):
    """A derived normalized projection while retaining the original raw page exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_page: RawPage
    normalized_text: str

    @property
    def raw_blocks(self) -> tuple[RawBlock, ...]:
        """Compatibility/readability projection; never a mutable cleaned copy."""
        return self.raw_page.raw_blocks


class ExtractedPageRecord(BaseModel):
    """A successfully extracted page, including raw provenance and clean projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["extracted"] = "extracted"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    raw_page: RawPage
    normalized_text: str


class QuarantinedPageRecord(BaseModel):
    """A page that could not be extracted, without fabricated empty text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["quarantined"] = "quarantined"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    reason_code: Literal["page-extraction-failed"]


NativePageRecord: TypeAlias = ExtractedPageRecord | QuarantinedPageRecord


def normalize_text(text: str) -> str:
    """Normalize only the derived view; raw spans are never altered."""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def _block_text(block: RawBlock) -> str:
    return "\n".join("".join(span.text for span in line.spans) for line in block.lines)


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
            {signature for block in page.raw_blocks if (signature := _candidate_signature(page, block))}
        )
    counts = Counter(signature for signatures in per_page_signatures for signature in signatures)
    repeated = frozenset(
        signature
        for signature, count in counts.items()
        if count >= _repetition_threshold(signature, body_page_count)
    )
    return repeated, dict(counts)


def _repetition_threshold(signature: str, body_page_count: int) -> int:
    if body_page_count <= 0:
        return body_page_count + 1
    fraction = RIGHT_MARGIN_REPETITION_FRACTION if signature.startswith("right-margin:") else HEADER_FOOTER_REPETITION_FRACTION
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
    for block in page.raw_blocks:
        signature = _candidate_signature(page, block)
        is_repeated = signature in repeated_signatures if signature is not None else False
        if is_repeated and body_page_count is not None and signature_counts is not None and signature is not None:
            is_repeated = signature_counts.get(signature, 0) >= _repetition_threshold(signature, body_page_count)
        if not is_repeated:
            text = normalize_text(_block_text(block))
            if text:
                retained.append(text)
    return CleanedPage(raw_page=page, normalized_text="\n".join(retained))


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
    header = _RENDER_HASH_PREFIX + f"{pixmap.width}:{pixmap.height}:{pixmap.n}:".encode("ascii")
    return hashlib.sha256(header + bytes(pixmap.samples)).hexdigest()


def _bbox(values: Any) -> BoundingBox:
    """Convert an untyped PyMuPDF rectangle only after enforcing its arity."""
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError("native bbox must have exactly four coordinates")
    return BoundingBox(x0=float(values[0]), y0=float(values[1]), x1=float(values[2]), y1=float(values[3]))


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
        if int(block.get("type", _TEXT_BLOCK_TYPE)) == _TEXT_BLOCK_TYPE and "lines" in block
    )
    rect = page.rect
    return RawPage(
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        pdf_page_index=pdf_page_index,
        page_label=printed_page_label(document.edition_year, pdf_page_index, policy=document.page_numbering),
        page_width=float(rect.width),
        page_height=float(rect.height),
        render_sha256=_render_sha256(page),
        raw_blocks=blocks,
    )


def extract_document(source_path: Path, document: SourceDocument) -> tuple[NativePageRecord, ...]:
    """Extract each page independently, retaining typed quarantine records on failure."""
    if document.extraction_method != "native":
        raise NativeExtractionError("document is not approved for native extraction")
    try:
        pdf: Any = pymupdf.open(source_path)  # type: ignore[no-untyped-call]
    except DOCUMENT_IO_ERRORS as error:
        raise NativeExtractionError("cannot open approved source PDF") from error
    records: list[NativePageRecord] = []
    raw_pages: list[RawPage] = []
    try:
        for pdf_page_index in range(1, document.pdf_page_count + 1):
            label = printed_page_label(document.edition_year, pdf_page_index, policy=document.page_numbering)
            try:
                raw_page = _extract_raw_page(pdf[pdf_page_index - 1], document=document, pdf_page_index=pdf_page_index)
            except PAGE_EXTRACTION_ERRORS:
                records.append(
                    QuarantinedPageRecord(
                        doc_id=document.doc_id,
                        edition_year=document.edition_year,
                        pdf_page_index=pdf_page_index,
                        page_label=label,
                        reason_code="page-extraction-failed",
                    )
                )
            else:
                raw_pages.append(raw_page)
                records.append(
                    ExtractedPageRecord(
                        doc_id=document.doc_id,
                        edition_year=document.edition_year,
                        pdf_page_index=pdf_page_index,
                        page_label=raw_page.page_label,
                        raw_page=raw_page,
                        normalized_text="",
                    )
                )
    finally:
        try:
            pdf.close()
        except DOCUMENT_IO_ERRORS as error:
            raise NativeExtractionError("cannot close approved source PDF") from error

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
            page, repeated_signatures=repeated, body_page_count=body_count, signature_counts=counts
        )
        for page in raw_pages
    }
    return tuple(
        ExtractedPageRecord(
            doc_id=record.doc_id,
            edition_year=record.edition_year,
            pdf_page_index=record.pdf_page_index,
            page_label=record.page_label,
            raw_page=record.raw_page,
            normalized_text=cleaned_by_page[record.pdf_page_index].normalized_text,
        )
        if isinstance(record, ExtractedPageRecord)
        else record
        for record in records
    )


def write_document_jsonl(output_path: Path, records: tuple[NativePageRecord, ...]) -> None:
    """Atomically write one stable, complete document JSONL without stale lines."""
    expected_pages = list(range(1, len(records) + 1))
    ordered = sorted(records, key=lambda record: record.pdf_page_index)
    if [record.pdf_page_index for record in ordered] != expected_pages:
        raise NativeExtractionError("page records must be complete and contiguous")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in ordered
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output_path)
    except OSError as error:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise NativeExtractionError("cannot write extraction output") from error
