"""Golden layout contracts for the 2023 OCR parser."""

from src.ingestion.parse_common import (
    parse_pages,
    parser_page_from_raw_page,
)
from tests.ingestion.test_page_continuation import (
    _raw_page,
    assert_golden_year,
    load_golden,
)


def test_2023_golden_layouts() -> None:
    """Catches specialized worker-layout role or boundary regressions."""
    assert_golden_year(2023)


def test_2023_preserves_target_and_requires_all_critical_field_review() -> None:
    """Catches OCR candidates losing 대상 or bypassing mandatory verification."""
    result = parse_pages(load_golden(2023, "first-case"), edition_year=2023)
    candidate = result[0]
    assert candidate.target_text == "합성 대상"
    assert candidate.critical_field_review == "unverified"
    assert candidate.review_status == "needs_review"
    assert not candidate.search_eligible
    assert not candidate.answer_eligible


def test_2023_actual_unlabeled_card_uses_geometry_and_preserves_split_rows() -> None:
    """Catches explicit-label fixtures hiding the production OCR card structure."""
    common = {
        "doc_id": "synthetic-2023-actual-card",
        "year": 2023,
        "source": "ocr",
    }
    hierarchy_raw = _raw_page(
        **common,
        page={
            "pdf_page_index": 12,
            "page_label": "12",
            "lines": [
                {
                    "text": "대분류: 합성 계약",
                    "bbox": [70.0, 55.0, 300.0, 85.0],
                    "font": "",
                    "size": 30.0,
                    "semantic_hint": "ocr_line",
                }
            ],
        },
    )
    card_lines = [
        {"text": "1편 합성 운영", "bbox": [350.0, 65.0, 545.0, 95.0]},
        {"text": "합성 카드", "bbox": [195.0, 205.0, 305.0, 230.0]},
        {"text": "둘째 조각", "bbox": [315.0, 205.0, 525.0, 230.0]},
        {"text": "1", "bbox": [72.0, 212.0, 105.0, 247.0]},
        {"text": "합성", "bbox": [195.0, 238.0, 275.0, 258.0]},
        {"text": "질문", "bbox": [285.0, 238.0, 360.0, 258.0]},
        {"text": "문장", "bbox": [195.0, 262.0, 275.0, 282.0]},
        {"text": "1. 대상", "bbox": [80.0, 300.0, 250.0, 325.0]},
        {"text": "합성 대상", "bbox": [80.0, 332.0, 525.0, 357.0]},
        {"text": "2. 근거", "bbox": [80.0, 375.0, 250.0, 400.0]},
        {"text": "합성 근거입니다.", "bbox": [80.0, 407.0, 525.0, 432.0]},
        {"text": "3. 답변", "bbox": [80.0, 450.0, 250.0, 475.0]},
        {"text": "합성 답변입니다.", "bbox": [80.0, 482.0, 525.0, 512.0]},
        {
            "text": "참고자료: 합성 카드 밖 참고입니다.",
            "bbox": [80.0, 550.0, 525.0, 580.0],
        },
        {"text": "합성 반복 탐색", "bbox": [572.0, 120.0, 592.0, 700.0]},
    ]
    card_raw = _raw_page(
        **common,
        page={
            "pdf_page_index": 13,
            "page_label": "13",
            "lines": [
                {
                    **line,
                    "font": "",
                    "size": float(line["bbox"][3]) - float(line["bbox"][1]),
                    "semantic_hint": "ocr_line",
                }
                for line in card_lines
            ],
        },
    )
    pages = tuple(
        parser_page_from_raw_page(
            raw_page,
            normalized_text=None,
            page_role_hint="body",
            source_sha256="a" * 64,
            upstream_review_status="needs_review",
            critical_review_policy="all-fields-human-verification",
        )
        for raw_page in (hierarchy_raw, card_raw)
    )

    result = parse_pages(pages, edition_year=2023)

    assert len(result) == 1
    candidate = result[0]
    assert (candidate.domain, candidate.part, candidate.case_no) == (
        "합성 계약",
        "합성 운영",
        "1",
    )
    assert candidate.title == "합성 카드 둘째 조각"
    assert candidate.question == "합성 질문 문장"
    assert candidate.target_text == "합성 대상"
    assert candidate.basis_text == "합성 근거입니다.\n합성 카드 밖 참고입니다."
    assert candidate.answer == "합성 답변입니다."
    assert [fragment.role for fragment in candidate.fragments].count("title") == 2
    assert [fragment.role for fragment in candidate.fragments].count("question") == 3
    header_locations = {
        (line.bbox, line.raw_text_sha256)
        for line in pages[1].lines
        if 195.0 <= line.bbox[0] and 200.0 <= line.bbox[1] < 290.0
    }
    assert header_locations.issubset(
        {(span.bbox, span.text_sha256) for span in candidate.source_spans}
    )
    assert candidate.critical_field_review == "unverified"
    assert candidate.review_status == "needs_review"
    assert not candidate.search_eligible
    assert not candidate.answer_eligible


def test_2023_terminal_basis_waits_for_next_marker_without_border_evidence() -> None:
    """Catches sentence punctuation inventing a 2023 OCR card boundary."""
    common = {
        "doc_id": "synthetic-2023-terminal-continuation",
        "year": 2023,
        "source": "ocr",
    }
    first_raw = _raw_page(
        **common,
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
                {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
                {"text": "사례 번호: 1", "bbox": [70.0, 130.0, 180.0, 155.0]},
                {"text": "제목: 합성 첫 제목", "bbox": [195.0, 130.0, 525.0, 155.0]},
                {"text": "질문: 합성 첫 질문", "bbox": [195.0, 190.0, 525.0, 220.0]},
                {"text": "답변: 합성 첫 답변", "bbox": [80.0, 240.0, 525.0, 270.0]},
                {
                    "text": "참고자료: 합성 첫 근거.",
                    "bbox": [80.0, 310.0, 525.0, 345.0],
                },
            ],
        },
    )
    second_raw = _raw_page(
        **common,
        page={
            "pdf_page_index": 2,
            "page_label": "2",
            "lines": [
                {
                    "text": "참고자료: 합성 다음 페이지 근거.",
                    "bbox": [80.0, 70.0, 525.0, 105.0],
                },
                {"text": "사례 번호: 2", "bbox": [70.0, 150.0, 180.0, 175.0]},
                {"text": "제목: 합성 둘째 제목", "bbox": [195.0, 150.0, 525.0, 175.0]},
                {
                    "text": "질문: 합성 둘째 질문",
                    "bbox": [195.0, 210.0, 525.0, 240.0],
                },
                {
                    "text": "답변: 합성 둘째 답변",
                    "bbox": [80.0, 260.0, 525.0, 290.0],
                },
                {
                    "text": "참고자료: 합성 둘째 근거.",
                    "bbox": [80.0, 330.0, 525.0, 365.0],
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
            upstream_review_status="needs_review",
            critical_review_policy="all-fields-human-verification",
        )
        for raw_page in (first_raw, second_raw)
    )

    result = parse_pages(pages, edition_year=2023)

    assert [case.case_no for case in result] == ["1", "2"]
    assert result[0].basis_text == "합성 첫 근거.\n합성 다음 페이지 근거."
    assert any(span.pdf_page_index == 2 for span in result[0].source_spans)
