"""Strict source-document manifest validation and verification."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import date
from pathlib import Path, PurePath
from typing import Any, Final, Literal

import pymupdf
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

PAGE_SIZE_TOLERANCE_PT = 0.01
"""Maximum harmless PDF point-coordinate rounding difference (1/100 point)."""

MAX_SUPPORTED_PDF_PAGE_COUNT: Final = 10_000
"""Corpus safety ceiling; reviewed originals are under 400 pages."""

APPROVED_EDITION_YEARS = (2020, 2021, 2022, 2023, 2024, 2025)
EDITION_SEQUENCE_ERROR = (
    "manifest must contain exactly editions 2020 through 2025 in order"
)
PDF_LIBRARY_ERRORS = (OSError, RuntimeError, ValueError)


class ManifestError(Exception):
    """Raised when a source cannot satisfy its approved manifest contract."""


class PageSizeProfile(BaseModel):
    """A contiguous PDF-page range that shares an expected media-box size."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    start_pdf_page: int = Field(ge=1)
    end_pdf_page: int = Field(ge=1)
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)

    @model_validator(mode="after")
    def end_is_not_before_start(self) -> PageSizeProfile:
        if self.end_pdf_page < self.start_pdf_page:
            raise ValueError("page-size profile end must not precede its start")
        return self


class PageNumberingPolicy(BaseModel):
    """Maps a PDF page to a safe body-page label."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    mode: Literal["offset"]
    body_start_pdf_page: int = Field(ge=1)
    body_end_pdf_page: int = Field(ge=1)
    offset: int

    @model_validator(mode="after")
    def has_nonnegative_body_labels(self) -> PageNumberingPolicy:
        if self.body_end_pdf_page < self.body_start_pdf_page:
            raise ValueError("body end must not precede body start")
        if self.body_start_pdf_page + self.offset < 1:
            raise ValueError("page-number offset would create a negative body label")
        return self


class SourceDocument(BaseModel):
    """The complete approved contract for one original source PDF."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    doc_id: str = Field(min_length=1)
    edition_year: int = Field(ge=1900, le=2100)
    official_title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    registration_no: str | None
    source_period_start: date | None
    source_period_end: date | None
    source_filename: str = Field(min_length=1)
    source_relpath: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int = Field(gt=0, le=MAX_SUPPORTED_PDF_PAGE_COUNT)
    page_size_profiles: tuple[PageSizeProfile, ...] = Field(min_length=1)
    extraction_method: Literal["native", "ocr"]
    source_dpi: int | None = Field(default=None, gt=0)
    render_dpi: int | None = Field(default=None, gt=0)
    page_numbering: PageNumberingPolicy
    official_public_url: AnyHttpUrl | None
    official_url_status: Literal["unverified", "verified", "unavailable"]
    redistribution_status: Literal["unverified", "approved", "denied"]
    access_level: Literal["staff", "public"]

    @model_validator(mode="after")
    def validates_page_ranges_and_dates(self) -> SourceDocument:
        if (
            self.source_period_start
            and self.source_period_end
            and self.source_period_end < self.source_period_start
        ):
            raise ValueError("source period end must not precede source period start")

        expected_start = 1
        for profile in self.page_size_profiles:
            if profile.start_pdf_page != expected_start:
                raise ValueError(
                    "page-size profiles must be contiguous from PDF page 1"
                )
            expected_start = profile.end_pdf_page + 1
        if expected_start - 1 != self.pdf_page_count:
            raise ValueError("page-size profiles must end at the PDF page count")
        if self.page_numbering.body_end_pdf_page > self.pdf_page_count:
            raise ValueError("body page range exceeds the PDF page count")

        relative = PurePath(self.source_relpath)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("source path must remain under the source root")
        if unicodedata.normalize("NFC", relative.name) != unicodedata.normalize(
            "NFC", self.source_filename
        ):
            raise ValueError("source filename must match source_relpath basename")
        return self


