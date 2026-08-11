from __future__ import annotations

import pytest

from src.ingestion.parse_common import (
    VerifiedParserAnnotation,
    VerifiedParserAnnotations,
    canonical_result_bytes,
    parse_pages,
    parse_pages_with_verified_annotations,
    parser_page_from_raw_page,
    verify_parser_annotations,
)
from tests.ingestion.test_page_continuation import _raw_page


def _unlabeled_hierarchy_page(*, reviewed_case_geometry: bool = True):
    raw_page = _raw_page(
        doc_id="synthetic-2023-reviewed-hierarchy",
        year=2023,
        source="ocr",
        page={
            "pdf_page_index": 1,
            "page_label": "1",
            "lines": [
                {"text": "합성 업무", "bbox": [80.0, 25.0, 300.0, 45.0]},
                {"text": "합성 운영", "bbox": [250.0, 65.0, 500.0, 85.0]},
                {
                    "text": "1",
                    "bbox": (
                        [90.0, 144.0, 105.0, 160.0]
                        if reviewed_case_geometry
                        else [20.0, 144.0, 35.0, 160.0]
                    ),
                },
                {"text": "제목: 합성 카드", "bbox": [195.0, 144.0, 525.0, 160.0]},
                {"text": "합성 질문?", "bbox": [195.0, 230.0, 525.0, 250.0]},
                {"text": "답변: 합성 답변", "bbox": [80.0, 285.0, 525.0, 310.0]},
            ],
        },
    )
    return parser_page_from_raw_page(
        raw_page,
        normalized_text=None,
        page_role_hint="body",
        source_sha256="a" * 64,
        upstream_review_status="needs_review",
        critical_review_policy="all-fields-human-verification",
    )


def test_verified_hierarchy_annotations_reparse_without_relaxing_default_parser() -> (
    None
):
    """Catches reviewed hierarchy being ignored or changing default parser bytes."""
    page = _unlabeled_hierarchy_page()
    default_before = parse_pages((page,), edition_year=2023)
    default_bytes = canonical_result_bytes(default_before)
    assert default_before.cases == ()
    assert len(default_before.quarantines) == 1
    domain, part = page.lines[:2]
    annotations = (
        VerifiedParserAnnotation(
            role="domain",
            pdf_page_index=1,
            bbox=domain.bbox,
            text_sha256=domain.raw_text_sha256,
        ),
        VerifiedParserAnnotation(
            role="part",
            pdf_page_index=1,
            bbox=part.bbox,
            text_sha256=part.raw_text_sha256,
        ),
    )

    verified = verify_parser_annotations(
        (page,), annotations=annotations, expected_source_sha256="a" * 64
    )
    result = parse_pages_with_verified_annotations(
        (page,), edition_year=2023, verified_annotations=verified
    )

    assert len(result.cases) == 1
    assert result.quarantines == ()
    assert (result.cases[0].domain, result.cases[0].part) == (
        "합성 업무",
        "합성 운영",
    )
    assert canonical_result_bytes(parse_pages((page,), edition_year=2023)) == (
        default_bytes
    )


def test_verified_annotation_wrapper_cannot_be_directly_initialized() -> None:
    """Catches an unverified wrapper bypassing exact page and source authority."""
    with pytest.raises(TypeError):
        VerifiedParserAnnotations()  # type: ignore[call-arg]


def test_annotation_verification_rejects_wrong_external_source_sha() -> None:
    """Catches a reviewed annotation being replayed onto another source."""
    page = _unlabeled_hierarchy_page()
    line = page.lines[0]
    annotation = VerifiedParserAnnotation(
        role="domain",
        pdf_page_index=1,
        bbox=line.bbox,
        text_sha256=line.raw_text_sha256,
    )

    with pytest.raises(ValueError, match="verified_parser_annotations_invalid"):
        verify_parser_annotations(
            (page,), annotations=(annotation,), expected_source_sha256="b" * 64
        )


def test_exact_case_and_fragment_roles_compile_without_geometry_relaxation() -> None:
    """Catches reviewed case roles being accepted but ignored by the parser."""
    page = _unlabeled_hierarchy_page(reviewed_case_geometry=False)
    assert parse_pages((page,), edition_year=2023).cases == ()
    roles = ("domain", "part", "case_no", "title", "question", "answer")
    annotations = tuple(
        VerifiedParserAnnotation(
            role=role,
            pdf_page_index=1,
            bbox=line.bbox,
            text_sha256=line.raw_text_sha256,
        )
        for role, line in zip(roles, page.lines, strict=True)
    )

    verified = verify_parser_annotations(
        (page,), annotations=annotations, expected_source_sha256="a" * 64
    )
    result = parse_pages_with_verified_annotations(
        (page,), edition_year=2023, verified_annotations=verified
    )

    assert len(result.cases) == 1
    assert result.quarantines == ()
    assert result.cases[0].case_no == "1"
    assert [fragment.role for fragment in result.cases[0].fragments] == [
        "title",
        "question",
        "answer",
    ]
