"""Golden layout contracts for the 2024-2025 OCR card parser."""

import hashlib
import inspect
import json

import pytest

from src.ingestion.extract_common import LayoutEvidence
from src.ingestion.parse_2024_2025 import parse_document
from src.ingestion.parse_common import (
    LayoutSegmentProvenance,
    ParserContractError,
    parse_pages,
    parser_page_from_raw_page,
)
from tests.ingestion.test_page_continuation import (
    _raw_page,
    assert_golden_year,
    load_golden,
)


def _actual_card_page(
    year: int,
    lines: list[dict[str, object]],
    regions: list[list[float]],
):
    ocr_lines = []
    for line in lines:
        bbox = line["bbox"]
        assert isinstance(bbox, list)
        ocr_lines.append(
            {
                **line,
                "font": "",
                "size": float(bbox[3]) - float(bbox[1]),
                "semantic_hint": "ocr_line",
            }
        )
    raw_page = _raw_page(
        doc_id=f"synthetic-{year}-actual-card",
        year=year,
        source="ocr",
        page={
            "pdf_page_index": 10,
            "page_label": "10",
            "lines": ocr_lines,
            "layout_evidence": {
                "status": "detected",
                "detector_version": "synthetic-card-v1",
                "regions": [
                    {
                        "region_type": "card",
                        "bbox": bbox,
                        "evidence": "raster-border",
                    }
                    for bbox in regions
                ],
            },
        },
    )
    source_sha256 = hashlib.sha256(
        f"{raw_page.doc_id}:synthetic-source".encode()
    ).hexdigest()
    sampling_status = "all_cases_required" if year == 2024 else "sampling_required"
    segment_end = 323 if year == 2024 else 313
    registry_payload = {
        "detector_version": "synthetic-card-v1",
        "doc_id": raw_page.doc_id,
        "edition_year": year,
        "policy_version": "layout-segment-registry-v1",
        "sampling_status": sampling_status,
        "segment_end_pdf_page": segment_end,
        "segment_key": "approved-document-body",
        "segment_start_pdf_page": 1,
        "source_sha256": source_sha256,
    }
    return parser_page_from_raw_page(
        raw_page,
        normalized_text=None,
        page_role_hint="body",
        upstream_review_status=(
            "needs_review" if year == 2024 else "machine_extracted"
        ),
        critical_review_policy=(
            "all-fields-human-verification"
            if year == 2024
            else "stratified-sample-with-layout-escalation"
        ),
        layout_segment_provenance=LayoutSegmentProvenance(
            segment_id=f"synthetic-{year}-actual-segment",
            segment_key="approved-document-body",
            segment_start_pdf_page=1,
            segment_end_pdf_page=segment_end,
            registry_policy_version="layout-segment-registry-v1",
            registry_sha256=hashlib.sha256(
                b"sen-qa-layout-segment-registry-v1\0"
                + json.dumps(
                    registry_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest(),
            detector_version="synthetic-card-v1",
            region_count=len(regions),
            sampling_status=sampling_status,
            doc_id=raw_page.doc_id,
            edition_year=year,
            source_sha256=source_sha256,
            pdf_page_index=raw_page.pdf_page_index,
            render_sha256=raw_page.render_sha256,
        ),
        source_sha256=source_sha256,
    )


def _actual_hierarchy_lines(year: int) -> list[dict[str, object]]:
    common = [
        {
            "text": "10",
            "bbox": [290.0, 765.0, 315.0, 790.0],
            "confidence": 0.96,
        },
    ]
    if year == 2024:
        return [
            {
                "text": "IV",
                "bbox": [430.0, 32.0, 455.0, 57.0],
                "confidence": 0.96,
            },
            {
                "text": "합성 계약",
                "bbox": [465.0, 32.0, 550.0, 57.0],
                "confidence": 0.96,
            },
            {
                "text": "4",
                "bbox": [60.0, 160.0, 105.0, 200.0],
                "confidence": 0.96,
            },
            {
                "text": "합성 운영",
                "bbox": [115.0, 160.0, 430.0, 200.0],
                "confidence": 0.96,
            },
            {
                "text": "합성 반복 탐색",
                "bbox": [572.0, 240.0, 592.0, 700.0],
                "confidence": 0.96,
            },
            *common,
        ]
    return [
        {
            "text": "1편 합성 운영",
            "bbox": [220.0, 88.0, 420.0, 120.0],
            "confidence": 0.96,
        },
        {
            "text": "I.",
            "bbox": [570.0, 35.0, 592.0, 60.0],
            "confidence": 0.96,
        },
        {
            "text": "합성",
            "bbox": [570.0, 65.0, 592.0, 90.0],
            "confidence": 0.96,
        },
        {
            "text": "계약",
            "bbox": [570.0, 95.0, 592.0, 120.0],
            "confidence": 0.96,
        },
        {
            "text": "4",
            "bbox": [60.0, 155.0, 105.0, 195.0],
            "confidence": 0.96,
        },
        {
            "text": "합성 절차",
            "bbox": [115.0, 155.0, 430.0, 195.0],
            "confidence": 0.96,
        },
        *common,
    ]


def _actual_first_card_top(year: int) -> float:
    return 217.0 if year == 2024 else 202.0


def _actual_case_lines(case_no: str, top: float, label: str) -> list[dict[str, object]]:
    return [
        {
            "text": "합성 카드",
            "bbox": [195.0, top + 10.0, 300.0, top + 35.0],
            "confidence": 0.96,
        },
        {
            "text": f"{label} 제목",
            "bbox": [310.0, top + 10.0, 525.0, top + 35.0],
            "confidence": 0.96,
        },
        {
            "text": case_no,
            "bbox": [72.0, top + 16.0, 105.0, top + 48.0],
            "confidence": 0.96,
        },
        {
            "text": "합성",
            "bbox": [195.0, top + 48.0, 275.0, top + 68.0],
            "confidence": 0.96,
        },
        {
            "text": f"{label} 질문",
            "bbox": [285.0, top + 48.0, 420.0, top + 68.0],
            "confidence": 0.96,
        },
        {
            "text": "문장",
            "bbox": [195.0, top + 70.0, 275.0, top + 90.0],
            "confidence": 0.96,
        },
        {
            "text": "1. 근거",
            "bbox": [80.0, top + 100.0, 250.0, top + 125.0],
            "confidence": 0.96,
        },
        {
            "text": f"합성 {label} 근거입니다.",
            "bbox": [80.0, top + 130.0, 525.0, top + 155.0],
            "confidence": 0.96,
        },
        {
            "text": "2. 답변",
            "bbox": [80.0, top + 165.0, 250.0, top + 190.0],
            "confidence": 0.96,
        },
        {
            "text": f"합성 {label} 답변입니다.",
            "bbox": [80.0, top + 195.0, 525.0, top + 225.0],
            "confidence": 0.96,
        },
    ]


@pytest.mark.parametrize("year", [2024, 2025])
def test_2024_2025_golden_layouts(year: int) -> None:
    """Catches card, role, vertical-tab, and layout-policy regressions."""
    assert_golden_year(year)


def test_2024_all_cases_remain_in_critical_review() -> None:
    """Catches 2024 low-resolution OCR candidates escaping the review queue."""
    candidate = parse_pages(load_golden(2024, "first-case"), edition_year=2024)[0]
    assert candidate.critical_field_review == "unverified"
    assert candidate.review_status == "needs_review"
    assert candidate.layout_segment_id == "synthetic-2024-segment-a"


def test_2025_records_layout_segment_sampling_provenance_without_approving() -> None:
    """Catches sample policy provenance being lost or promoted to search approval."""
    candidate = parse_pages(load_golden(2025, "first-case"), edition_year=2025)[0]
    assert candidate.layout_segment_id == "synthetic-2025-segment-a"
    assert candidate.critical_field_review == "sampling_required"
    assert candidate.review_status == "machine_extracted"
    assert not candidate.search_eligible
    assert not candidate.answer_eligible


def test_2025_upstream_review_status_is_never_downgraded() -> None:
    """Catches a reviewed OCR page becoming machine-extracted after parsing."""
    top = _actual_first_card_top(2025)
    page = _actual_card_page(
        2025,
        [
            *_actual_hierarchy_lines(2025),
            *_actual_case_lines("1", top, "검수"),
        ],
        [[60.0, top, 540.0, top + 240.0]],
    ).model_copy(update={"upstream_review_status": "needs_review"})

    candidate = parse_pages((page,), edition_year=2025)[0]

    assert candidate.upstream_review_status == "needs_review"
    assert candidate.review_status == "needs_review"


def test_2025_any_contributing_review_page_keeps_candidate_in_review() -> None:
    """Catches a machine first page hiding a reviewed continuation page."""
    pages = load_golden(2025, "continuation")
    mixed = (
        pages[0].model_copy(update={"upstream_review_status": "machine_extracted"}),
        pages[1].model_copy(update={"upstream_review_status": "needs_review"}),
    )

    result = parse_pages(mixed, edition_year=2025)

    assert len(result) == 2
    assert result[0].source_spans[-1].pdf_page_index == mixed[1].pdf_page_index
    assert all(case.upstream_review_status == "needs_review" for case in result)
    assert all(case.review_status == "needs_review" for case in result)


def test_layout_segment_full_binding_reaches_candidate_without_external_mapping() -> (
    None
):
    """Catches page-index-only segment IDs losing their approved registry binding."""
    page = load_golden(2025, "first-case")[0]
    candidate = parse_pages((page,), edition_year=2025)[0]
    assert len(candidate.layout_segment_provenances) == 1
    provenance = candidate.layout_segment_provenances[0]
    assert provenance.segment_id == candidate.layout_segment_id
    assert provenance.doc_id == page.doc_id
    assert provenance.edition_year == page.edition_year
    assert provenance.pdf_page_index == page.pdf_page_index
    assert provenance.render_sha256 == page.render_sha256
    assert provenance.detector_version == page.layout_evidence.detector_version
    assert provenance.region_count == len(page.layout_evidence.regions)
    assert provenance.sampling_status == "sampling_required"
    assert "layout_segments_by_page" not in inspect.signature(parse_document).parameters


def test_layout_segment_binding_covers_every_candidate_page_and_rejects_drift() -> None:
    """Catches continuation render provenance loss or forged registry drift."""
    pages = load_golden(2025, "continuation")
    result = parse_pages(pages, edition_year=2025)
    assert [
        [item.pdf_page_index for item in case.layout_segment_provenances]
        for case in result
    ] == [[41, 42], [42]]
    segment = pages[0].layout_segment_provenance
    assert segment is not None
    forged = pages[0].model_copy(
        update={
            "layout_segment_provenance": segment.model_copy(
                update={"registry_sha256": "f" * 64}
            )
        }
    )
    with pytest.raises(
        ParserContractError, match="parser page contract is invalid"
    ) as captured:
        parse_pages((forged,), edition_year=2025)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("year", [2024, 2025])
def test_missing_card_evidence_is_quarantined_not_inferred(year: int) -> None:
    """Catches graphical card borders being invented from OCR line geometry alone."""
    pages = load_golden(year, "first-case")
    without_evidence = pages[0].model_copy(
        update={
            "layout_evidence": LayoutEvidence(
                status="unavailable", detector_version=None, regions=()
            ),
            "layout_segment_provenance": pages[0].layout_segment_provenance.model_copy(
                update={"region_count": 0}
            )
            if pages[0].layout_segment_provenance is not None
            else None,
        }
    )
    result = parse_pages((without_evidence,), edition_year=year)
    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


@pytest.mark.parametrize("year", [2024, 2025])
def test_vertical_domain_tab_updates_metadata_but_never_opens_a_case(year: int) -> None:
    """Catches the right-side category tab being mistaken for a case marker."""
    result = parse_pages(load_golden(year, "first-case"), edition_year=year)
    assert len(result) == 1
    assert result[0].domain == "계약"
    assert [transition.role for transition in result.transitions].count("domain") == 1


@pytest.mark.parametrize("year", [2024, 2025])
def test_plain_digit_in_card_opens_case_even_when_title_arrives_first(
    year: int,
) -> None:
    """Catches OCR source order or missing 사례 label hiding the number/title pair."""
    page = _actual_card_page(
        year,
        [
            *_actual_hierarchy_lines(year),
            *_actual_case_lines("1", _actual_first_card_top(year), "순서"),
        ],
        [
            [
                60.0,
                _actual_first_card_top(year),
                540.0,
                _actual_first_card_top(year) + 240.0,
            ]
        ],
    )
    result = parse_pages((page,), edition_year=year)
    assert len(result) == 1
    assert result[0].case_no == "1"
    assert result[0].domain == "합성 계약"
    assert result[0].part == "합성 운영"
    assert result[0].subtopic == ("합성 절차" if year == 2025 else None)
    assert result[0].title == "합성 카드 순서 제목"
    assert result[0].question == "합성 순서 질문 문장"
    number = next(line for line in page.lines if line.raw_text == "1")
    title = next(line for line in page.lines if line.raw_text == "합성 카드")
    assert title.source_block_index < number.source_block_index
    assert page.lines.index(title) < page.lines.index(number)
    title_location = (title.bbox, title.raw_text_sha256)
    number_location = (number.bbox, number.raw_text_sha256)
    candidate_locations = [
        (span.bbox, span.text_sha256) for span in result[0].source_spans
    ]
    assert candidate_locations.index(title_location) < candidate_locations.index(
        number_location
    )


@pytest.mark.parametrize("year", [2024, 2025])
def test_card_regions_group_two_cases_without_right_tab_bleed(year: int) -> None:
    """Catches same-page cards or the vertical tab being merged into one candidate."""
    page = _actual_card_page(
        year,
        [
            *_actual_hierarchy_lines(year),
            *_actual_case_lines("1", _actual_first_card_top(year), "하나"),
            *(
                [
                    {
                        "text": "참고자료: 합성 카드 밖 참고입니다.",
                        "bbox": [80.0, 455.0, 525.0, 485.0],
                        "confidence": 0.96,
                    }
                ]
                if year == 2025
                else []
            ),
            *_actual_case_lines("2", 500.0, "둘"),
        ],
        [
            [
                60.0,
                _actual_first_card_top(year),
                540.0,
                _actual_first_card_top(year) + 240.0,
            ],
            [60.0, 500.0, 540.0, 740.0],
        ],
    )
    result = parse_pages((page,), edition_year=year)
    assert [case.case_no for case in result.cases] == ["1", "2"]
    assert [(case.domain, case.part) for case in result] == [
        ("합성 계약", "합성 운영"),
        ("합성 계약", "합성 운영"),
    ]
    assert [case.subtopic for case in result] == [
        "합성 절차" if year == 2025 else None,
        "합성 절차" if year == 2025 else None,
    ]
    assert [case.title for case in result] == [
        "합성 카드 하나 제목",
        "합성 카드 둘 제목",
    ]
    assert [case.question for case in result] == [
        "합성 하나 질문 문장",
        "합성 둘 질문 문장",
    ]
    assert [case.answer for case in result] == [
        "합성 하나 답변입니다.",
        "합성 둘 답변입니다.",
    ]
    expected_first_basis = "합성 하나 근거입니다."
    if year == 2025:
        expected_first_basis += "\n합성 카드 밖 참고입니다."
    assert [case.basis_text for case in result] == [
        expected_first_basis,
        "합성 둘 근거입니다.",
    ]
    tab_locations = {
        (line.bbox, line.raw_text_sha256)
        for line in page.lines
        if line.bbox[0] >= 570.0
    }
    case_locations = {
        (span.bbox, span.text_sha256) for case in result for span in case.source_spans
    }
    assert tab_locations.isdisjoint(case_locations)


def test_2025_numbered_roles_and_post_card_reference_attach_by_state() -> None:
    """Catches 1.근거/2.답변 headings or a bounded trailing reference being dropped."""
    page = _actual_card_page(
        2025,
        [
            *_actual_hierarchy_lines(2025),
            *_actual_case_lines("1", _actual_first_card_top(2025), "역할"),
            {
                "text": "참고자료: 합성 카드 후 참고입니다.",
                "bbox": [80.0, 455.0, 525.0, 485.0],
                "confidence": 0.96,
            },
        ],
        [[60.0, 202.0, 540.0, 442.0]],
    )
    result = parse_pages((page,), edition_year=2025)
    assert len(result) == 1
    assert result[0].answer == "합성 역할 답변입니다."
    assert result[0].basis_text == "합성 역할 근거입니다.\n합성 카드 후 참고입니다."


@pytest.mark.parametrize(
    ("fixture_name", "missing_hint"),
    [("first-case", "question"), ("audit", "facts")],
)
def test_required_case_roles_fail_closed(fixture_name: str, missing_hint: str) -> None:
    """Catches incomplete QA or audit records becoming searchable candidates."""
    page = load_golden(2025, fixture_name)[0]
    if missing_hint == "question":
        retained = tuple(
            line for line in page.lines if line.semantic_hint != "question"
        )
    else:
        retained = tuple(
            line
            for line in page.lines
            if not line.normalized_text.startswith("감사 사실")
        )
    incomplete = page.model_copy(update={"lines": retained})
    result = parse_pages((incomplete,), edition_year=2025)
    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]
