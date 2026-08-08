"""Immutable, provenance-preserving models shared by page extractors."""

from __future__ import annotations

import math
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingestion.manifest import PageNumberingPolicy, page_label

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# These checked-in values make ``printed_page_label`` usable without opening a
# manifest.  Extraction itself always passes the manifest policy; the drift
# test makes any future manifest change an explicit review decision.
APPROVED_PAGE_POLICIES: dict[int, PageNumberingPolicy] = {
    2020: PageNumberingPolicy(mode="offset", body_start_pdf_page=7, body_end_pdf_page=299, offset=-6),
    2021: PageNumberingPolicy(mode="offset", body_start_pdf_page=7, body_end_pdf_page=382, offset=0),
    2022: PageNumberingPolicy(mode="offset", body_start_pdf_page=7, body_end_pdf_page=384, offset=0),
}


class BoundingBox(BaseModel):
    """A finite, ordered PDF-coordinate rectangle in page points."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def is_finite_and_ordered(self) -> Self:
        if not all(math.isfinite(value) for value in (self.x0, self.y0, self.x1, self.y1)):
            raise ValueError("bbox coordinates must be finite")
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("bbox coordinates must be ordered")
        return self

    @classmethod
    def from_tuple(cls, values: tuple[float, float, float, float]) -> BoundingBox:
        """Build a checked box from PyMuPDF's four-coordinate representation."""
        return cls(x0=values[0], y0=values[1], x1=values[2], y1=values[3])


class RawSpan(BaseModel):
    """Unchanged native text span supplied by PyMuPDF."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str
    bbox: BoundingBox
    font: str
    size: float
    confidence: float = Field(default=1.0)

    @model_validator(mode="after")
    def is_native_and_finite(self) -> Self:
        if self.confidence != 1.0:
            raise ValueError("native span confidence must be exactly 1.0")
        if not math.isfinite(self.size):
            raise ValueError("span size must be finite")
        return self


class RawLine(BaseModel):
    """Unchanged native text line supplied by PyMuPDF."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bbox: BoundingBox
    spans: tuple[RawSpan, ...]


class RawBlock(BaseModel):
    """Unchanged native text block supplied by PyMuPDF."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bbox: BoundingBox
    lines: tuple[RawLine, ...]


class RawPage(BaseModel):
    """Immutable, pre-cleanup page provenance and its native text hierarchy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)
    render_sha256: str
    raw_blocks: tuple[RawBlock, ...]

    @model_validator(mode="after")
    def has_safe_page_provenance(self) -> Self:
        if not math.isfinite(self.page_width) or not math.isfinite(self.page_height):
            raise ValueError("page geometry must be finite")
        if self.page_label is not None and (not self.page_label.isdecimal() or int(self.page_label) < 1):
            raise ValueError("page label must be a positive decimal string")
        if not _SHA256_RE.fullmatch(self.render_sha256):
            raise ValueError("render SHA-256 must be lowercase hexadecimal")
        return self


def printed_page_label(
    edition_year: int, pdf_page_index: int, *, policy: PageNumberingPolicy | None = None
) -> str | None:
    """Return the safe printed label, using a passed manifest policy when available."""
    selected_policy = policy or APPROVED_PAGE_POLICIES.get(edition_year)
    if selected_policy is None:
        return None
    label = page_label(selected_policy, pdf_page_index)
    return str(label) if label is not None else None
