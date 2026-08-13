"""Immutable, provenance-preserving models shared by page extractors."""

from __future__ import annotations

import math
import re
from typing import Final, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingestion.manifest import (
    PageNumberingPolicy,
    PageSizeProfile,
    SourceDocument,
    page_label,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PAGE_RECORD_SCHEMA_VERSION: Final = 2
APPROVED_LAYOUT_DETECTOR_VERSION: Final = "green-card-border-v1"

SemanticHint: TypeAlias = Literal[
    "title",
    "question",
    "amount",
    "date",
    "law_name",
    "article",
    "document_number",
    "ocr_line",
]
LayoutEvidenceStatus: TypeAlias = Literal[
    "not_applicable", "unavailable", "failed", "not_detected", "detected"
]


def exact_declared_field_mapping(
    value: object,
    expected_model: type[BaseModel],
) -> dict[str, object] | None:
    """Copy only an exact model's declared fields without invoking serialization."""
    if type(value) is not expected_model:
        return None
    raw_fields = object.__getattribute__(value, "__dict__")
    if type(raw_fields) is not dict or set(raw_fields) != set(
        expected_model.model_fields
    ):
        return None
    return {
        field_name: raw_fields[field_name] for field_name in expected_model.model_fields
    }


def _exact_tuple(value: object) -> tuple[object, ...] | None:
    if type(value) is not tuple:
        return None
    return value


def _revalidate_page_size_profile(value: object) -> PageSizeProfile | None:
    fields = exact_declared_field_mapping(value, PageSizeProfile)
    if fields is None:
        return None
    try:
        return PageSizeProfile.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_page_numbering_policy(
    value: object,
) -> PageNumberingPolicy | None:
    fields = exact_declared_field_mapping(value, PageNumberingPolicy)
    if fields is None:
        return None
    try:
        return PageNumberingPolicy.model_validate(fields)
    except (TypeError, ValueError):
        return None


def revalidate_source_document(value: object) -> SourceDocument | None:
    """Recursively reconstruct an exact manifest document without serializing input."""
    fields = exact_declared_field_mapping(value, SourceDocument)
    if fields is None:
        return None
    raw_profiles = _exact_tuple(fields["page_size_profiles"])
    page_numbering = _revalidate_page_numbering_policy(fields["page_numbering"])
    if raw_profiles is None or page_numbering is None:
        return None
    profiles = tuple(_revalidate_page_size_profile(profile) for profile in raw_profiles)
    if any(profile is None for profile in profiles):
        return None
    fields["page_size_profiles"] = tuple(
        profile for profile in profiles if profile is not None
    )
    fields["page_numbering"] = page_numbering
    try:
        return SourceDocument.model_validate(fields)
    except (TypeError, ValueError):
        return None


# These checked-in values make ``printed_page_label`` usable without opening a
# manifest.  Extraction itself always passes the manifest policy; the drift
# test makes any future manifest change an explicit review decision.
APPROVED_PAGE_POLICIES: dict[int, PageNumberingPolicy] = {
    2020: PageNumberingPolicy(
        mode="offset", body_start_pdf_page=7, body_end_pdf_page=299, offset=-6
    ),
    2021: PageNumberingPolicy(
        mode="offset", body_start_pdf_page=7, body_end_pdf_page=382, offset=0
    ),
    2022: PageNumberingPolicy(
        mode="offset", body_start_pdf_page=7, body_end_pdf_page=384, offset=0
    ),
}


class BoundingBox(BaseModel):
    """A finite, ordered PDF-coordinate rectangle in page points."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def is_finite_and_ordered(self) -> Self:
        if not all(
            math.isfinite(value) for value in (self.x0, self.y0, self.x1, self.y1)
        ):
            raise ValueError("bbox coordinates must be finite")
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("bbox coordinates must be ordered")
        return self

    @classmethod
    def from_tuple(cls, values: tuple[float, float, float, float]) -> BoundingBox:
        """Build a checked box from PyMuPDF's four-coordinate representation."""
        return cls(x0=values[0], y0=values[1], x1=values[2], y1=values[3])


class LayoutRegion(BaseModel):
    """One raster-observed layout region in normalized PDF-point coordinates."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    region_type: Literal["card"]
    bbox: BoundingBox
    evidence: Literal["raster-border"]

    @model_validator(mode="after")
    def has_positive_area(self) -> Self:
        if self.bbox.x0 >= self.bbox.x1 or self.bbox.y0 >= self.bbox.y1:
            raise ValueError("layout region bbox must have positive area")
        return self


class LayoutEvidence(BaseModel):
    """Explicit fail-closed result of optional raster layout detection."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    status: LayoutEvidenceStatus = "not_applicable"
    detector_version: str | None = Field(
        default=None, pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
    )
    regions: tuple[LayoutRegion, ...] = ()

    @model_validator(mode="after")
    def matches_status(self) -> Self:
        detector_ran = self.status in {"failed", "not_detected", "detected"}
        if detector_ran != (self.detector_version is not None):
            raise ValueError("layout evidence detector version does not match status")
        if self.status == "detected" and not self.regions:
            raise ValueError("detected layout evidence requires regions")
        if self.status != "detected" and self.regions:
            raise ValueError("layout evidence regions require detected status")
        return self


class RawSpan(BaseModel):
    """Unchanged text span with source-native or measured OCR confidence."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    text: str
    bbox: BoundingBox
    font: str
    size: float
    confidence: float = Field(default=1.0)
    semantic_hint: SemanticHint | None = None

    @model_validator(mode="after")
    def has_finite_confidence_and_size(self) -> Self:
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("span confidence must be finite and between 0 and 1")
        if not math.isfinite(self.size):
            raise ValueError("span size must be finite")
        return self


class RawLine(BaseModel):
    """Unchanged text line supplied by the selected extractor."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    bbox: BoundingBox
    spans: tuple[RawSpan, ...]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def has_finite_confidence(self) -> Self:
        if not math.isfinite(self.confidence):
            raise ValueError("line confidence must be finite")
        return self


class RawBlock(BaseModel):
    """Unchanged native text block supplied by PyMuPDF."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    bbox: BoundingBox
    lines: tuple[RawLine, ...]


class RawPage(BaseModel):
    """Immutable, pre-cleanup page provenance and raw text hierarchy."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    extraction_source: Literal["native", "ocr"] = "native"
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)
    render_sha256: str
    raw_blocks: tuple[RawBlock, ...]
    layout_evidence: LayoutEvidence = Field(default_factory=LayoutEvidence)

    @model_validator(mode="after")
    def has_safe_page_provenance(self) -> Self:
        if not math.isfinite(self.page_width) or not math.isfinite(self.page_height):
            raise ValueError("page geometry must be finite")
        if self.page_label is not None and (
            not self.page_label.isdecimal() or int(self.page_label) < 1
        ):
            raise ValueError("page label must be a positive decimal string")
        if not _SHA256_RE.fullmatch(self.render_sha256):
            raise ValueError("render SHA-256 must be lowercase hexadecimal")
        if self.extraction_source == "native" and any(
            line.confidence != 1.0 or any(span.confidence != 1.0 for span in line.spans)
            for block in self.raw_blocks
            for line in block.lines
        ):
            raise ValueError("native page confidence must be exactly 1.0")
        if self.extraction_source == "native" and any(
            span.semantic_hint is not None
            for block in self.raw_blocks
            for line in block.lines
            for span in line.spans
        ):
            raise ValueError("native page cannot contain OCR semantic hints")
        if (
            self.extraction_source == "native"
            and self.layout_evidence.status != "not_applicable"
        ):
            raise ValueError("native page layout evidence must be not applicable")
        region_keys = tuple(
            (
                region.bbox.y0,
                region.bbox.x0,
                region.bbox.y1,
                region.bbox.x1,
                region.region_type,
            )
            for region in self.layout_evidence.regions
        )
        if region_keys != tuple(sorted(region_keys)) or len(region_keys) != len(
            set(region_keys)
        ):
            raise ValueError(
                "layout regions must be unique and deterministically sorted"
            )
        if any(
            region.bbox.x0 < 0.0
            or region.bbox.y0 < 0.0
            or region.bbox.x1 > self.page_width
            or region.bbox.y1 > self.page_height
            for region in self.layout_evidence.regions
        ):
            raise ValueError("layout region must remain inside page geometry")
        return self


