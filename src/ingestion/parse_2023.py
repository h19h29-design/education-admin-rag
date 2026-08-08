"""2023 OCR-layout parser entry points."""

from collections.abc import Sequence

from src.ingestion.extract_ocr import OcrPageRecord
from src.ingestion.parse_common import (
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    parse_pages,
    parser_page_from_ocr_record,
)


def parse_2023(pages: Sequence[ParserPage]) -> ParseResult:
    """Parse provenance-adapted 2023 OCR pages."""
    return parse_pages(pages, edition_year=2023)


def parse_document(
    records: Sequence[OcrPageRecord],
    *,
    page_role_policy: VerifiedPageRolePolicy,
) -> ParseResult:
    """Adapt reviewed OCR records and keep every critical field unverified."""
    pages = tuple(
        parser_page_from_ocr_record(record, page_role_policy=page_role_policy)
        for record in records
    )
    return parse_2023(pages)
