"""Cross-page and fail-closed contracts for annual case parsing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src.ingestion.extract_common import (
    BoundingBox,
    LayoutEvidence,
    LayoutRegion,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
)
from src.ingestion.extract_native import ExtractedPageRecord, QuarantinedPageRecord
from src.ingestion.extract_ocr import QuarantinedOcrPageRecord
from src.ingestion.parse_common import (
    LayoutSegmentProvenance,
    ParserContractError,
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    canonical_result_bytes,
    parse_pages,
    parser_page_from_native_record,
    parser_page_from_ocr_record,
    parser_page_from_raw_page,
)

FIXTURE_ROOT = Path("tests/fixtures/page-golden")
EXPECTED_ROOT = Path("tests/fixtures/parsed-cases")
GOLDEN_NAMES = (
    "cover",
    "toc",
    "first-case",
    "continuation",
    "audit",
    "last-case",
    "credits",
)


def _raw_page(
    *,
    doc_id: str,
    year: int,
    source: str,
    page: dict[str, Any],
) -> RawPage:
    blocks: list[RawBlock] = []
    for item in page["lines"]:
        raw_lines: list[RawLine] = []
        for line_item in item.get("raw_lines", (item,)):
            span_items = line_item.get("spans", (line_item,))
            spans: list[RawSpan] = []
            for span_item in span_items:
                span_bbox = BoundingBox.from_tuple(
                    tuple(float(value) for value in span_item["bbox"])
                )
                span_confidence = float(span_item.get("confidence", 1.0))
                spans.append(
                    RawSpan(
                        text=span_item["text"],
                        bbox=span_bbox,
                        font=span_item.get("font", "SyntheticFixture"),
                        size=float(span_item.get("size", 10.0)),
                        confidence=span_confidence,
                        semantic_hint=span_item.get("semantic_hint"),
                    )
                )
            line_bbox = BoundingBox(
                x0=min(span.bbox.x0 for span in spans),
                y0=min(span.bbox.y0 for span in spans),
                x1=max(span.bbox.x1 for span in spans),
                y1=max(span.bbox.y1 for span in spans),
            )
            raw_lines.append(
                RawLine(
                    bbox=line_bbox,
                    spans=tuple(spans),
                    confidence=min(span.confidence for span in spans),
                )
            )
        block_bbox = BoundingBox(
            x0=min(line.bbox.x0 for line in raw_lines),
            y0=min(line.bbox.y0 for line in raw_lines),
            x1=max(line.bbox.x1 for line in raw_lines),
            y1=max(line.bbox.y1 for line in raw_lines),
        )
        blocks.append(RawBlock(bbox=block_bbox, lines=tuple(raw_lines)))
    render_sha256 = hashlib.sha256(
        f"{doc_id}:{page['pdf_page_index']}".encode()
    ).hexdigest()
    layout_payload = page.get("layout_evidence")
    layout_evidence = LayoutEvidence()
    if layout_payload is not None:
        layout_evidence = LayoutEvidence(
            status=layout_payload["status"],
            detector_version=layout_payload["detector_version"],
            regions=tuple(
                LayoutRegion(
                    region_type=region["region_type"],
                    bbox=BoundingBox.from_tuple(
                        tuple(float(value) for value in region["bbox"])
                    ),
                    evidence=region["evidence"],
                )
                for region in layout_payload["regions"]
            ),
        )
    return RawPage(
        doc_id=doc_id,
        edition_year=year,
        extraction_source=source,
        pdf_page_index=page["pdf_page_index"],
        page_label=page["page_label"],
        page_width=600.0,
        page_height=800.0,
        render_sha256=render_sha256,
        raw_blocks=tuple(blocks),
        layout_evidence=layout_evidence,
    )


def _boundary_parser_page(
    *,
    doc_id: str,
    year: int,
    source: str,
    pdf_page_index: int,
    page_role_hint: str,
    lines: list[dict[str, Any]],
    upstream_review_status: str,
) -> ParserPage:
    raw_page = _raw_page(
        doc_id=doc_id,
        year=year,
        source=source,
        page={
            "pdf_page_index": pdf_page_index,
            "page_label": str(pdf_page_index),
            "lines": lines,
        },
    )
    return parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint=page_role_hint,
        source_sha256="a" * 64,
        upstream_review_status=upstream_review_status,
        critical_review_policy=(
            "not_applicable" if year <= 2022 else "all-fields-human-verification"
        ),
    )


def _layout_segment(
    payload: dict[str, Any] | None,
    raw_page: RawPage,
) -> LayoutSegmentProvenance | None:
    if payload is None:
        return None
    detector_version = raw_page.layout_evidence.detector_version
    assert detector_version is not None
    source_sha256 = hashlib.sha256(
        f"{raw_page.doc_id}:synthetic-source".encode()
    ).hexdigest()
    sampling_status = payload["sample_status"]
    segment_end = 323 if raw_page.edition_year == 2024 else 313
    registry_payload = {
        "detector_version": detector_version,
        "doc_id": raw_page.doc_id,
        "edition_year": raw_page.edition_year,
        "policy_version": "layout-segment-registry-v1",
        "sampling_status": sampling_status,
        "segment_end_pdf_page": segment_end,
        "segment_key": "approved-document-body",
        "segment_start_pdf_page": 1,
        "source_sha256": source_sha256,
    }
    return LayoutSegmentProvenance(
        segment_id=payload["segment_id"],
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
        detector_version=detector_version,
        region_count=len(raw_page.layout_evidence.regions),
        sampling_status=sampling_status,
        doc_id=raw_page.doc_id,
        edition_year=raw_page.edition_year,
        source_sha256=source_sha256,
        pdf_page_index=raw_page.pdf_page_index,
        render_sha256=raw_page.render_sha256,
    )


def load_golden(year: int, name: str) -> tuple[ParserPage, ...]:
    payload = json.loads(
        (FIXTURE_ROOT / str(year) / f"{name}.json").read_text(encoding="utf-8")
    )
    pages: list[ParserPage] = []
    for page_payload in payload["pages"]:
        raw_page = _raw_page(
            doc_id=payload["doc_id"],
            year=payload["edition_year"],
            source=payload["extraction_source"],
            page=page_payload,
        )
        pages.append(
            parser_page_from_raw_page(
                raw_page,
                normalized_text="\n".join(
                    item["text"] for item in page_payload["lines"]
                ),
                page_role_hint=page_payload["page_role_hint"],
                layout_segment_provenance=_layout_segment(
                    page_payload.get("layout_segment_provenance"), raw_page
                ),
                source_sha256=hashlib.sha256(
                    f"{raw_page.doc_id}:synthetic-source".encode()
                ).hexdigest(),
                upstream_review_status=(
                    "machine_extracted"
                    if year in (2020, 2021, 2022, 2025)
                    else "needs_review"
                ),
                critical_review_policy=(
                    "not_applicable"
                    if year in (2020, 2021, 2022)
                    else (
                        "all-fields-human-verification"
                        if year in (2023, 2024)
                        else "stratified-sample-with-layout-escalation"
                    )
                ),
            )
        )
    return tuple(pages)


def result_summary(result: ParseResult) -> dict[str, Any]:
    return {
        "cases": [
            {
                "case_no": case.case_no,
                "case_type": case.case_type,
                "title": case.title,
                "question": case.question,
                "answer": case.answer,
                "facts": case.facts,
                "basis_text": case.basis_text,
                "target_text": case.target_text,
                "situation_text": case.situation_text,
                "source_spans": [
                    span.model_dump(mode="json") for span in case.source_spans
                ],
                "review_status": case.review_status,
                "critical_field_review": case.critical_field_review,
                "layout_segment_id": case.layout_segment_id,
                "search_eligible": case.search_eligible,
                "answer_eligible": case.answer_eligible,
            }
            for case in result.cases
        ],
        "quarantines": [
            {
                "reason_code": quarantine.reason_code,
                "page_ids": list(quarantine.page_ids),
                "span_count": quarantine.span_count,
            }
            for quarantine in result.quarantines
        ],
        "transitions": [
            {
                "pdf_page_index": transition.pdf_page_index,
                "role": transition.role,
                "value": transition.value,
            }
            for transition in result.transitions
        ],
    }


def assert_golden_year(year: int) -> None:
    assert {path.stem for path in (FIXTURE_ROOT / str(year)).glob("*.json")} == set(
        GOLDEN_NAMES
    )
    assert {
        path.name.removesuffix(".expected.json")
        for path in (EXPECTED_ROOT / str(year)).glob("*.expected.json")
    } == set(GOLDEN_NAMES)
    for name in GOLDEN_NAMES:
        result = parse_pages(load_golden(year, name), edition_year=year)
        expected = json.loads(
            (EXPECTED_ROOT / str(year) / f"{name}.expected.json").read_text(
                encoding="utf-8"
            )
        )
        assert result_summary(result) == expected, f"{year}/{name}"


def test_answer_continues_until_next_case_marker() -> None:
    """Catches a page break splitting a still-open answer from its reference material."""
    result = parse_pages(load_golden(2022, "continuation"), edition_year=2022)

    assert len(result) == 2
    assert "다음 페이지의 참고자료" in (result[0].basis_text or "")
    assert result[0].source_spans[-1].pdf_page_index == 42
    assert result[1].source_spans[0].pdf_page_index == 42


def test_native_terminal_basis_waits_for_next_marker_before_closing() -> None:
    """Catches a sentence-ending basis line fabricating a native page boundary."""
    common = {
        "doc_id": "synthetic-native-terminal-continuation",
        "year": 2022,
        "source": "native",
        "upstream_review_status": "machine_extracted",
    }
    first = _boundary_parser_page(
        **common,
        pdf_page_index=1,
        page_role_hint="body",
        lines=[
            {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
            {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
            {"text": "사례 번호: 1", "bbox": [70.0, 130.0, 180.0, 155.0]},
            {"text": "제목: 합성 첫 제목", "bbox": [195.0, 130.0, 525.0, 155.0]},
            {
                "text": "질문1: 합성 첫 질문",
                "bbox": [121.0, 190.0, 525.0, 220.0],
            },
            {
                "text": "답변1: 합성 첫 답변",
                "bbox": [78.0, 240.0, 525.0, 270.0],
            },
            {
                "text": "참고자료: 합성 첫 근거.",
                "bbox": [80.0, 310.0, 525.0, 345.0],
            },
        ],
    )
    second = _boundary_parser_page(
        **common,
        pdf_page_index=2,
        page_role_hint="body",
        lines=[
            {
                "text": "참고자료: 합성 다음 페이지 근거.",
                "bbox": [80.0, 70.0, 525.0, 105.0],
            },
            {"text": "사례 번호: 2", "bbox": [70.0, 150.0, 180.0, 175.0]},
            {"text": "제목: 합성 둘째 제목", "bbox": [195.0, 150.0, 525.0, 175.0]},
            {
                "text": "질문1: 합성 둘째 질문",
                "bbox": [121.0, 210.0, 525.0, 240.0],
            },
            {
                "text": "답변1: 합성 둘째 답변",
                "bbox": [78.0, 260.0, 525.0, 290.0],
            },
            {
                "text": "참고자료: 합성 둘째 근거.",
                "bbox": [80.0, 330.0, 525.0, 365.0],
            },
        ],
    )

    result = parse_pages((first, second), edition_year=2022)

    assert [case.case_no for case in result] == ["1", "2"]
    assert result[0].basis_text == "합성 첫 근거.\n합성 다음 페이지 근거."
    assert any(span.pdf_page_index == 2 for span in result[0].source_spans)


def test_parse_result_is_an_explicit_sequence_without_hiding_quarantines() -> None:
    """Catches sequence compatibility dropping explicit review/quarantine metadata."""
    result = parse_pages(load_golden(2022, "first-case"), edition_year=2022)

    assert tuple(result) == result.cases
    assert result[0] is result.cases[0]
    assert result.quarantines == ()
    assert result.transitions


def test_overlapping_case_markers_fail_closed_without_body_text_in_quarantine() -> None:
    """Catches an OCR bleed/split ambiguity being auto-merged or leaking source text."""
    raw_page = _raw_page(
        doc_id="synthetic-ambiguous",
        year=2023,
        source="ocr",
        page={
            "pdf_page_index": 7,
            "page_label": "7",
            "lines": [
                {
                    "text": "대분류: 계약",
                    "bbox": [70.0, 50.0, 300.0, 75.0],
                    "confidence": 0.96,
                },
                {
                    "text": "편: 계약 일반",
                    "bbox": [250.0, 80.0, 500.0, 105.0],
                    "confidence": 0.96,
                },
                {
                    "text": "사례 번호: 1",
                    "bbox": [70.0, 120.0, 190.0, 150.0],
                    "confidence": 0.96,
                },
                {
                    "text": "제목: 합성 경계 제목",
                    "bbox": [195.0, 120.0, 530.0, 150.0],
                    "confidence": 0.96,
                    "semantic_hint": "title",
                },
                {
                    "text": "답변: 합성 경계 답변",
                    "bbox": [70.0, 250.0, 530.0, 330.0],
                    "confidence": 0.96,
                },
                {
                    "text": "사례 번호: 2",
                    "bbox": [70.0, 280.0, 190.0, 315.0],
                    "confidence": 0.96,
                },
                {
                    "text": "제목: 합성 겹침 제목",
                    "bbox": [195.0, 340.0, 530.0, 370.0],
                    "confidence": 0.96,
                    "semantic_hint": "title",
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="needs_review",
        critical_review_policy="all-fields-human-verification",
    )

    result = parse_pages((page,), edition_year=2023)

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == [
        "ambiguous_boundary",
    ]
    rendered = canonical_result_bytes(result).decode("utf-8")
    assert "합성 경계" not in rendered
    assert "합성 겹침" not in rendered


def test_quarantined_page_breaks_continuation_instead_of_fabricating_a_gap() -> None:
    """Catches a case silently continuing across an upstream-quarantined page."""
    pages = list(load_golden(2022, "continuation"))
    gap = ParserPage(
        doc_id=pages[0].doc_id,
        edition_year=2022,
        extraction_source="native",
        source_sha256=pages[0].source_sha256,
        pdf_page_index=42,
        page_label="42",
        page_width=600.0,
        page_height=800.0,
        render_sha256="f" * 64,
        lines=(),
        page_status="quarantined",
        page_role_hint="body",
        quality_flags=(),
        upstream_review_status="needs_review",
        critical_review_policy="not_applicable",
        critical_fields=(),
        layout_evidence=LayoutEvidence(),
        layout_segment_provenance=None,
        upstream_reason_code="page-extraction-failed",
    )

    result = parse_pages((pages[0], gap), edition_year=2022)

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == [
        "ambiguous_boundary",
        "page-extraction-failed",
    ]


def test_input_must_be_one_monotonic_document_and_error_is_value_free() -> None:
    """Catches mixed, duplicate, or non-monotonic envelopes yielding unstable bytes."""
    pages = load_golden(2022, "continuation")
    with pytest.raises(ParserContractError, match="monotonic") as error:
        parse_pages(tuple(reversed(pages)), edition_year=2022)
    assert pages[0].doc_id not in str(error.value)

    changed = pages[1].model_copy(update={"doc_id": "synthetic-other"})
    with pytest.raises(ParserContractError, match="single document") as error:
        parse_pages((pages[0], changed), edition_year=2022)
    assert "synthetic-other" not in str(error.value)


def test_raw_bbox_indexes_and_exact_text_hash_are_preserved() -> None:
    """Catches a parser hashing normalized text or losing raw coordinate provenance."""
    pages = load_golden(2020, "first-case")
    source_line = next(line for line in pages[0].lines if line.raw_text == "1")
    result = parse_pages(pages, edition_year=2020)

    assert source_line.source_block_index == 3
    assert source_line.source_line_index == 0
    assert source_line.source_span_index == 0
    assert (
        source_line.raw_text_sha256
        == hashlib.sha256(source_line.raw_text.encode("utf-8")).hexdigest()
    )
    assert result[0].source_spans[0].bbox == source_line.bbox
    assert result[0].source_spans[0].text_sha256 == source_line.raw_text_sha256


def test_result_bytes_are_stable_across_identical_runs() -> None:
    """Catches set/dict iteration changing candidate or transition output order."""
    pages = load_golden(2025, "continuation")
    first = canonical_result_bytes(parse_pages(pages, edition_year=2025))
    second = canonical_result_bytes(parse_pages(pages, edition_year=2025))
    assert first == second


def test_numeric_page_gap_requires_an_explicit_quarantined_page() -> None:
    """Catches continuation crossing an unexplained missing PDF page."""
    pages = load_golden(2022, "continuation")
    skipped = pages[1].model_copy(update={"pdf_page_index": 43, "page_label": "43"})
    with pytest.raises(ParserContractError, match="contiguous"):
        parse_pages((pages[0], skipped), edition_year=2022)


def test_zero_area_raw_bbox_is_rejected_before_source_span_creation() -> None:
    """Catches a zero-area upstream line becoming an unlocatable citation."""
    raw_page = _raw_page(
        doc_id="synthetic-zero-area",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {
                    "text": "번호 상자: 1",
                    "bbox": [70.0, 100.0, 70.0, 120.0],
                    "confidence": 1.0,
                }
            ],
        },
    )
    with pytest.raises(ParserContractError, match="positive area"):
        parser_page_from_raw_page(
            raw_page,
            normalized_text="번호 상자: 1",
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )


def test_ocr_candidate_confidence_is_minimum_contributing_line() -> None:
    """Catches averaging away one low-confidence OCR role fragment."""
    raw_page = _raw_page(
        doc_id="synthetic-confidence",
        year=2023,
        source="ocr",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {
                    "text": "대분류: 계약",
                    "bbox": [70.0, 50.0, 300.0, 75.0],
                    "confidence": 0.91,
                },
                {
                    "text": "편: 계약 일반",
                    "bbox": [250.0, 80.0, 500.0, 105.0],
                    "confidence": 0.91,
                },
                {
                    "text": "사례 번호: 1",
                    "bbox": [70.0, 120.0, 190.0, 150.0],
                    "confidence": 0.91,
                },
                {
                    "text": "제목: 합성 신뢰도 제목",
                    "bbox": [195.0, 120.0, 530.0, 150.0],
                    "confidence": 0.91,
                    "semantic_hint": "title",
                },
                {
                    "text": "질문: 합성 신뢰도 질문?",
                    "bbox": [70.0, 180.0, 530.0, 210.0],
                    "confidence": 0.91,
                    "semantic_hint": "question",
                },
                {
                    "text": "답변: 합성 신뢰도 답변입니다.",
                    "bbox": [70.0, 240.0, 530.0, 280.0],
                    "confidence": 0.62,
                },
                {
                    "text": "근거: 합성 신뢰도 근거입니다.",
                    "bbox": [70.0, 310.0, 530.0, 350.0],
                    "confidence": 0.85,
                    "semantic_hint": "law_name",
                },
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="needs_review",
        critical_review_policy="all-fields-human-verification",
    )
    result = parse_pages((page,), edition_year=2023)
    assert result[0].extraction_confidence == 0.62


@pytest.mark.parametrize(
    "roles",
    [
        "질문1: 합성 짝 질문?\n답변2: 합성 짝 답변입니다.",
        "질문1: 합성 첫 질문?\n질문1: 합성 중복 질문?\n답변1: 합성 답변입니다.",
        "답변1: 합성 선행 답변입니다.\n질문1: 합성 후행 질문?",
    ],
)
def test_numbered_role_pairing_ambiguity_is_quarantined(roles: str) -> None:
    """Catches missing, duplicate, mismatched, or reversed numbered role pairs."""
    raw_page = _raw_page(
        doc_id="synthetic-role-pair",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {
                    "text": "대분류: 계약",
                    "bbox": [80.0, 50.0, 320.0, 75.0],
                    "confidence": 1.0,
                },
                {
                    "text": "편: 계약 일반",
                    "bbox": [250.0, 80.0, 500.0, 105.0],
                    "confidence": 1.0,
                },
                {
                    "text": "1",
                    "bbox": [88.0, 128.0, 103.0, 148.0],
                    "confidence": 1.0,
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 짝 제목",
                    "bbox": [121.0, 121.0, 516.0, 148.0],
                    "confidence": 1.0,
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": roles.split("\n")[0],
                    "bbox": [
                        121.0 if roles.split("\n")[0].startswith("질문") else 78.0,
                        190.0,
                        525.0,
                        220.0,
                    ],
                    "confidence": 1.0,
                },
                *[
                    {
                        "text": text,
                        "bbox": [
                            121.0 if text.startswith("질문") else 78.0,
                            235.0 + offset * 45.0,
                            525.0,
                            265.0 + offset * 45.0,
                        ],
                        "confidence": 1.0,
                    }
                    for offset, text in enumerate(roles.split("\n")[1:])
                ],
                {
                    "text": "참고자료: 합성 짝 참고입니다.",
                    "bbox": [80.0, 400.0, 525.0, 435.0],
                    "confidence": 1.0,
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


def test_case_marker_text_inside_answer_body_does_not_open_a_case() -> None:
    """Catches marker-like prose inside an answer becoming a false case start."""
    raw_page = _raw_page(
        doc_id="synthetic-marker-spoof",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {
                    "text": "대분류: 계약",
                    "bbox": [80.0, 50.0, 320.0, 75.0],
                    "confidence": 1.0,
                },
                {
                    "text": "편: 계약 일반",
                    "bbox": [250.0, 80.0, 500.0, 105.0],
                    "confidence": 1.0,
                },
                {
                    "text": "1",
                    "bbox": [88.0, 128.0, 103.0, 148.0],
                    "confidence": 1.0,
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 표식 제목",
                    "bbox": [121.0, 121.0, 516.0, 148.0],
                    "confidence": 1.0,
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "질문1: 합성 표식 질문?",
                    "bbox": [121.0, 190.0, 525.0, 220.0],
                    "confidence": 1.0,
                },
                {
                    "text": "답변1: 본문 안의 번호 상자: 9 표시는 합성 예시입니다.",
                    "bbox": [80.0, 240.0, 525.0, 280.0],
                    "confidence": 1.0,
                },
                {
                    "text": "참고자료: 합성 표식 참고입니다.",
                    "bbox": [80.0, 310.0, 525.0, 345.0],
                    "confidence": 1.0,
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
    assert result[0].case_no == "1"


def test_eof_unfinished_open_case_is_quarantined() -> None:
    """Catches EOF implicitly closing a case without a next marker or confirmed border."""
    raw_page = _raw_page(
        doc_id="synthetic-unfinished",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {
                    "text": "1",
                    "bbox": [88.0, 158.0, 103.0, 178.0],
                    "font": "",
                    "size": 16.0,
                },
                {
                    "text": "합성 미완성 제목",
                    "bbox": [121.0, 151.0, 516.0, 178.0],
                    "font": "",
                    "size": 11.0,
                },
                {
                    "text": "질문1 합성 미완성 질문?",
                    "bbox": [80.0, 210.0, 525.0, 240.0],
                },
                {
                    "text": "답변1 합성 미완성 답변",
                    "bbox": [80.0, 255.0, 525.0, 290.0],
                },
            ],
        },
    )
    unfinished = parser_page_from_raw_page(
        raw_page,
        normalized_text="synthetic projection",
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )
    result = parse_pages((unfinished,), edition_year=2022)
    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


@pytest.mark.parametrize("with_credits", [False, True])
def test_complete_last_case_finalizes_at_verified_document_end(
    with_credits: bool,
) -> None:
    """Catches EOF or reviewed credits turning a complete final case into ambiguity."""
    common = {
        "doc_id": "synthetic-complete-document-end",
        "year": 2022,
        "source": "native",
        "upstream_review_status": "machine_extracted",
    }
    body = _boundary_parser_page(
        **common,
        pdf_page_index=1,
        page_role_hint="body",
        lines=[
            {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
            {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
            {"text": "사례 번호: 1", "bbox": [70.0, 130.0, 180.0, 155.0]},
            {"text": "제목: 합성 마지막 제목", "bbox": [195.0, 130.0, 525.0, 155.0]},
            {
                "text": "질문1: 합성 마지막 질문",
                "bbox": [121.0, 190.0, 525.0, 220.0],
            },
            {
                "text": "답변1: 합성 마지막 답변",
                "bbox": [78.0, 240.0, 525.0, 270.0],
            },
        ],
    )
    pages = [body]
    if with_credits:
        pages.append(
            _boundary_parser_page(
                **common,
                pdf_page_index=2,
                page_role_hint="credits",
                lines=[
                    {
                        "text": "합성 제작 정보",
                        "bbox": [80.0, 100.0, 525.0, 140.0],
                    }
                ],
            )
        )

    result = parse_pages(tuple(pages), edition_year=2022)

    assert [case.case_no for case in result] == ["1"]
    assert result.quarantines == ()


@pytest.mark.parametrize("with_credits", [False, True])
def test_incomplete_last_case_stays_quarantined_at_document_end(
    with_credits: bool,
) -> None:
    """Catches document-end finalization promoting a case without an answer."""
    common = {
        "doc_id": "synthetic-incomplete-document-end",
        "year": 2022,
        "source": "native",
        "upstream_review_status": "machine_extracted",
    }
    body = _boundary_parser_page(
        **common,
        pdf_page_index=1,
        page_role_hint="body",
        lines=[
            {"text": "대분류: 합성 업무", "bbox": [80.0, 45.0, 300.0, 70.0]},
            {"text": "편: 합성 운영", "bbox": [250.0, 85.0, 500.0, 110.0]},
            {"text": "사례 번호: 1", "bbox": [70.0, 130.0, 180.0, 155.0]},
            {"text": "제목: 합성 미완성 제목", "bbox": [195.0, 130.0, 525.0, 155.0]},
            {
                "text": "질문1: 합성 미완성 질문",
                "bbox": [121.0, 190.0, 525.0, 220.0],
            },
        ],
    )
    pages = [body]
    if with_credits:
        pages.append(
            _boundary_parser_page(
                **common,
                pdf_page_index=2,
                page_role_hint="credits",
                lines=[
                    {
                        "text": "합성 제작 정보",
                        "bbox": [80.0, 100.0, 525.0, 140.0],
                    }
                ],
            )
        )

    result = parse_pages(tuple(pages), edition_year=2022)

    assert result.cases == ()
    assert [item.reason_code for item in result.quarantines] == ["ambiguous_boundary"]


def test_one_raw_span_is_never_assigned_to_two_cases() -> None:
    """Catches a next-marker line being retained by both adjacent candidates."""
    result = parse_pages(load_golden(2022, "continuation"), edition_year=2022)
    keys = [
        {
            (
                span.pdf_page_index,
                span.bbox,
                span.text_sha256,
            )
            for span in candidate.source_spans
        }
        for candidate in result.cases
    ]
    assert keys[0].isdisjoint(keys[1])


def test_boundary_precision_recall_and_f1_are_one_on_all_golden_pairs() -> None:
    """Catches matching case counts that still have wrong start/end source boundaries."""
    true_positive = false_positive = false_negative = 0
    for year in range(2020, 2026):
        for name in GOLDEN_NAMES:
            result = parse_pages(load_golden(year, name), edition_year=year)
            expected = json.loads(
                (EXPECTED_ROOT / str(year) / f"{name}.expected.json").read_text(
                    encoding="utf-8"
                )
            )
            actual_boundaries = {
                (
                    case.source_spans[0].pdf_page_index,
                    case.source_spans[0].bbox,
                    case.source_spans[-1].pdf_page_index,
                    case.source_spans[-1].bbox,
                )
                for case in result.cases
            }
            expected_boundaries = {
                (
                    case["source_spans"][0]["pdf_page_index"],
                    tuple(case["source_spans"][0]["bbox"]),
                    case["source_spans"][-1]["pdf_page_index"],
                    tuple(case["source_spans"][-1]["bbox"]),
                )
                for case in expected["cases"]
            }
            true_positive += len(actual_boundaries & expected_boundaries)
            false_positive += len(actual_boundaries - expected_boundaries)
            false_negative += len(expected_boundaries - actual_boundaries)
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)
    f1 = 2 * precision * recall / (precision + recall)
    assert (precision, recall, f1) == (1.0, 1.0, 1.0)


def test_unclassified_manifest_page_cannot_fall_back_to_body() -> None:
    """Catches an unclassified front-matter page parsing case-like text as body."""
    raw_page = _raw_page(
        doc_id="synthetic-unclassified",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 3,
            "page_label": None,
            "lines": [
                {
                    "text": "1",
                    "bbox": [76.0, 120.0, 110.0, 165.0],
                    "font": "SyntheticNumber",
                    "size": 35.0,
                },
                {
                    "text": "합성 비본문 제목",
                    "bbox": [111.0, 120.0, 516.0, 155.0],
                    "font": "SyntheticTitle",
                    "size": 16.0,
                },
            ],
        },
    )
    policy = VerifiedPageRolePolicy(
        doc_id="synthetic-unclassified",
        edition_year=2022,
        extraction_source="native",
        source_sha256="a" * 64,
        pdf_page_count=12,
        body_start_pdf_page=7,
        body_end_pdf_page=10,
        cover_page_indexes=(1,),
        toc_page_indexes=(2,),
        credits_page_indexes=(12,),
    )
    with pytest.raises(ParserContractError, match="unclassified"):
        parser_page_from_raw_page(
            raw_page,
            normalized_text="synthetic projection",
            page_role_policy=policy,
            source_sha256="a" * 64,
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )


@pytest.mark.parametrize("source", ["native", "ocr"])
def test_upstream_quarantined_record_is_preserved_as_standalone_gap(
    source: str,
) -> None:
    """Catches extractor quarantine reason/count disappearing before parsing."""
    if source == "native":
        record = QuarantinedPageRecord(
            schema_version=2,
            doc_id="synthetic-native-gap",
            edition_year=2022,
            source_sha256="b" * 64,
            document_pdf_page_count=12,
            pdf_page_index=8,
            page_label="8",
            reason_code="page-extraction-failed",
        )
        policy = VerifiedPageRolePolicy(
            doc_id="synthetic-native-gap",
            edition_year=2022,
            extraction_source="native",
            source_sha256="b" * 64,
            pdf_page_count=12,
            body_start_pdf_page=7,
            body_end_pdf_page=10,
            cover_page_indexes=(1,),
            toc_page_indexes=(2, 3, 4, 5, 6),
            credits_page_indexes=(12,),
        )
        page = parser_page_from_native_record(record, page_role_policy=policy)
        year = 2022
        expected_reason = "page-extraction-failed"
    else:
        record = QuarantinedOcrPageRecord(
            schema_version=2,
            doc_id="synthetic-ocr-gap",
            edition_year=2025,
            pdf_page_index=8,
            page_label="8",
            source_sha256="a" * 64,
            render_sha256="c" * 64,
            render_dpi=300,
            image_digest="sha256:" + "b" * 64,
            quality_flags=(),
            reason_code="ocr-adapter-failed",
        )
        policy = VerifiedPageRolePolicy(
            doc_id="synthetic-ocr-gap",
            edition_year=2025,
            extraction_source="ocr",
            source_sha256="a" * 64,
            pdf_page_count=12,
            body_start_pdf_page=7,
            body_end_pdf_page=10,
            cover_page_indexes=(1,),
            toc_page_indexes=(2, 3, 4, 5, 6),
            credits_page_indexes=(12,),
        )
        page = parser_page_from_ocr_record(record, page_role_policy=policy)
        year = 2025
        expected_reason = "ocr-adapter-failed"
    result = parse_pages((page,), edition_year=year)
    assert result.cases == ()
    assert [
        (item.reason_code, item.page_ids, item.span_count)
        for item in result.quarantines
    ] == [(expected_reason, (8,), 0)]


def test_adapter_validation_error_has_no_source_bearing_cause_or_context() -> None:
    """Catches Pydantic include_input data remaining reachable through exception chaining."""
    sentinel = "SYNTHETIC-SOURCE-SENTINEL"
    raw_page = _raw_page(
        doc_id="synthetic-sanitized-error",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 8,
            "page_label": "8",
            "lines": [
                {
                    "text": sentinel,
                    "bbox": [70.0, 100.0, 70.0, 130.0],
                    "confidence": 1.0,
                }
            ],
        },
    )
    with pytest.raises(ParserContractError) as captured:
        parser_page_from_raw_page(
            raw_page,
            normalized_text="synthetic projection",
            page_role_hint="body",
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value)


@pytest.mark.parametrize(
    ("policy_update", "source_sha256"),
    [
        ({"doc_id": "synthetic-other"}, "a" * 64),
        ({"edition_year": 2021}, "a" * 64),
        ({"extraction_source": "ocr"}, "a" * 64),
        ({}, "b" * 64),
        ({"body_start_pdf_page": 9}, "a" * 64),
        (
            {
                "pdf_page_count": 7,
                "body_end_pdf_page": 7,
                "credits_page_indexes": (),
            },
            "a" * 64,
        ),
    ],
)
def test_bound_page_role_policy_rejects_document_and_manifest_replay(
    policy_update: dict[str, object], source_sha256: str
) -> None:
    """Catches a role policy replayed across a document, year, source, or bounds."""
    raw_page = _raw_page(
        doc_id="synthetic-bound-policy",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 8,
            "page_label": "8",
            "lines": [
                {
                    "text": "합성 계약 검증",
                    "bbox": [80.0, 100.0, 525.0, 130.0],
                }
            ],
        },
    )
    policy = VerifiedPageRolePolicy(
        doc_id="synthetic-bound-policy",
        edition_year=2022,
        extraction_source="native",
        source_sha256="a" * 64,
        pdf_page_count=12,
        body_start_pdf_page=7,
        body_end_pdf_page=10,
        cover_page_indexes=(1,),
        toc_page_indexes=(2, 3, 4, 5, 6),
        credits_page_indexes=(12,),
    ).model_copy(update=policy_update)

    with pytest.raises(ParserContractError) as captured:
        parser_page_from_raw_page(
            raw_page,
            normalized_text="synthetic projection",
            page_role_policy=policy,
            source_sha256=source_sha256,
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_page.doc_id not in str(captured.value)


@pytest.mark.parametrize("source", ["native", "ocr"])
def test_forged_extractor_record_is_revalidated_before_parser_access(
    source: str,
) -> None:
    """Catches model_construct bypasses entering adapters through direct attribute access."""
    sentinel = "SYNTHETIC-FORGED-RECORD"
    if source == "native":
        record = QuarantinedPageRecord.model_construct(
            schema_version=2,
            status="quarantined",
            doc_id=sentinel,
            edition_year=2022,
            source_sha256="a" * 64,
            document_pdf_page_count=12,
            pdf_page_index=0,
            page_label=None,
            reason_code="page-extraction-failed",
        )
        adapter = parser_page_from_native_record
    else:
        record = QuarantinedOcrPageRecord.model_construct(
            schema_version=2,
            status="quarantined",
            doc_id=sentinel,
            edition_year=2025,
            pdf_page_index=0,
            page_label=None,
            source_sha256="a" * 64,
            render_sha256="c" * 64,
            render_dpi=300,
            image_digest="sha256:" + "b" * 64,
            quality_flags=(),
            reason_code="ocr-adapter-failed",
        )
        adapter = parser_page_from_ocr_record
    with pytest.raises(ParserContractError) as captured:
        adapter(record)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value)


def _native_policy_record(
    *,
    status: str,
    source_sha256: str,
    document_pdf_page_count: int,
) -> ExtractedPageRecord | QuarantinedPageRecord:
    common = {
        "schema_version": 2,
        "doc_id": "synthetic-native-binding",
        "edition_year": 2022,
        "source_sha256": source_sha256,
        "document_pdf_page_count": document_pdf_page_count,
        "pdf_page_index": 8,
        "page_label": "8",
    }
    if status == "quarantined":
        return QuarantinedPageRecord(
            **common,
            reason_code="page-extraction-failed",
        )
    raw_page = _raw_page(
        doc_id="synthetic-native-binding",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 8,
            "page_label": "8",
            "lines": [
                {
                    "text": "합성 원문",
                    "bbox": [80.0, 100.0, 525.0, 130.0],
                }
            ],
        },
    )
    return ExtractedPageRecord(
        **common,
        raw_page=raw_page,
        normalized_text="합성 원문",
        retained_raw_block_indexes=(0,),
        removed_raw_block_evidence=(),
    )


def _native_binding_policy() -> VerifiedPageRolePolicy:
    return VerifiedPageRolePolicy(
        doc_id="synthetic-native-binding",
        edition_year=2022,
        extraction_source="native",
        source_sha256="a" * 64,
        pdf_page_count=12,
        body_start_pdf_page=7,
        body_end_pdf_page=10,
        cover_page_indexes=(1,),
        toc_page_indexes=(2, 3, 4, 5, 6),
        credits_page_indexes=(12,),
    )


def _native_binding_raw_page() -> RawPage:
    return _raw_page(
        doc_id="synthetic-native-binding",
        year=2022,
        source="native",
        page={
            "pdf_page_index": 8,
            "page_label": "8",
            "lines": [
                {
                    "text": "합성 직접 경계",
                    "bbox": [80.0, 100.0, 525.0, 130.0],
                }
            ],
        },
    )


def test_raw_page_policy_rejects_omitted_source_sha_value_free() -> None:
    """Catches a raw-page adapter synthesizing the manifest SHA for its caller."""
    raw_page = _native_binding_raw_page()
    with pytest.raises(ParserContractError, match="verified role policy") as captured:
        parser_page_from_raw_page(
            raw_page,
            normalized_text="합성 직접 경계",
            page_role_policy=_native_binding_policy(),
            upstream_review_status="machine_extracted",
            critical_review_policy="not_applicable",
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_page.doc_id not in str(captured.value)


def test_raw_page_policy_accepts_matching_explicit_source_sha() -> None:
    """Catches an exact caller-supplied SHA being rejected at the policy boundary."""
    page = parser_page_from_raw_page(
        _native_binding_raw_page(),
        normalized_text="합성 직접 경계",
        page_role_policy=_native_binding_policy(),
        source_sha256="a" * 64,
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
    )
    assert page.source_sha256 == "a" * 64
    assert page.page_role_hint == "body"


@pytest.mark.parametrize("status", ["extracted", "quarantined"])
@pytest.mark.parametrize(
    ("source_sha256", "document_pdf_page_count"),
    [("b" * 64, 12), ("a" * 64, 13)],
)
def test_native_record_manifest_binding_rejects_sha_or_page_count_mismatch(
    status: str, source_sha256: str, document_pdf_page_count: int
) -> None:
    """Catches valid native records replayed under a different manifest binding."""
    record = _native_policy_record(
        status=status,
        source_sha256=source_sha256,
        document_pdf_page_count=document_pdf_page_count,
    )
    with pytest.raises(ParserContractError, match="verified role policy"):
        parser_page_from_native_record(
            record,
            page_role_policy=_native_binding_policy(),
        )


@pytest.mark.parametrize("status", ["extracted", "quarantined"])
def test_native_record_manifest_binding_accepts_match_and_retains_sha(
    status: str,
) -> None:
    """Catches source SHA disappearing after a fully matching native adapter boundary."""
    record = _native_policy_record(
        status=status,
        source_sha256="a" * 64,
        document_pdf_page_count=12,
    )
    page = parser_page_from_native_record(
        record,
        page_role_policy=_native_binding_policy(),
    )
    assert page.source_sha256 == "a" * 64
    assert page.page_status == status


def test_parser_sequence_rejects_mixed_or_missing_source_sha() -> None:
    """Catches continuation joining pages from different or unbound source bytes."""
    pages = load_golden(2022, "continuation")
    assert len(parse_pages(pages, edition_year=2022)) == 2
    mixed = (
        pages[0].model_copy(update={"source_sha256": "a" * 64}),
        pages[1].model_copy(update={"source_sha256": "b" * 64}),
    )
    with pytest.raises(ParserContractError, match="source SHA"):
        parse_pages(mixed, edition_year=2022)
    missing = tuple(page.model_copy(update={"source_sha256": None}) for page in pages)
    with pytest.raises(ParserContractError, match="source SHA"):
        parse_pages(missing, edition_year=2022)