def _revalidate_bounding_box(value: object) -> BoundingBox | None:
    fields = exact_declared_field_mapping(value, BoundingBox)
    if fields is None:
        return None
    try:
        return BoundingBox.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_layout_region(value: object) -> LayoutRegion | None:
    fields = exact_declared_field_mapping(value, LayoutRegion)
    if fields is None:
        return None
    bbox = _revalidate_bounding_box(fields["bbox"])
    if bbox is None:
        return None
    fields["bbox"] = bbox
    try:
        return LayoutRegion.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_layout_evidence(value: object) -> LayoutEvidence | None:
    fields = exact_declared_field_mapping(value, LayoutEvidence)
    if fields is None:
        return None
    raw_regions = _exact_tuple(fields["regions"])
    if raw_regions is None:
        return None
    regions = tuple(_revalidate_layout_region(region) for region in raw_regions)
    if any(region is None for region in regions):
        return None
    fields["regions"] = tuple(region for region in regions if region is not None)
    try:
        return LayoutEvidence.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_raw_span(value: object) -> RawSpan | None:
    fields = exact_declared_field_mapping(value, RawSpan)
    if fields is None:
        return None
    bbox = _revalidate_bounding_box(fields["bbox"])
    if bbox is None:
        return None
    fields["bbox"] = bbox
    try:
        return RawSpan.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_raw_line(value: object) -> RawLine | None:
    fields = exact_declared_field_mapping(value, RawLine)
    if fields is None:
        return None
    bbox = _revalidate_bounding_box(fields["bbox"])
    raw_spans = _exact_tuple(fields["spans"])
    if bbox is None or raw_spans is None:
        return None
    spans = tuple(_revalidate_raw_span(span) for span in raw_spans)
    if any(span is None for span in spans):
        return None
    fields["bbox"] = bbox
    fields["spans"] = tuple(span for span in spans if span is not None)
    try:
        return RawLine.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_raw_block(value: object) -> RawBlock | None:
    fields = exact_declared_field_mapping(value, RawBlock)
    if fields is None:
        return None
    bbox = _revalidate_bounding_box(fields["bbox"])
    raw_lines = _exact_tuple(fields["lines"])
    if bbox is None or raw_lines is None:
        return None
    lines = tuple(_revalidate_raw_line(line) for line in raw_lines)
    if any(line is None for line in lines):
        return None
    fields["bbox"] = bbox
    fields["lines"] = tuple(line for line in lines if line is not None)
    try:
        return RawBlock.model_validate(fields)
    except (TypeError, ValueError):
        return None


def revalidate_raw_page(value: object) -> RawPage | None:
    """Recursively reconstruct an exact raw-page tree without serializing input."""
    fields = exact_declared_field_mapping(value, RawPage)
    if fields is None:
        return None
    raw_blocks = _exact_tuple(fields["raw_blocks"])
    layout_evidence = _revalidate_layout_evidence(fields["layout_evidence"])
    if raw_blocks is None or layout_evidence is None:
        return None
    blocks = tuple(_revalidate_raw_block(block) for block in raw_blocks)
    if any(block is None for block in blocks):
        return None
    fields["raw_blocks"] = tuple(block for block in blocks if block is not None)
    fields["layout_evidence"] = layout_evidence
    try:
        return RawPage.model_validate(fields)
    except (TypeError, ValueError):
        return None


def printed_page_label(
    edition_year: int, pdf_page_index: int, *, policy: PageNumberingPolicy | None = None
) -> str | None:
    """Return the safe printed label, using a passed manifest policy when available."""
    selected_policy = policy or APPROVED_PAGE_POLICIES.get(edition_year)
    if selected_policy is None:
        return None
    label = page_label(selected_policy, pdf_page_index)
    return str(label) if label is not None else None
