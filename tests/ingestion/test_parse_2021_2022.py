"""Golden layout contracts for the 2021-2022 native parser."""

import traceback

import pytest
from pydantic import ValidationError

from src.ingestion import parse_common as parser_common
from src.ingestion.parse_common import (
    ParserLine,
    parse_pages,
    parser_page_from_raw_page,
)
from tests.ingestion.test_page_continuation import (
    _raw_page,
    assert_golden_year,
    load_golden,
)


@pytest.mark.parametrize("year", [2021, 2022])
def test_2021_2022_golden_layouts(year: int) -> None:
    """Catches yearly hierarchy, numbered roles, and front-matter regressions."""
    assert_golden_year(year)


@pytest.mark.parametrize("year", [2021, 2022])
def test_toc_number_at_right_is_never_a_case_marker(year: int) -> None:
    """Catches x≈0.775 contents-page numbers being parsed as body case numbers."""
    result = parse_pages(load_golden(year, "toc"), edition_year=year)
    assert result.cases == ()
    assert [transition.role for transition in result.transitions] == ["toc"]


@pytest.mark.parametrize("year", [2021, 2022])
def test_numbered_question_and_answer_roles_preserve_ordinals(year: int) -> None:
    """Catches 질문1/답변1 being dropped or merged without their role ordinals."""
    result = parse_pages(load_golden(year, "first-case"), edition_year=year)
    roles = [(fragment.role, fragment.ordinal) for fragment in result[0].fragments]
    expected_ordinal = None if year == 2021 else 1
    assert ("question", expected_ordinal) in roles
    assert ("answer", expected_ordinal) in roles


def test_exact_duplicate_bbox_text_is_deduped_with_count_and_one_citation() -> None:
    """Catches duplicate 2022 text-layer lines becoming duplicate case provenance."""
    pages = load_golden(2022, "audit")
    duplicate = next(line for line in pages[0].lines if line.duplicate_count == 2)
    result = parse_pages(pages, edition_year=2022)
    assert duplicate.duplicate_count == 2
    assert (
        sum(
            span.text_sha256 == duplicate.raw_text_sha256
            for span in result[0].source_spans
        )
        == 1
    )


