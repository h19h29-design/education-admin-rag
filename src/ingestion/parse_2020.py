"""2020 native-layout parser entry points."""

from collections.abc import Sequence

from src.ingestion.extract_native import NativePageRecord
from src.ingestion.parse_common import (
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    parse_pages,
    parser_page_from_native_record,
)


def parse_2020(pages: Sequence[ParserPage]) -> ParseResult:
    """Parse provenance-adapted 2020 pages."""
    return parse_pages(pages, edition_year=2020)


def parse_document(
    records: Sequence[NativePageRecord],
    *,
    page_role_policy: VerifiedPageRolePolicy,
) -> ParseResult:
    """Adapt reviewed native records and parse the 2020 edition."""
    pages = tuple(
        parser_page_from_native_record(record, page_role_policy=page_role_policy)
        for record in records
    )
    return parse_2020(pages)
