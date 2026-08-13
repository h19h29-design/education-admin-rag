"""2024-2025 OCR card-layout parser entry points."""

from collections.abc import Sequence
from typing import Literal

from src.ingestion.extract_ocr import OcrPageRecord
from src.ingestion.parse_common import (
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    parse_pages,
    parser_page_from_ocr_record,
)


def parse_2024_2025(
    pages: Sequence[ParserPage], *, edition_year: Literal[2024, 2025]
) -> ParseResult:
    """Parse card regions without inferring unavailable raster evidence."""
    return parse_pages(pages, edition_year=edition_year)


def parse_document(
    records: Sequence[OcrPageRecord],
    *,
    edition_year: Literal[2024, 2025],
    page_role_policy: VerifiedPageRolePolicy,
) -> ParseResult:
    """Adapt OCR records using only their validated registry-bound provenance."""
    pages = tuple(
        parser_page_from_ocr_record(
            record,
            page_role_policy=page_role_policy,
        )
        for record in records
    )
    return parse_2024_2025(pages, edition_year=edition_year)
