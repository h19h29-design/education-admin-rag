"""Golden layout contracts for the 2020 native parser."""

from src.ingestion.parse_common import parse_pages, parser_page_from_raw_page
from tests.ingestion.test_page_continuation import (
    _raw_page,
    assert_golden_year,
    load_golden,
)


def test_2020_golden_layouts() -> None:
    """Catches case bleed/split or front-matter false positives in all seven fixtures."""
    assert_golden_year(2020)


def test_right_margin_19_part_navigation_is_not_a_case_or_part_boundary() -> None:
    """Catches repeated x≈0.96 vertical navigation closing the body case."""
    result = parse_pages(load_golden(2020, "first-case"), edition_year=2020)
    assert len(result) == 1
    assert result[0].part == "계약 일반"
    assert all(transition.value != "19편" for transition in result.transitions)


def test_2020_related_basis_marker_can_be_attached_to_content() -> None:
    """Catches requiring an exact standalone 관련근거 marker when content follows it."""
    result = parse_pages(load_golden(2020, "first-case"), edition_year=2020)
    assert result[0].basis_text == "합성 첫 근거입니다."


def test_native_whitespace_span_does_not_reject_meaningful_page() -> None:
    """Catches decorative PDF whitespace causing the whole page to be rejected."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-whitespace-span",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 53,
            "page_label": "47",
            "lines": [
                {
                    "text": "   ",
                    "bbox": [70.0, 80.0, 90.0, 95.0],
                    "font": "",
                    "size": 8.0,
                },
                {
                    "text": "제목: 합성 본문",
                    "bbox": [70.0, 120.0, 300.0, 145.0],
                    "font": "",
                    "size": 12.0,
                },
            ],
        },
    )

    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    assert len(raw_page.raw_blocks) == 2
    assert tuple(line.normalized_text for line in page.lines) == ("제목: 합성 본문",)


def test_2020_actual_native_card_uses_size_coordinates_without_font_names() -> None:
    """Catches synthetic font names or explicit role labels masking native layout."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-actual-card",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "대분류: 합성 계약",
                    "bbox": [70.0, 25.0, 300.0, 50.0],
                    "font": "",
                    "size": 12.0,
                },
                {
                    "text": "1편 합성 운영",
                    "bbox": [350.0, 65.0, 545.0, 95.0],
                    "font": "",
                    "size": 18.0,
                },
                {
                    "text": "합성 카드 질문 제목",
                    "bbox": [117.0, 160.0, 525.0, 190.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "text": "1",
                    "bbox": [73.0, 165.0, 93.0, 185.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 240.0, 94.0, 275.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 첫 답변입니다.",
                            "bbox": [100.0, 240.0, 525.0, 275.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 285.0, 94.0, 320.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 둘째 답변입니다.",
                            "bbox": [100.0, 285.0, 525.0, 320.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 360.0, 250.0, 385.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 근거입니다.",
                    "bbox": [80.0, 395.0, 525.0, 430.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "19편",
                    "bbox": [574.0, 110.0, 590.0, 690.0],
                    "font": "",
                    "size": 7.0,
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    result = parse_pages((page,), edition_year=2020)

    assert len(result) == 1
    candidate = result[0]
    assert (candidate.domain, candidate.part, candidate.case_no) == (
        "합성 계약",
        "합성 운영",
        "1",
    )
    assert candidate.title == "합성 카드 질문 제목"
    assert candidate.question == "합성 카드 질문 제목"
    assert candidate.answer == "합성 첫 답변입니다.\n합성 둘째 답변입니다."
    assert candidate.basis_text == "합성 근거입니다."
    assert all("19편" not in fragment.text for fragment in candidate.fragments)


def test_2020_narrow_top_part_heading_supplies_top_level_hierarchy() -> None:
    """Catches rejecting the measured narrow horizontal part heading on body pages."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-narrow-part",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [472.0, 40.0, 548.0, 58.0],
                    "font": "",
                    "size": 9.0,
                },
                {
                    "text": "합성 이전 행",
                    "bbox": [100.0, 150.0, 520.0, 160.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "text": "합성 질문 제목",
                    "bbox": [100.0, 170.0, 520.0, 190.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "text": "1",
                    "bbox": [72.0, 165.0, 92.0, 185.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 220.0, 94.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 답변입니다.",
                            "bbox": [100.0, 220.0, 525.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 300.0, 250.0, 325.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 근거입니다.",
                    "bbox": [80.0, 335.0, 525.0, 370.0],
                    "font": "",
                    "size": 11.0,
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    result = parse_pages((page,), edition_year=2020)

    assert len(result) == 1
    assert (result[0].domain, result[0].part) == ("합성 운영", "합성 운영")


def test_2020_large_adjacent_section_heading_is_not_a_qa_card() -> None:
    """Catches a measured section ordinal being promoted to a QA case number."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-section-ordinal",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [472.0, 40.0, 548.0, 58.0],
                    "font": "",
                    "size": 9.0,
                },
                {
                    "text": "1",
                    "bbox": [72.0, 122.0, 92.0, 145.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 섹션 표제",
                    "bbox": [117.0, 120.0, 525.0, 150.0],
                    "font": "",
                    "size": 15.0,
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 220.0, 94.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 본문입니다.",
                            "bbox": [100.0, 220.0, 525.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 300.0, 250.0, 325.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 본문입니다.",
                    "bbox": [80.0, 335.0, 525.0, 370.0],
                    "font": "",
                    "size": 11.0,
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    result = parse_pages((page,), edition_year=2020)

    assert result.cases == ()


def test_2020_wide_numeric_body_label_is_not_a_qa_card() -> None:
    """Catches accepting a body numeral outside the measured number-box width."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-wide-numeric-label",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [472.0, 40.0, 548.0, 58.0],
                    "font": "",
                    "size": 9.0,
                },
                {
                    "text": "1",
                    "bbox": [72.0, 122.0, 132.0, 145.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 질문 제목",
                    "bbox": [150.0, 120.0, 525.0, 145.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 220.0, 94.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 답변입니다.",
                            "bbox": [100.0, 220.0, 525.0, 255.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 300.0, 250.0, 325.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 근거입니다.",
                    "bbox": [80.0, 335.0, 525.0, 370.0],
                    "font": "",
                    "size": 11.0,
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    result = parse_pages((page,), edition_year=2020)

    assert result.cases == ()


def test_2020_audit_run_marker_closes_the_open_qa_card() -> None:
    """Catches a QA case bleeding into the following measured audit section."""
    first_raw_page = _raw_page(
        doc_id="synthetic-2020-audit-transition",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [472.0, 40.0, 548.0, 58.0],
                    "font": "",
                    "size": 9.0,
                },
                {
                    "text": "1",
                    "bbox": [72.0, 165.0, 92.0, 185.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 질문 제목",
                    "bbox": [117.0, 160.0, 525.0, 190.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "spans": [
                        {
                            "text": "∙",
                            "bbox": [80.0, 240.0, 94.0, 275.0],
                            "font": "",
                            "size": 11.0,
                        },
                        {
                            "text": "합성 답변입니다.",
                            "bbox": [100.0, 240.0, 525.0, 275.0],
                            "font": "",
                            "size": 11.0,
                        },
                    ],
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 310.0, 250.0, 335.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 QA 근거",
                    "bbox": [80.0, 350.0, 525.0, 385.0],
                    "font": "",
                    "size": 11.0,
                },
            ],
        },
    )
    second_raw_page = _raw_page(
        doc_id="synthetic-2020-audit-transition",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 21,
            "page_label": "21",
            "lines": [
                {
                    "text": "감사사례",
                    "bbox": [100.0, 80.0, 260.0, 110.0],
                    "font": "",
                    "size": 15.0,
                },
                {
                    "text": "관련 근거",
                    "bbox": [80.0, 180.0, 250.0, 205.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 감사 근거",
                    "bbox": [80.0, 220.0, 525.0, 255.0],
                    "font": "",
                    "size": 11.0,
                },
            ],
        },
    )

    pages = tuple(
        parser_page_from_raw_page(
            raw_page,
            normalized_text="synthetic projection",
            page_role_hint="body",
            source_sha256="a" * 64,
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )
        for raw_page in (first_raw_page, second_raw_page)
    )

    result = parse_pages(pages, edition_year=2020)

    assert len(result) == 1
    assert result[0].basis_text == "합성 QA 근거"
    assert {span.pdf_page_index for span in result[0].source_spans} == {20}


def test_2020_actual_audit_titles_are_individually_quarantined() -> None:
    """Catches silently dropping audit boundaries whose roles cannot be split safely."""
    raw_page = _raw_page(
        doc_id="synthetic-2020-audit-boundaries",
        year=2020,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [472.0, 40.0, 548.0, 58.0],
                    "font": "",
                    "size": 9.0,
                },
                {
                    "text": "감사사례",
                    "bbox": [100.0, 80.0, 260.0, 110.0],
                    "font": "",
                    "size": 15.0,
                },
                {
                    "text": "1. 합성 감사 제목",
                    "bbox": [63.0, 130.0, 360.0, 148.0],
                    "font": "",
                    "size": 13.0,
                },
                {
                    "text": "합성 검수 본문 하나",
                    "bbox": [75.0, 165.0, 525.0, 195.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "text": "합성 검수 본문 둘",
                    "bbox": [75.0, 205.0, 525.0, 235.0],
                    "font": "",
                    "size": 10.0,
                },
                {
                    "text": "2. 합성 감사 제목",
                    "bbox": [63.0, 280.0, 410.0, 298.0],
                    "font": "",
                    "size": 13.0,
                },
                {
                    "text": "합성 검수 본문 셋",
                    "bbox": [75.0, 315.0, 525.0, 345.0],
                    "font": "",
                    "size": 10.0,
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )

    result = parse_pages((page,), edition_year=2020)

    assert result.cases == ()
    assert len(result.quarantines) == 3
    assert all(item.reason_code == "ambiguous_boundary" for item in result.quarantines)
    assert [item.span_count for item in result.quarantines] == [1, 3, 2]
    assert all(item.source_spans for item in result.quarantines)