def test_conflicting_same_bbox_text_is_quarantined() -> None:
    """Catches non-identical overlapping text-layer lines being silently deduped."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-conflict",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 10,
            "page_label": "10",
            "lines": [
                {"text": "대분류: 계약", "bbox": [80.0, 50.0, 320.0, 75.0]},
                {"text": "편: 계약 일반", "bbox": [250.0, 80.0, 500.0, 105.0]},
                {
                    "text": "1",
                    "bbox": [76.0, 120.0, 110.0, 165.0],
                    "font": "SyntheticNumber",
                    "size": 35.0,
                },
                {
                    "text": "합성 충돌 제목",
                    "bbox": [111.0, 120.0, 516.0, 155.0],
                    "font": "SyntheticTitle",
                    "size": 16.0,
                },
                {
                    "text": "답변1 합성 충돌 하나입니다.",
                    "bbox": [80.0, 220.0, 525.0, 255.0],
                },
                {
                    "text": "답변1 합성 충돌 둘입니다.",
                    "bbox": [80.0, 220.0, 525.0, 255.0],
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
    result = parse_pages((page,), edition_year=2022)
    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


@pytest.mark.parametrize("year", [2021])
def test_actual_native_numbered_rows_use_geometry_without_role_labels(
    year: int,
) -> None:
    """Catches font-name and 질문/답변-label dependence on native pages."""
    numbered_rows = (
        [
            {"text": "1) 합성 첫 질문", "bbox": [120.0, 255.0, 525.0, 285.0]},
            {"text": "합성 첫 질문 연속 행", "bbox": [136.0, 290.0, 525.0, 315.0]},
            {"text": "2) 합성 둘째 질문", "bbox": [120.0, 325.0, 525.0, 355.0]},
            {"text": "1) 합성 첫 답변", "bbox": [78.0, 395.0, 525.0, 425.0]},
            {"text": "합성 첫 답변 연속 행", "bbox": [95.0, 430.0, 525.0, 455.0]},
            {"text": "2) 합성 둘째 답변입니다.", "bbox": [78.0, 465.0, 525.0, 495.0]},
        ]
        if year == 2021
        else [
            {"text": "1) 합성 질문", "bbox": [120.0, 255.0, 525.0, 285.0]},
            {"text": "1) 합성 답변입니다.", "bbox": [120.0, 365.0, 525.0, 395.0]},
        ]
    )
    raw_page = _raw_page(
        doc_id=f"synthetic-{year}-actual-card",
        year=year,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "Ⅰ. 합성 업무분야",
                    "bbox": [495.0, 52.0, 570.0, 70.0],
                    "font": "",
                    "size": 10.6,
                },
                {
                    "text": "1편 합성 운영",
                    "bbox": [250.0, 120.0, 350.0, 145.0],
                    "font": "",
                    "size": 18.0,
                },
                {
                    "text": "3",
                    "bbox": [60.0, 170.0, 95.0, 210.0],
                    "font": "",
                    "size": 35.0,
                },
                {
                    "text": "합성 절차",
                    "bbox": [110.0, 170.0, 430.0, 205.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 카드 제목",
                    "bbox": [115.0, 238.0, 525.0, 265.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1",
                    "bbox": [76.0, 245.0, 91.0, 265.0],
                    "font": "",
                    "size": 16.0,
                },
                *[
                    {
                        **line,
                        "bbox": [
                            line["bbox"][0],
                            line["bbox"][1] + 55.0,
                            line["bbox"][2],
                            line["bbox"][3] + 55.0,
                        ],
                        "font": "",
                        "size": 11.0,
                    }
                    for line in numbered_rows
                ],
                {
                    "text": "참고자료",
                    "bbox": [80.0, 620.0, 250.0, 645.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 655.0, 525.0, 690.0],
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

    result = parse_pages((page,), edition_year=year)

    assert len(result) == 1
    candidate = result[0]
    assert (
        candidate.domain,
        candidate.part,
        candidate.subtopic,
        candidate.case_no,
    ) == (
        "합성 업무분야",
        "합성 운영",
        "합성 절차",
        "1",
    )
    assert candidate.title == "합성 카드 제목"
    if year == 2021:
        assert candidate.question == (
            "합성 첫 질문\n합성 첫 질문 연속 행\n합성 둘째 질문"
        )
        assert candidate.answer == (
            "합성 첫 답변\n합성 첫 답변 연속 행\n합성 둘째 답변입니다."
        )
        title_location = next(
            fragment.source_span
            for fragment in candidate.fragments
            if fragment.role == "title"
        )
        assert all(
            fragment.source_span != title_location
            for fragment in candidate.fragments
            if fragment.role == "question"
        )
    else:
        assert candidate.question == "합성 질문"
        assert candidate.answer == "합성 답변입니다."
    assert candidate.basis_text == "합성 참고입니다."


def test_2022_unlabeled_question_and_numbered_answer_geometry() -> None:
    """Catches rejecting the measured unnumbered-Q and numbered-answer card."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-unlabeled-question",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {"text": "소주제: 합성 절차", "bbox": [80.0, 125.0, 400.0, 150.0]},
                {
                    "text": "합성 카드 제목",
                    "bbox": [115.0, 238.0, 525.0, 265.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1",
                    "bbox": [76.0, 245.0, 91.0, 265.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 무번호 질문",
                    "bbox": [121.0, 305.0, 525.0, 335.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 질문 연속 행",
                    "bbox": [136.0, 340.0, 525.0, 365.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1) 합성 첫 답변",
                    "bbox": [78.0, 405.0, 525.0, 435.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 답변 연속 행",
                    "bbox": [95.0, 440.0, 525.0, 465.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "2) 합성 둘째 답변입니다.",
                    "bbox": [78.0, 475.0, 525.0, 505.0],
                    "font": "",
                    "size": 11.0,
                },
                {"text": "참고자료", "bbox": [80.0, 550.0, 250.0, 575.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 590.0, 525.0, 625.0],
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

    result = parse_pages((page,), edition_year=2022)

    assert len(result) == 1
    assert result[0].question == "합성 무번호 질문\n합성 질문 연속 행"
    assert result[0].answer == (
        "합성 첫 답변\n합성 답변 연속 행\n합성 둘째 답변입니다."
    )


def test_2022_split_roman_domain_and_unlabeled_question_card() -> None:
    """Catches requiring the measured Roman tab and label to be one span."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-split-domain",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "spans": [
                        {
                            "text": "Ⅱ.",
                            "bbox": [504.0, 52.0, 513.0, 70.0],
                            "font": "",
                            "size": 10.5,
                        },
                        {
                            "text": "합성 업무분야",
                            "bbox": [513.0, 52.0, 539.0, 70.0],
                            "font": "",
                            "size": 10.5,
                        },
                    ]
                },
                {
                    "text": "1편 합성 운영",
                    "bbox": [240.0, 110.0, 360.0, 135.0],
                    "font": "",
                    "size": 18.0,
                },
                {
                    "text": "3",
                    "bbox": [60.0, 170.0, 95.0, 210.0],
                    "font": "",
                    "size": 35.0,
                },
                {
                    "text": "합성 절차",
                    "bbox": [95.0, 170.0, 430.0, 205.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 카드 제목",
                    "bbox": [121.0, 238.0, 525.0, 265.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1",
                    "bbox": [88.0, 245.0, 103.0, 265.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 무번호 질문",
                    "bbox": [121.0, 305.0, 525.0, 335.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1) 합성 첫 답변",
                    "bbox": [78.0, 405.0, 525.0, 435.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "합성 답변 연속 행",
                    "bbox": [93.0, 440.0, 525.0, 465.0],
                    "font": "",
                    "size": 11.0,
                },
                {"text": "참고자료", "bbox": [80.0, 510.0, 250.0, 535.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 550.0, 525.0, 585.0],
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

    domain_lines = page.lines[:2]
    assert domain_lines[0].source_block_index == domain_lines[1].source_block_index
    assert domain_lines[0].source_line_index == domain_lines[1].source_line_index
    assert [line.source_span_index for line in domain_lines] == [0, 1]

    result = parse_pages((page,), edition_year=2022)

    assert len(result) == 1
    assert result[0].domain == "합성 업무분야"
    assert result[0].part == "합성 운영"
    assert result[0].subtopic == "합성 절차"
    assert result[0].question == "합성 무번호 질문"
    assert result[0].answer == "합성 첫 답변\n합성 답변 연속 행"
    domain_transition = next(
        transition for transition in result.transitions if transition.role == "domain"
    )
    assert domain_transition.value == "합성 업무분야"
    candidate_locations = {
        (span.bbox, span.text_sha256) for span in result[0].source_spans
    }
    assert all(
        (line.bbox, line.raw_text_sha256) not in candidate_locations
        for line in domain_lines
    )


def test_2022_split_roman_like_center_line_is_not_a_domain() -> None:
    """Catches broad same-line Roman grouping outside the measured right tab."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-split-domain-near-miss",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "spans": [
                        {
                            "text": "Ⅱ.",
                            "bbox": [390.0, 52.0, 399.0, 70.0],
                            "font": "",
                            "size": 10.5,
                        },
                        {
                            "text": "합성 본문",
                            "bbox": [399.0, 52.0, 450.0, 70.0],
                            "font": "",
                            "size": 10.5,
                        },
                    ]
                }
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

    result = parse_pages((page,), edition_year=2022)

    assert all(transition.role != "domain" for transition in result.transitions)


def test_2022_measured_wide_centered_part_heading_is_accepted() -> None:
    """Catches rejecting measured long part labels solely by heading width."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-wide-part",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "2편 합성 장문 운영 업무 절차",
                    "bbox": [177.0, 110.0, 423.0, 135.0],
                    "font": "",
                    "size": 18.0,
                }
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

    result = parse_pages((page,), edition_year=2022)

    part_transitions = [
        transition for transition in result.transitions if transition.role == "part"
    ]
    assert len(part_transitions) == 1
    assert part_transitions[0].value == "합성 장문 운영 업무 절차"


def test_2022_part_overlay_is_consumed_once_without_body_contamination() -> None:
    """Catches leaving near-identical text-layer overlays in the case body."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-part-overlay",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "1편 합성 운영",
                    "bbox": [240.0, 110.0, 360.0, 135.0],
                    "font": "",
                    "size": 18.0,
                },
                {
                    "text": "1편 합성 운영",
                    "bbox": [239.5, 110.2, 360.5, 135.2],
                    "font": "",
                    "size": 18.0,
                },
                {
                    "text": "1편 합성 운영",
                    "bbox": [240.2, 109.8, 359.8, 134.8],
                    "font": "",
                    "size": 18.0,
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

    found, consumed = parser_common._actual_ocr_hierarchy(page, page.lines)
    result = parse_pages((page,), edition_year=2022)

    assert found["part"][1] == "합성 운영"
    assert len(consumed) == 3
    assert [transition.role for transition in result.transitions] == ["part"]
    assert result.cases == ()
    assert result.quarantines == ()


def _parse_2022_actual_phase_rows(
    rows: list[dict[str, object]],
    *,
    title_bbox: list[float] | None = None,
    marker_bbox: list[float] | None = None,
) -> parser_common.ParseResult:
    raw_page = _raw_page(
        doc_id="synthetic-2022-role-phase",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "text": "합성 카드 제목",
                    "bbox": title_bbox or [121.0, 151.0, 516.0, 178.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1",
                    "bbox": marker_bbox or [88.0, 158.0, 103.0, 178.0],
                    "font": "",
                    "size": 16.0,
                },
                *rows,
                {"text": "참고자료", "bbox": [80.0, 510.0, 250.0, 535.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 550.0, 525.0, 585.0],
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
    return parse_pages((page,), edition_year=2022)


def test_2022_first_question_uses_title_bottom_not_tall_marker_bottom() -> None:
    """Catches dropping a Q row that visually follows title but overlaps number box."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "합성 무번호 질문",
                "bbox": [121.0, 170.0, 525.0, 195.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "1) 합성 답변",
                "bbox": [78.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            },
        ],
        title_bbox=[121.0, 151.0, 516.0, 166.0],
        marker_bbox=[88.0, 158.0, 103.0, 178.0],
    )

    assert len(result) == 1
    assert result[0].title == "합성 카드 제목"
    assert result[0].question == "합성 무번호 질문"


def test_2022_question_overlapping_title_is_not_promoted() -> None:
    """Catches accepting a second x≈.202 row before the title row has ended."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "합성 겹친 행",
                "bbox": [121.0, 170.0, 525.0, 195.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "1) 합성 답변",
                "bbox": [78.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            },
        ],
        title_bbox=[121.0, 151.0, 516.0, 180.0],
        marker_bbox=[88.0, 158.0, 103.0, 178.0],
    )

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


@pytest.mark.parametrize(
    ("title_text", "title_bbox"),
    [
        ("1) 합성 순번형 제목", [121.0, 151.0, 516.0, 166.0]),
        ("합성 물음표형 제목?", [121.0, 151.0, 516.0, 166.0]),
        ("합성 후행형 제목", [121.0, 160.0, 516.0, 172.0]),
    ],
)
def test_2022_same_block_header_title_precedes_body_content_regex(
    title_text: str,
    title_bbox: list[float],
) -> None:
    """Catches ordinal/question-looking title rows being parsed as body Q rows."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-content-shaped-title",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "raw_lines": [
                        {
                            "text": "1",
                            "bbox": [88.0, 158.0, 103.0, 178.0],
                            "font": "",
                            "size": 16.0,
                        },
                        {
                            "text": title_text,
                            "bbox": title_bbox,
                            "font": "",
                            "size": 11.0,
                        },
                    ]
                },
                {
                    "text": "합성 별도 질문",
                    "bbox": [121.0, 180.0, 525.0, 205.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1) 합성 답변",
                    "bbox": [78.0, 230.0, 525.0, 260.0],
                    "font": "",
                    "size": 11.0,
                },
                {"text": "참고자료", "bbox": [80.0, 310.0, 250.0, 335.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 350.0, 525.0, 385.0],
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

    result = parse_pages((page,), edition_year=2022)

    assert len(result) == 1
    assert result[0].title == title_text
    assert result[0].question == "합성 별도 질문"


def test_2022_question_label_in_answer_band_stays_answer_continuation() -> None:
    """Catches body text beginning 질문 being mistaken for an A→Q phase restart."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "합성 무번호 질문",
                "bbox": [121.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "1) 합성 답변",
                "bbox": [78.0, 310.0, 525.0, 340.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "질문2: 합성 답변 내부 라벨",
                "bbox": [78.0, 360.0, 525.0, 390.0],
                "font": "",
                "size": 11.0,
            },
        ]
    )

    assert len(result) == 1
    assert result[0].answer == "합성 답변\n질문2: 합성 답변 내부 라벨"


def test_2022_same_block_split_title_fragments_join_in_visual_order() -> None:
    """Catches dropping measured multi-line-object pieces of one title row."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-split-title",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "raw_lines": [
                        {
                            "text": "1",
                            "bbox": [88.0, 158.0, 103.0, 178.0],
                            "font": "",
                            "size": 16.0,
                        },
                        {
                            "spans": [
                                {
                                    "text": "합성",
                                    "bbox": [121.0, 151.0, 250.0, 166.0],
                                    "font": "",
                                    "size": 11.0,
                                },
                                {
                                    "text": "분할",
                                    "bbox": [250.0, 151.0, 380.0, 166.0],
                                    "font": "",
                                    "size": 11.0,
                                },
                                {
                                    "text": "제목",
                                    "bbox": [380.0, 151.0, 516.0, 166.0],
                                    "font": "",
                                    "size": 11.0,
                                },
                            ]
                        },
                    ]
                },
                {
                    "text": "합성 별도 질문",
                    "bbox": [121.0, 180.0, 525.0, 205.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1) 합성 답변",
                    "bbox": [78.0, 230.0, 525.0, 260.0],
                    "font": "",
                    "size": 11.0,
                },
                {"text": "참고자료", "bbox": [80.0, 310.0, 250.0, 335.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 350.0, 525.0, 385.0],
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

    result = parse_pages((page,), edition_year=2022)

    assert len(result) == 1
    assert result[0].title == "합성 분할 제목"
    title_fragments = [
        fragment for fragment in result[0].fragments if fragment.role == "title"
    ]
    assert len(title_fragments) == 3
    assert [fragment.source_span.bbox[0] for fragment in title_fragments] == [
        121.0,
        250.0,
        380.0,
    ]
    assert (
        len(
            {
                (
                    line.source_block_index,
                    line.source_line_index,
                )
                for line in page.lines
                if line.bbox[1] == 151.0
            }
        )
        == 1
    )
    assert result[0].question == "합성 별도 질문"


def test_2022_monotonic_role_bands_allow_noncontiguous_answer_ordinals() -> None:
    """Catches treating numbered answer-list items as Q/A pair identifiers."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "합성 무번호 질문",
                "bbox": [121.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "2) 합성 첫 답변",
                "bbox": [78.0, 310.0, 525.0, 340.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "7) 합성 둘째 답변",
                "bbox": [78.0, 360.0, 525.0, 390.0],
                "font": "",
                "size": 11.0,
            },
        ]
    )

    assert len(result) == 1
    assert result[0].question == "합성 무번호 질문"
    assert result[0].answer == "합성 첫 답변\n합성 둘째 답변"


def test_2022_question_band_reentry_after_answer_is_quarantined() -> None:
    """Catches accepting a Q-band restart after the card entered answer phase."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "1) 합성 첫 질문",
                "bbox": [121.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "1) 합성 답변",
                "bbox": [78.0, 310.0, 525.0, 340.0],
                "font": "",
                "size": 11.0,
            },
            {
                "text": "질문2: 합성 후행 질문",
                "bbox": [121.0, 360.0, 525.0, 390.0],
                "font": "",
                "size": 11.0,
            },
        ]
    )

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "text": "1) 합성 답변만",
                "bbox": [78.0, 310.0, 525.0, 340.0],
                "font": "",
                "size": 11.0,
            }
        ],
        [
            {
                "text": "1) 합성 질문만",
                "bbox": [121.0, 230.0, 525.0, 260.0],
                "font": "",
                "size": 11.0,
            }
        ],
    ],
)
def test_2022_missing_question_or_answer_phase_is_quarantined(
    rows: list[dict[str, object]],
) -> None:
    """Catches promoting a card without both measured role phases."""
    result = _parse_2022_actual_phase_rows(rows)

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


def test_2022_title_and_plain_left_answer_without_question_is_quarantined() -> None:
    """Catches applying the reviewed 2021 title-query fallback to 2022 cards."""
    result = _parse_2022_actual_phase_rows(
        [
            {
                "text": "합성 답변만 있는 본문",
                "bbox": [78.0, 310.0, 525.0, 340.0],
                "font": "",
                "size": 11.0,
            }
        ]
    )

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


def test_parser_model_validation_hides_untrusted_input_from_error_surfaces() -> None:
    """Catches direct parser-model validation echoing source text in diagnostics."""
    sentinel = "SYNTHETIC-UNTRUSTED-PARSER-LINE"
    with pytest.raises(ValidationError) as captured:
        ParserLine.model_validate(sentinel)

    error = captured.value
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )
    assert sentinel not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_2021_center_roman_like_header_is_not_a_domain() -> None:
    """Catches broad Roman matching outside the measured top-right domain band."""
    raw_page = _raw_page(
        doc_id="synthetic-2021-center-roman",
        year=2021,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {
                    "text": "Ⅰ. 합성 본문",
                    "bbox": [400.0, 52.0, 475.0, 70.0],
                    "font": "",
                    "size": 10.6,
                }
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

    result = parse_pages((page,), edition_year=2021)

    assert all(transition.role != "domain" for transition in result.transitions)


def test_2021_plain_answer_band_uses_title_as_question_only_without_q_band() -> None:
    """Catches losing measured cards whose heading is their only query evidence."""
    raw_page = _raw_page(
        doc_id="synthetic-2021-title-question-fallback",
        year=2021,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "text": "합성 카드 제목",
                    "bbox": [115.0, 238.0, 525.0, 265.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1",
                    "bbox": [76.0, 245.0, 91.0, 265.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 답변 본문입니다.",
                    "bbox": [78.0, 305.0, 525.0, 335.0],
                    "font": "",
                    "size": 11.0,
                },
                {"text": "참고자료", "bbox": [80.0, 375.0, 250.0, 400.0]},
                {
                    "text": "합성 참고입니다.",
                    "bbox": [80.0, 415.0, 525.0, 450.0],
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

    result = parse_pages((page,), edition_year=2021)

    assert len(result) == 1
    candidate = result[0]
    assert candidate.title == candidate.question == "합성 카드 제목"
    assert candidate.answer == "합성 답변 본문입니다."
    title_fragment = next(
        fragment for fragment in candidate.fragments if fragment.role == "title"
    )
    question_fragment = next(
        fragment for fragment in candidate.fragments if fragment.role == "question"
    )
    assert question_fragment.source_span == title_fragment.source_span


@pytest.mark.parametrize("year", [2020, 2021, 2022])
def test_actual_native_audit_title_is_locatably_quarantined(year: int) -> None:
    """Catches promoting an audit title when facts and answer have no safe split."""
    raw_page = _raw_page(
        doc_id=f"synthetic-{year}-audit-boundary",
        year=year,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "text": "감사사례",
                    "bbox": [80.0, 125.0, 250.0, 145.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1. 합성 감사 제목",
                    "bbox": [63.0, 160.0, 390.0, 174.0],
                    "font": "",
                    "size": 14.0,
                },
                {
                    "text": "합성 검수 본문",
                    "bbox": [75.0, 205.0, 525.0, 235.0],
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

    result = parse_pages((page,), edition_year=year)

    assert result.cases == ()
    assert len(result.quarantines) == 2
    assert all(item.reason_code == "ambiguous_boundary" for item in result.quarantines)
    audit_spans = [
        (span.pdf_page_index, span.bbox, span.text_sha256)
        for item in result.quarantines
        for span in item.source_spans
    ]
    assert len(audit_spans) == len(set(audit_spans)) == 3


def test_2022_native_audit_title_without_run_marker_is_quarantined() -> None:
    """Catches dropping an unmarked audit run instead of preserving its spans."""
    raw_page = _raw_page(
        doc_id="synthetic-2022-unmarked-audit",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 20,
            "page_label": "20",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {
                    "text": "합성 감사 구간 표식",
                    "bbox": [80.0, 125.0, 300.0, 145.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "1. 합성 감사 제목",
                    "bbox": [63.0, 160.0, 390.0, 174.0],
                    "font": "",
                    "size": 14.0,
                },
                {
                    "text": "합성 검수 본문",
                    "bbox": [75.0, 205.0, 525.0, 235.0],
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

    result = parse_pages((page,), edition_year=2022)

    assert result.cases == ()
    assert len(result.quarantines) == 1
    quarantine = result.quarantines[0]
    assert quarantine.reason_code == "ambiguous_boundary"
    assert len(quarantine.source_spans) == 3