class SourceManifest(BaseModel):
    """Strict manifest envelope with unique, chronological editions."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    documents: tuple[SourceDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def has_unique_chronological_documents(self) -> SourceManifest:
        years = [document.edition_year for document in self.documents]
        doc_ids = [document.doc_id for document in self.documents]
        if tuple(years) != APPROVED_EDITION_YEARS:
            raise ValueError(EDITION_SEQUENCE_ERROR)
        if len(set(doc_ids)) != len(doc_ids):
            raise ValueError("document IDs must be unique")
        return self


def load_manifest(manifest_path: Path) -> tuple[SourceDocument, ...]:
    """Load a JSON manifest without allowing unknown or malformed fields."""
    manifest_text: str | None = None
    read_error_message: str | None = None
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except UnicodeError:
        read_error_message = "manifest validation failed"
    except OSError:
        read_error_message = "cannot read manifest file"
    if read_error_message is not None or manifest_text is None:
        raise ManifestError(read_error_message or "cannot read manifest file") from None
    validation_message: str | None = None
    try:
        manifest = SourceManifest.model_validate_json(manifest_text)
    except ValidationError as error:
        validation_message = (
            EDITION_SEQUENCE_ERROR
            if EDITION_SEQUENCE_ERROR in str(error)
            else "manifest validation failed"
        )
    if validation_message is not None:
        raise ManifestError(validation_message) from None
    return manifest.documents


def resolve_source(source_root: Path, expected_doc: SourceDocument) -> Path:
    """Resolve one manifest relative path while preventing root escape."""
    relative = Path(expected_doc.source_relpath)
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError("source path must remain under the source root")

    try:
        root = source_root.resolve()
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError) as error:
        raise ManifestError("cannot resolve source path") from error
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ManifestError("resolved source path escapes the source root") from error
    try:
        is_file = candidate.is_file()
    except (OSError, RuntimeError) as error:
        raise ManifestError("cannot resolve source path") from error
    if not is_file:
        raise ManifestError("source file not found under the source root")
    return candidate


def page_label(policy: PageNumberingPolicy, pdf_page: int) -> int | None:
    """Return a citation label only for actual body pages, never a negative value."""
    if not policy.body_start_pdf_page <= pdf_page <= policy.body_end_pdf_page:
        return None
    label = pdf_page + policy.offset
    return label if label >= 1 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ManifestError("cannot read source file") from error
    return digest.hexdigest()


def _matches_profile(
    page_number: int, rect: pymupdf.Rect, profiles: tuple[PageSizeProfile, ...]
) -> bool:
    for profile in profiles:
        if profile.start_pdf_page <= page_number <= profile.end_pdf_page:
            return (
                abs(float(rect.width) - profile.width_pt) <= PAGE_SIZE_TOLERANCE_PT
                and abs(float(rect.height) - profile.height_pt)
                <= PAGE_SIZE_TOLERANCE_PT
            )
    return False


def verify_source(source_path: Path, expected_doc: SourceDocument) -> None:
    """Verify filename, streamed digest, page count, and page geometry exactly enough."""
    if unicodedata.normalize("NFC", source_path.name) != unicodedata.normalize(
        "NFC", expected_doc.source_filename
    ):
        raise ManifestError("filename mismatch")
    if _sha256(source_path) != expected_doc.sha256:
        raise ManifestError("SHA-256 mismatch")

    try:
        document: Any = pymupdf.open(source_path)  # type: ignore[no-untyped-call]
    except PDF_LIBRARY_ERRORS as error:
        raise ManifestError("cannot open source PDF") from error
    try:
        try:
            page_count = int(document.page_count)
        except PDF_LIBRARY_ERRORS as error:
            raise ManifestError("cannot inspect source PDF") from error
        if page_count != expected_doc.pdf_page_count:
            raise ManifestError("page-count mismatch")
        for page_number in range(1, page_count + 1):
            try:
                page: Any = document[page_number - 1]
                rect = page.rect
            except PDF_LIBRARY_ERRORS as error:
                raise ManifestError("cannot inspect source PDF") from error
            if not _matches_profile(page_number, rect, expected_doc.page_size_profiles):
                raise ManifestError("page-size profile mismatch")
    finally:
        try:
            document.close()
        except PDF_LIBRARY_ERRORS as error:
            raise ManifestError("cannot inspect source PDF") from error
