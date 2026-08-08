"""2021-2022 native-layout parser entry points."""

from collections.abc import Sequence
from typing import Literal

from src.ingestion.extract_native import NativePageRecord
from src.ingestion.parse_common import (
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    parse_pages,
    parser_page_from_native_record,
)


def parse_2021_2022(
    pages: Sequence[ParserPage], *, edition_year: Literal[2021, 2022]
) -> ParseResult:
    """Parse provenance-adapted pages with the selected native layout."""
    return parse_pages(pages, edition_year=edition_year)


def parse_document(
    records: Sequence[NativePageRecord],
    *,
    edition_year: Literal[2021, 2022],
    page_role_policy: VerifiedPageRolePolicy,
) -> ParseResult:
    """Adapt reviewed native records and parse one selected edition."""
    pages = tuple(
        parser_page_from_native_record(record, page_role_policy=page_role_policy)
        for record in records
    )
    return parse_2021_2022(pages, edition_year=edition_year)
