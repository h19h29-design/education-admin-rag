"""Privacy-safe integration metadata for strict annual parser inputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import traceback
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

import src.ingestion.parse_metadata as parse_metadata_module
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
from src.ingestion.extract_ocr import (
    CriticalFieldStatus,
    ExtractedOcrPageRecord,
    LayoutSegmentProvenance,
    QuarantinedOcrPageRecord,
)
from src.ingestion.manifest import SourceDocument, load_manifest
from src.ingestion.parse_metadata import (
    PageSetMetadata,
    ParseMetadataError,
    build_parse_metadata,
    canonical_metadata_bytes,
)


def _manifest_payload() -> dict[str, object]:
    documents: list[dict[str, object]] = []
    for year in range(2020, 2026):
        is_native = year <= 2022
        documents.append(
            {
                "doc_id": f"fixture-{year}",
                "edition_year": year,
                "official_title": "Synthetic fixture",
                "publisher": "Fixture",
                "registration_no": None,
                "source_period_start": None,
                "source_period_end": None,
                "source_filename": f"{year}.pdf",
                "source_relpath": f"{year}.pdf",
                "sha256": hashlib.sha256(f"source-{year}".encode()).hexdigest(),
                "pdf_page_count": 2,
                "page_size_profiles": [
                    {
                        "start_pdf_page": 1,
                        "end_pdf_page": 2,
                        "width_pt": 595.0,
                        "height_pt": 841.0,
                    }
                ],
                "extraction_method": "native" if is_native else "ocr",
                "source_dpi": None
                if is_native
                else {2023: 96, 2024: 150, 2025: 300}[year],
                "render_dpi": None
                if is_native
                else {2023: 300, 2024: 350, 2025: 300}[year],
                "page_numbering": {
                    "mode": "offset",
                    "body_start_pdf_page": 2,
                    "body_end_pdf_page": 2,
                    "offset": -1,
                },
                "official_public_url": None,
                "official_url_status": "unverified",
                "redistribution_status": "unverified",
                "access_level": "staff",
            }
        )
    return {"documents": documents}


def _write_manifest(tmp_path: Path) -> tuple[Path, tuple[SourceDocument, ...]]:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(_manifest_payload(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path, load_manifest(path)


def _write_jsonl(path: Path, records: tuple[object, ...]) -> bytes:
    rendered = b"".join(
        json.dumps(
            record.model_dump(mode="json"),  # type: ignore[attr-defined]
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for record in records
    )
    path.write_bytes(rendered)
    return rendered


def _native_quarantine_records(
    document: SourceDocument,
) -> tuple[QuarantinedPageRecord, ...]:
    return tuple(
        QuarantinedPageRecord(
            schema_version=2,
            doc_id=document.doc_id,
            edition_year=document.edition_year,
            source_sha256=document.sha256,
            document_pdf_page_count=document.pdf_page_count,
            pdf_page_index=index,
            page_label=None if index == 1 else "1",
            reason_code="page-extraction-failed",
        )
        for index in (1, 2)
    )


def _ocr_quarantine_record(
    document: SourceDocument, *, image_digest: str
) -> QuarantinedOcrPageRecord:
    assert document.render_dpi is not None
    return QuarantinedOcrPageRecord(
        schema_version=2,
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        pdf_page_index=2,
        page_label="1",
        source_sha256=document.sha256,
        render_sha256=None,
        render_dpi=document.render_dpi,
        image_digest=image_digest,
        quality_flags=(),
        reason_code="page-render-failed",
    )


def _approved_2025_layout_record(
    document: SourceDocument,
    *,
    render_sha256: str,
    image_digest: str,
    region_bbox: tuple[float, float, float, float] | None = None,
) -> ExtractedOcrPageRecord:
    registry_payload = {
        "detector_version": "green-card-border-v1",
        "doc_id": document.doc_id,
        "edition_year": 2025,
        "policy_version": "layout-segment-registry-v1",
        "sampling_status": "sampling_required",
        "segment_end_pdf_page": document.page_numbering.body_end_pdf_page,
        "segment_key": "approved-document-body",
        "segment_start_pdf_page": document.page_numbering.body_start_pdf_page,
        "source_sha256": document.sha256,
    }
    registry_sha256 = hashlib.sha256(
        b"sen-qa-layout-segment-registry-v1\0"
        + json.dumps(
            registry_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    regions = (
        (
            LayoutRegion(
                region_type="card",
                bbox=BoundingBox.from_tuple(region_bbox),
                evidence="raster-border",
            ),
        )
        if region_bbox is not None
        else ()
    )
    segment = LayoutSegmentProvenance(
        segment_id="layout-segment-"
        + hashlib.sha256(
            b"sen-qa-layout-segment-id-v1\0" + registry_sha256.encode("ascii")
        ).hexdigest()[:32],
        segment_key="approved-document-body",
        segment_start_pdf_page=document.page_numbering.body_start_pdf_page,
        segment_end_pdf_page=document.page_numbering.body_end_pdf_page,
        registry_policy_version="layout-segment-registry-v1",
        registry_sha256=registry_sha256,
        detector_version="green-card-border-v1",
        region_count=len(regions),
        sampling_status="sampling_required",
    )
    width = document.page_size_profiles[0].width_pt
    height = document.page_size_profiles[0].height_pt
    raw_page = RawPage(
        doc_id=document.doc_id,
        edition_year=2025,
        extraction_source="ocr",
        pdf_page_index=13,
        page_label="13",
        page_width=width,
        page_height=height,
        render_sha256=render_sha256,
        raw_blocks=(
            RawBlock(
                bbox=BoundingBox(x0=0.0, y0=0.0, x1=width, y1=height),
                lines=(),
            ),
        ),
        layout_evidence=LayoutEvidence(
            status="detected" if regions else "not_detected",
            detector_version="green-card-border-v1",
            regions=regions,
        ),
    )
    return ExtractedOcrPageRecord(
        schema_version=2,
        doc_id=document.doc_id,
        edition_year=2025,
        pdf_page_index=13,
        page_label="13",
        source_sha256=document.sha256,
        render_sha256=render_sha256,
        render_dpi=300,
        image_digest=image_digest,
        quality_flags=(),
        raw_page=raw_page,
        layout_segment_provenance=segment,
        review_queue=(),
        critical_review_policy="stratified-sample-with-layout-escalation",
        critical_fields=tuple(
            CriticalFieldStatus(
                field_type=field_type,
                status="sampling_required",
                review_required=bool(regions),
            )
            for field_type in (
                "title",
                "question",
                "amount",
                "date",
                "law_name",
                "article",
            )
        ),
        review_status="needs_review" if regions else "machine_extracted",
    )


def _native_case_record(
    document: SourceDocument, *, sentinel: str
) -> ExtractedPageRecord:
    values = (
        "대분류: 합성 계약",
        "편: 합성 운영",
        "사례 번호: 1",
        f"질문 제목: {sentinel}-TITLE",
        f"질문: {sentinel}-QUESTION",
        f"답변: {sentinel}-ANSWER.",
        f"관련 근거: {sentinel}-BASIS.",
    )
    blocks: list[RawBlock] = []
    for index, value in enumerate(values):
        y0 = 80.0 + index * 70.0
        bbox = BoundingBox(x0=80.0, y0=y0, x1=520.0, y1=y0 + 30.0)
        span = RawSpan(
            text=value,
            bbox=bbox,
            font="Fixture",
            size=12.0,
            confidence=1.0,
        )
        blocks.append(
            RawBlock(
                bbox=bbox,
                lines=(RawLine(bbox=bbox, spans=(span,), confidence=1.0),),
            )
        )
    raw_page = RawPage(
        doc_id=document.doc_id,
        edition_year=2020,
        extraction_source="native",
        pdf_page_index=2,
        page_label="1",
        page_width=595.0,
        page_height=841.0,
        render_sha256="a" * 64,
        raw_blocks=tuple(blocks),
    )
    return ExtractedPageRecord(
        schema_version=2,
        doc_id=document.doc_id,
        edition_year=2020,
        source_sha256=document.sha256,
        document_pdf_page_count=document.pdf_page_count,
        pdf_page_index=2,
        page_label="1",
        raw_page=raw_page,
        normalized_text="\n".join(values),
        retained_raw_block_indexes=tuple(range(len(blocks))),
        removed_raw_block_evidence=(),
    )


def test_native_full_document_metadata_is_deterministic_and_value_free(
    tmp_path: Path,
) -> None:
    """Catches nondeterministic output or a raw input/path value entering diagnostics."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[0]
    input_path = tmp_path / "PRIVATE-PATH.jsonl"
    input_bytes = _write_jsonl(input_path, _native_quarantine_records(document))

    first = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    second = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    rendered = canonical_metadata_bytes(first)

    assert first == second
    assert rendered == canonical_metadata_bytes(second)
    payload = json.loads(rendered)
    assert payload["metadata_schema"] == "sen-qa-parse-metadata-v1"
    assert (
        payload["doc_id"],
        payload["edition_year"],
        payload["extraction_source"],
    ) == (
        document.doc_id,
        2020,
        "native",
    )
    assert payload["page_set"] == {
        "count": 2,
        "first": 1,
        "last": 2,
        "sha256": hashlib.sha256(b"sen-qa-page-set-v1\0[1,2]").hexdigest(),
    }
    assert (
        payload["hashes"]["manifest_sha256"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
    assert payload["hashes"]["source_sha256"] == document.sha256
    assert payload["hashes"]["input_sha256"] == hashlib.sha256(input_bytes).hexdigest()
    assert len(payload["hashes"]["parse_sha256"]) == 64
    assert payload["record_counts"] == {
        "extracted": 0,
        "quarantined": 2,
        "total": 2,
    }
    assert payload["record_quarantine_reason_counts"] == {
        "ocr-adapter-failed": 0,
        "ocr-provenance-invalid": 0,
        "page-extraction-failed": 2,
        "page-render-failed": 0,
    }
    assert payload["case_type_counts"] == {"audit": 0, "qa": 0, "total": 0}
    assert payload["eligibility_counts"] == {
        "answer_eligible": 0,
        "answer_ineligible": 0,
        "search_eligible": 0,
        "search_ineligible": 0,
    }
    assert str(input_path) not in rendered.decode()
    assert "PRIVATE-PATH" not in rendered.decode()


def test_ocr_explicit_page_metadata_dispatches_and_stays_ineligible(
    tmp_path: Path,
) -> None:
    """Catches OCR input being routed through native parsing or acquiring eligibility."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[-1]
    image_digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(document, image_digest=image_digest),),
    )

    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="2",
        expected_image_digest=image_digest,
    )
    payload = json.loads(canonical_metadata_bytes(metadata))

    assert payload["extraction_source"] == "ocr"
    assert payload["page_set"]["count"] == 1
    assert payload["record_counts"] == {
        "extracted": 0,
        "quarantined": 1,
        "total": 1,
    }
    assert payload["parser_quarantine_reason_counts"]["page-render-failed"] == 1
    assert payload["layout_evidence_counts"]["no_evidence"] == 1
    assert payload["layout_sampling_counts"] == {
        "all_cases_required": 0,
        "no_segment": 1,
        "sampling_required": 0,
    }
    assert payload["eligibility_counts"]["search_eligible"] == 0
    assert payload["eligibility_counts"]["answer_eligible"] == 0

    try:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages="all",
            expected_image_digest=image_digest,
        )
    except ParseMetadataError as error:
        assert error.code == "selection_invalid"
    else:
        raise AssertionError("OCR all-page shorthand must fail closed")


@pytest.mark.parametrize("year", [2021, 2022])
def test_native_annual_metadata_uses_the_matching_parser(
    tmp_path: Path, year: int
) -> None:
    """Catches 2021/2022 records being sent through the 2020 annual contract."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[year - 2020]
    input_path = tmp_path / f"{year}.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(document))

    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=year,
        pages="all",
    )

    assert metadata.edition_year == year
    assert metadata.record_counts["quarantined"] == 2
    assert metadata.parser_quarantine_reason_counts["page-extraction-failed"] == 2


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("schema", "input_invalid"),
        ("source", "policy_mismatch"),
        ("extra", "input_invalid"),
        ("mixed", "input_invalid"),
    ],
)
def test_tampered_or_mixed_jsonl_fails_without_retaining_raw_values(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    """Catches schema/tamper rejection echoing a source sentinel through exceptions."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[0]
    payloads = [
        record.model_dump(mode="json")
        for record in _native_quarantine_records(document)
    ]
    sentinel = "PRIVATE-SOURCE-SENTINEL"
    if mutation == "schema":
        payloads[0]["schema_version"] = 1
        payloads[0]["private"] = sentinel
    elif mutation == "source":
        payloads[0]["source_sha256"] = "e" * 64
    elif mutation == "extra":
        payloads[0]["private"] = sentinel
    else:
        ocr = _ocr_quarantine_record(documents[-1], image_digest="sha256:" + "d" * 64)
        payloads[1] = ocr.model_dump(mode="json")
    input_path = tmp_path / f"{sentinel}.jsonl"
    input_path.write_bytes(
        b"".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
            for payload in payloads
        )
    )

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    error = captured.value
    assert error.code == expected_code
    assert error.__cause__ is None
    assert error.__context__ is None
    disclosed = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert sentinel not in disclosed
    assert str(input_path) not in disclosed


@pytest.mark.parametrize(
    ("pages", "digest", "expected_code"),
    [
        ("1", "sha256:" + "d" * 64, "policy_mismatch"),
        ("2,2", "sha256:" + "d" * 64, "selection_invalid"),
        ("all", "sha256:" + "d" * 64, "selection_invalid"),
        ("2", None, "image_digest_invalid"),
        ("2", "sha256:" + "e" * 64, "policy_mismatch"),
    ],
)
def test_ocr_page_and_image_digest_contracts_fail_closed(
    tmp_path: Path,
    pages: str,
    digest: str | None,
    expected_code: str,
) -> None:
    """Catches a different OCR page set or container image replaying valid records."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[-1]
    recorded_digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(document, image_digest=recorded_digest),),
    )

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages=pages,
            expected_image_digest=digest,
        )

    assert captured.value.code == expected_code
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_wrong_year_and_ocr_schema_are_rejected_before_parsing(tmp_path: Path) -> None:
    """Catches annual dispatch accepting another edition or an old page schema."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[-1]
    digest = "sha256:" + "d" * 64
    payload = _ocr_quarantine_record(document, image_digest=digest).model_dump(
        mode="json"
    )
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(document, image_digest=digest),),
    )

    with pytest.raises(ParseMetadataError) as wrong_year:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2024,
            pages="2",
            expected_image_digest=digest,
        )
    assert wrong_year.value.code == "policy_mismatch"

    payload["schema_version"] = 1
    input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ParseMetadataError) as old_schema:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages="2",
            expected_image_digest=digest,
        )
    assert old_schema.value.code == "input_invalid"


def test_approved_layout_sampling_is_counted_and_bound_to_the_render(
    tmp_path: Path,
) -> None:
    """Catches layout sampling metadata dropping its registry/render binding."""
    manifest_path = Path("data/manifests/sen_qa_sources.json")
    document = load_manifest(manifest_path)[-1]
    digest = "sha256:" + "d" * 64
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _write_jsonl(
        first_path,
        (
            _approved_2025_layout_record(
                document,
                render_sha256="a" * 64,
                image_digest=digest,
            ),
        ),
    )
    _write_jsonl(
        second_path,
        (
            _approved_2025_layout_record(
                document,
                render_sha256="b" * 64,
                image_digest=digest,
            ),
        ),
    )

    first = build_parse_metadata(
        first_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="13",
        expected_image_digest=digest,
    )
    second = build_parse_metadata(
        second_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="13",
        expected_image_digest=digest,
    )

    assert first.layout_evidence_counts["not_detected"] == 1
    assert first.layout_sampling_counts == {
        "all_cases_required": 0,
        "no_segment": 0,
        "sampling_required": 1,
    }
    assert first.layout_region_count == 0
    assert first.layout_binding_sha256 != second.layout_binding_sha256
    assert first.hashes.parse_sha256 == second.hashes.parse_sha256


def test_layout_binding_hash_covers_region_geometry_without_outputting_bbox(
    tmp_path: Path,
) -> None:
    """Catches two different detected card regions sharing one metadata binding."""
    manifest_path = Path("data/manifests/sen_qa_sources.json")
    document = load_manifest(manifest_path)[-1]
    digest = "sha256:" + "d" * 64
    render_sha256 = "a" * 64
    first_path = tmp_path / "first-region.jsonl"
    second_path = tmp_path / "second-region.jsonl"
    _write_jsonl(
        first_path,
        (
            _approved_2025_layout_record(
                document,
                render_sha256=render_sha256,
                image_digest=digest,
                region_bbox=(60.0, 200.0, 530.0, 390.0),
            ),
        ),
    )
    _write_jsonl(
        second_path,
        (
            _approved_2025_layout_record(
                document,
                render_sha256=render_sha256,
                image_digest=digest,
                region_bbox=(60.0, 210.0, 530.0, 400.0),
            ),
        ),
    )

    first = build_parse_metadata(
        first_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="13",
        expected_image_digest=digest,
    )
    second = build_parse_metadata(
        second_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="13",
        expected_image_digest=digest,
    )

    assert first.layout_evidence_counts["detected"] == 1
    assert first.layout_region_count == 1
    assert first.layout_binding_sha256 != second.layout_binding_sha256
    rendered = canonical_metadata_bytes(first).decode("ascii")
    assert "bbox" not in rendered
    assert "60.0" not in rendered


def test_case_role_review_and_transition_values_are_reduced_to_counts(
    tmp_path: Path,
) -> None:
    """Catches parsed case or hierarchy values leaking instead of aggregate counts."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[0]
    sentinel = "PRIVATE-CONTENT-SENTINEL"
    first_page = _native_quarantine_records(document)[0]
    input_path = tmp_path / "case.jsonl"
    _write_jsonl(
        input_path,
        (first_page, _native_case_record(document, sentinel=sentinel)),
    )

    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    rendered = canonical_metadata_bytes(metadata).decode("ascii")

    assert metadata.case_type_counts == {"audit": 0, "qa": 1, "total": 1}
    assert all(count == 0 for count in metadata.missing_required_role_counts.values())
    assert metadata.role_absence_counts == {
        "answer": 0,
        "basis": 0,
        "facts": 1,
        "question": 0,
        "situation": 1,
        "target": 1,
        "title": 0,
    }
    assert metadata.review_counts["critical_not_applicable"] == 1
    assert metadata.review_counts["review_machine_extracted"] == 1
    assert metadata.eligibility_counts == {
        "answer_eligible": 0,
        "answer_ineligible": 1,
        "search_eligible": 0,
        "search_ineligible": 1,
    }
    assert metadata.transition_role_counts["domain"] == 1
    assert metadata.transition_role_counts["part"] == 1
    assert sentinel not in rendered
    for value in ("합성 계약", "합성 운영", "TITLE", "QUESTION", "ANSWER", "BASIS"):
        assert value not in rendered


def test_canonical_renderer_revalidates_forged_counter_keys_value_free(
    tmp_path: Path,
) -> None:
    """Catches model-copy construction smuggling a source value into a count key."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    sentinel = "PRIVATE-COUNT-KEY"
    forged = metadata.model_copy(
        update={"record_counts": {sentinel: 2}},
    )

    with pytest.raises(ParseMetadataError) as captured:
        canonical_metadata_bytes(forged)

    assert captured.value.code == "input_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value) + repr(captured.value)


class _SplitBombSelection(str):
    def split(self, *args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise AssertionError("page tokenization occurred before the token bound")


def test_ocr_selection_rejects_too_many_tokens_before_split(tmp_path: Path) -> None:
    """Catches an attacker forcing unbounded page-token allocation."""
    manifest_path, documents = _write_manifest(tmp_path)
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(documents[-1], image_digest=digest),),
    )

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages=_SplitBombSelection("1,1,1"),
            expected_image_digest=digest,
        )

    assert captured.value.code == "selection_invalid"


@pytest.mark.parametrize("pages", ["9" * 5_000, "1-" + "9" * 5_000])
def test_ocr_selection_rejects_overlong_endpoint_before_integer_conversion(
    tmp_path: Path, pages: str
) -> None:
    """Catches huge decimal endpoints escaping the fixed selection error boundary."""
    manifest_path, documents = _write_manifest(tmp_path)
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(documents[-1], image_digest=digest),),
    )

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages=pages,
            expected_image_digest=digest,
        )

    assert captured.value.code == "selection_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_ocr_selection_rejects_endpoint_before_materializing_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an out-of-document range being expanded before endpoint validation."""
    manifest_path, documents = _write_manifest(tmp_path)
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(documents[-1], image_digest=digest),),
    )

    def range_bomb(*args: object) -> range:
        del args
        raise AssertionError("page range was materialized before bounds validation")

    monkeypatch.setattr(parse_metadata_module, "range", range_bomb, raising=False)
    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages="1-1,2-3",
            expected_image_digest=digest,
        )

    assert captured.value.code == "selection_invalid"


def test_tiny_manifest_rejects_huge_page_count_before_policy_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a tiny manifest driving an eager full-document role tuple."""
    payload = _manifest_payload()
    documents = payload["documents"]
    assert isinstance(documents, list)
    selected = documents[-1]
    assert isinstance(selected, dict)
    record_document = SourceDocument.model_validate_json(json.dumps(selected))
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(record_document, image_digest=digest),),
    )
    rejected_count = 50_000_000
    selected["pdf_page_count"] = rejected_count
    profiles = selected["page_size_profiles"]
    assert isinstance(profiles, list)
    profile = profiles[0]
    assert isinstance(profile, dict)
    profile["end_pdf_page"] = rejected_count
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    builtin_range = range

    def bounded_range(*args: int) -> range:
        candidate = builtin_range(*args)
        if len(candidate) > 10_000:
            raise AssertionError("full-document page policy was allocated")
        return candidate

    monkeypatch.setattr(parse_metadata_module, "range", bounded_range, raising=False)
    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2025,
            pages="2",
            expected_image_digest=digest,
        )

    error = captured.value
    assert error.code == "manifest_invalid"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(rejected_count) not in str(error) + repr(error)


def test_forged_manifest_document_is_revalidated_before_policy_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches model_construct bypassing the metadata manifest handoff."""
    payload = _manifest_payload()
    documents = payload["documents"]
    assert isinstance(documents, list)
    selected = documents[-1]
    assert isinstance(selected, dict)
    document = SourceDocument.model_validate_json(json.dumps(selected))
    digest = "sha256:" + "d" * 64
    input_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        input_path,
        (_ocr_quarantine_record(document, image_digest=digest),),
    )
    rejected_count = 50_000_000
    oversized_profile = type(document.page_size_profiles[0]).model_validate(
        {
            **document.page_size_profiles[0].model_dump(),
            "end_pdf_page": rejected_count,
        }
    )
    forged = SourceDocument.model_construct(
        **{
            **document.__dict__,
            "pdf_page_count": rejected_count,
            "page_size_profiles": (oversized_profile,),
        }
    )

    def forged_manifest(path: Path, edition_year: int) -> tuple[SourceDocument, bytes]:
        del path, edition_year
        return forged, b"{}"

    builtin_range = range

    def bounded_range(*args: int) -> range:
        candidate = builtin_range(*args)
        if len(candidate) > 10_000:
            raise AssertionError("forged full-document page policy was allocated")
        return candidate

    monkeypatch.setattr(parse_metadata_module, "_manifest", forged_manifest)
    monkeypatch.setattr(parse_metadata_module, "range", bounded_range, raising=False)
    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=tmp_path / "manifest.json",
            edition_year=2025,
            pages="2",
            expected_image_digest=digest,
        )

    error = captured.value
    assert error.code == "manifest_invalid"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(rejected_count) not in str(error) + repr(error)


@pytest.mark.parametrize("failure_type", ["deep", "overflow"])
def test_json_resource_errors_are_fixed_and_cause_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: str,
) -> None:
    """Catches JSON depth/overflow exceptions escaping with parser internals."""
    manifest_path, _ = _write_manifest(tmp_path)
    input_path = tmp_path / "input.jsonl"
    if failure_type == "deep":
        input_path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n")
    else:
        input_path.write_text('{"status":"quarantined"}\n', encoding="utf-8")
        original_loads = json.loads

        def overflow_on_record(
            value: object, *args: object, **kwargs: object
        ) -> object:
            if isinstance(value, (bytes, bytearray)) and b'"documents"' not in value:
                raise OverflowError
            return original_loads(value, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(json, "loads", overflow_on_record)

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == "input_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_metadata_validation_error_is_fixed_and_cause_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches aggregate Pydantic failures retaining internal or source values."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    sentinel = "PRIVATE-METADATA-VALIDATION"

    def invalid_metadata(**kwargs: object) -> None:
        del kwargs
        raise ValueError(sentinel)

    monkeypatch.setattr(parse_metadata_module, "_metadata", invalid_metadata)
    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == "parse_failed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value) + repr(captured.value)


def test_direct_metadata_validation_hides_input_value_everywhere() -> None:
    """Catches Pydantic retaining an invalid metadata value in public diagnostics."""
    sentinel = "PRIVATE-DIRECT-PYDANTIC-INPUT"

    with pytest.raises(ValidationError) as captured:
        PageSetMetadata.model_validate(
            {
                "count": sentinel,
                "first": 1,
                "last": 1,
                "sha256": "a" * 64,
            }
        )

    error = captured.value
    assert error.__cause__ is None
    assert error.__context__ is None
    disclosed = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert sentinel not in disclosed


def test_bounded_reader_limits_are_fixed() -> None:
    """Catches weakening a byte boundary while preserving the helper tests."""
    assert parse_metadata_module._MANIFEST_MAX_BYTES == 1024 * 1024
    assert parse_metadata_module._INPUT_JSONL_MAX_BYTES == 512 * 1024 * 1024
    assert parse_metadata_module._JSONL_RECORD_MAX_BYTES == 8 * 1024 * 1024


def test_manifest_and_input_total_exact_limit_pass_then_plus_one_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches off-by-one errors in both regular-file total-byte limits."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    input_bytes = _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    manifest_size = len(manifest_path.read_bytes())

    monkeypatch.setattr(parse_metadata_module, "_MANIFEST_MAX_BYTES", manifest_size)
    monkeypatch.setattr(
        parse_metadata_module, "_INPUT_JSONL_MAX_BYTES", len(input_bytes)
    )
    assert (
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        ).record_counts["total"]
        == 2
    )

    monkeypatch.setattr(
        parse_metadata_module,
        "_MANIFEST_MAX_BYTES",
        manifest_size - 1,
    )
    with pytest.raises(ParseMetadataError) as manifest_error:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )
    assert manifest_error.value.code == "manifest_invalid"

    monkeypatch.setattr(parse_metadata_module, "_MANIFEST_MAX_BYTES", manifest_size)
    monkeypatch.setattr(
        parse_metadata_module,
        "_INPUT_JSONL_MAX_BYTES",
        len(input_bytes) - 1,
    )
    with pytest.raises(ParseMetadataError) as input_error:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )
    assert input_error.value.code == "input_invalid"


def test_jsonl_record_exact_limit_passes_and_one_extra_byte_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches parsing an individually oversized record under the total limit."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "PRIVATE-OVERSIZED-RECORD.jsonl"
    input_bytes = _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    lines = input_bytes[:-1].split(b"\n")
    record_limit = max(map(len, lines))
    monkeypatch.setattr(parse_metadata_module, "_JSONL_RECORD_MAX_BYTES", record_limit)

    assert (
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        ).record_counts["total"]
        == 2
    )

    oversized_index = max(range(len(lines)), key=lambda index: len(lines[index]))
    lines[oversized_index] += b" "
    input_path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == "input_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "PRIVATE-OVERSIZED-RECORD" not in str(captured.value) + repr(captured.value)


@pytest.mark.parametrize("fifo_role", ["manifest", "input"])
def test_fifo_fails_quickly_without_opening_a_blocking_reader(
    tmp_path: Path,
    fifo_role: str,
) -> None:
    """Catches a named pipe blocking metadata validation indefinitely."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    fifo_path = tmp_path / "metadata.fifo"
    os.mkfifo(fifo_path)
    selected_manifest = fifo_path if fifo_role == "manifest" else manifest_path
    selected_input = fifo_path if fifo_role == "input" else input_path
    expected_code = "manifest_invalid" if fifo_role == "manifest" else "input_invalid"
    program = """
import sys
from pathlib import Path
from src.ingestion.parse_metadata import ParseMetadataError, build_parse_metadata
try:
    build_parse_metadata(
        Path(sys.argv[1]),
        manifest_path=Path(sys.argv[2]),
        edition_year=2020,
        pages="all",
    )
except ParseMetadataError as error:
    print(error.code)
    raise SystemExit(0)
raise SystemExit(3)
"""

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(selected_input),
                str(selected_manifest),
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("bounded metadata reader blocked on a nonregular file")

    assert completed.returncode == 0
    assert completed.stdout.strip() == expected_code
    assert str(fifo_path) not in completed.stdout + completed.stderr


@pytest.mark.parametrize("nonregular_role", ["manifest", "input"])
def test_directory_is_rejected_as_nonregular(
    tmp_path: Path,
    nonregular_role: str,
) -> None:
    """Catches reading directory or device-like inputs through a path helper."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    selected_manifest = tmp_path if nonregular_role == "manifest" else manifest_path
    selected_input = tmp_path if nonregular_role == "input" else input_path
    expected_code = (
        "manifest_invalid" if nonregular_role == "manifest" else "input_invalid"
    )

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            selected_input,
            manifest_path=selected_manifest,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == expected_code
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_manifest_duplicate_keys_are_rejected_value_free(tmp_path: Path) -> None:
    """Catches JSON last-key-wins changing an approved manifest field."""
    manifest_path, documents = _write_manifest(tmp_path)
    raw = manifest_path.read_text(encoding="utf-8")
    sentinel = "PRIVATE-DUPLICATE-DOC"
    duplicated = raw.replace(
        '"doc_id": "fixture-2020"',
        f'"doc_id": "{sentinel}", "doc_id": "fixture-2020"',
        1,
    )
    assert duplicated != raw
    manifest_path.write_text(duplicated, encoding="utf-8")
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == "manifest_invalid"
    assert sentinel not in str(captured.value) + repr(captured.value)


def test_manifest_symlink_swap_cannot_mix_hash_and_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches hashing one manifest while validating a swapped symlink target."""
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_payload = _manifest_payload()
    second_payload = _manifest_payload()
    second_documents = second_payload["documents"]
    assert isinstance(second_documents, list)
    second_2020 = second_documents[0]
    assert isinstance(second_2020, dict)
    second_2020["doc_id"] = "fixture-swapped-2020"
    second_2020["sha256"] = "e" * 64
    first_path.write_text(json.dumps(first_payload, sort_keys=True), encoding="utf-8")
    second_path.write_text(json.dumps(second_payload, sort_keys=True), encoding="utf-8")
    swapped_document = load_manifest(second_path)[0]
    link = tmp_path / "manifest.json"
    link.symlink_to(first_path)
    original_read = os.read
    read_calls = 0

    def swap_after_binary_open(descriptor: int, byte_count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            link.unlink()
            link.symlink_to(second_path)
        return original_read(descriptor, byte_count)

    monkeypatch.setattr(os, "read", swap_after_binary_open)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(swapped_document))

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=link,
            edition_year=2020,
            pages="all",
        )

    assert captured.value.code == "policy_mismatch"
    assert read_calls >= 1


def test_each_file_uses_one_consistent_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches path reopening between fstat, read, and digest calculation."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    original_open = os.open
    original_fstat = os.fstat
    original_read = os.read
    original_close = os.close
    live: dict[int, str] = {}
    open_counts: Counter[str] = Counter()
    event_counts: Counter[tuple[str, str]] = Counter()

    def tracked_open(
        path: os.PathLike[str] | str, flags: int, mode: int = 0o777
    ) -> int:
        descriptor = original_open(path, flags, mode)
        label = "manifest" if Path(path) == manifest_path else "input"
        live[descriptor] = label
        open_counts[label] += 1
        return descriptor

    def tracked_fstat(descriptor: int) -> os.stat_result:
        event_counts[(live[descriptor], "fstat")] += 1
        return original_fstat(descriptor)

    def tracked_read(descriptor: int, byte_count: int) -> bytes:
        event_counts[(live[descriptor], "read")] += 1
        return original_read(descriptor, byte_count)

    def tracked_close(descriptor: int) -> None:
        event_counts[(live[descriptor], "close")] += 1
        original_close(descriptor)
        del live[descriptor]

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fstat", tracked_fstat)
    monkeypatch.setattr(os, "read", tracked_read)
    monkeypatch.setattr(os, "close", tracked_close)

    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )

    assert metadata.record_counts["total"] == 2
    assert open_counts == {"manifest": 1, "input": 1}
    assert live == {}
    for label in ("manifest", "input"):
        assert event_counts[(label, "fstat")] >= 2
        assert event_counts[(label, "read")] >= 1
        assert event_counts[(label, "close")] == 1


def test_partial_descriptor_failure_is_value_and_context_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches hashing or parsing bytes from an incomplete descriptor read."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "PRIVATE-PARTIAL-READ.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    original_open = os.open
    original_read = os.read
    input_descriptor: int | None = None
    partial_returned = False
    sentinel = "PRIVATE-READ-FAILURE"

    def identify_open(
        path: os.PathLike[str] | str, flags: int, mode: int = 0o777
    ) -> int:
        nonlocal input_descriptor
        descriptor = original_open(path, flags, mode)
        if Path(path) == input_path:
            input_descriptor = descriptor
        return descriptor

    def fail_after_partial(descriptor: int, byte_count: int) -> bytes:
        nonlocal partial_returned
        if descriptor != input_descriptor:
            return original_read(descriptor, byte_count)
        if not partial_returned:
            partial_returned = True
            return original_read(descriptor, min(byte_count, 16))
        raise OSError(sentinel)

    monkeypatch.setattr(os, "open", identify_open)
    monkeypatch.setattr(os, "read", fail_after_partial)

    with pytest.raises(ParseMetadataError) as captured:
        build_parse_metadata(
            input_path,
            manifest_path=manifest_path,
            edition_year=2020,
            pages="all",
        )

    assert partial_returned is True
    assert captured.value.code == "input_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    disclosed = (
        str(captured.value)
        + repr(captured.value)
        + "".join(traceback.format_exception(captured.value))
    )
    assert sentinel not in disclosed
    assert "PRIVATE-PARTIAL-READ" not in disclosed
    assert str(input_path) not in disclosed


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("record_counts", {"extracted": 0, "quarantined": 2, "total": 1}),
        ("case_type_counts", {"audit": 1, "qa": 0, "total": 0}),
        (
            "record_quarantine_reason_counts",
            {
                "ocr-adapter-failed": 0,
                "ocr-provenance-invalid": 0,
                "page-extraction-failed": 1,
                "page-render-failed": 0,
            },
        ),
        (
            "layout_evidence_counts",
            {
                "detected": 0,
                "failed": 0,
                "no_evidence": 1,
                "not_applicable": 0,
                "not_detected": 0,
                "unavailable": 0,
            },
        ),
        (
            "layout_sampling_counts",
            {"all_cases_required": 0, "no_segment": 2, "sampling_required": 0},
        ),
        (
            "review_counts",
            {
                "critical_not_applicable": 1,
                "critical_sampling_required": 0,
                "critical_unverified": 0,
                "review_machine_extracted": 0,
                "review_needs_review": 0,
                "upstream_machine_extracted": 0,
                "upstream_needs_review": 0,
            },
        ),
        (
            "missing_required_role_counts",
            {
                "audit_answer": 0,
                "audit_facts": 0,
                "audit_title": 0,
                "qa_answer": 0,
                "qa_question": 0,
                "qa_title": 1,
            },
        ),
    ],
)
def test_canonical_renderer_rejects_cross_count_invariant_drift(
    tmp_path: Path, field: str, replacement: dict[str, int]
) -> None:
    """Catches internally inconsistent aggregate metadata being serialized."""
    manifest_path, documents = _write_manifest(tmp_path)
    input_path = tmp_path / "native.jsonl"
    _write_jsonl(input_path, _native_quarantine_records(documents[0]))
    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    forged = metadata.model_copy(update={field: replacement})

    with pytest.raises(ParseMetadataError) as captured:
        canonical_metadata_bytes(forged)

    assert captured.value.code == "input_invalid"


def test_canonical_renderer_rejects_page_and_ocr_sampling_drift(
    tmp_path: Path,
) -> None:
    """Catches page bounds or OCR sampling partitions diverging from record totals."""
    manifest_path, documents = _write_manifest(tmp_path)
    native_path = tmp_path / "native.jsonl"
    _write_jsonl(native_path, _native_quarantine_records(documents[0]))
    native = build_parse_metadata(
        native_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    forged_page_set = native.model_copy(
        update={"page_set": native.page_set.model_copy(update={"first": 2})}
    )
    digest = "sha256:" + "d" * 64
    ocr_path = tmp_path / "ocr.jsonl"
    _write_jsonl(
        ocr_path,
        (_ocr_quarantine_record(documents[-1], image_digest=digest),),
    )
    ocr = build_parse_metadata(
        ocr_path,
        manifest_path=manifest_path,
        edition_year=2025,
        pages="2",
        expected_image_digest=digest,
    )
    forged_sampling = ocr.model_copy(
        update={
            "layout_sampling_counts": {
                "all_cases_required": 0,
                "no_segment": 0,
                "sampling_required": 0,
            }
        }
    )

    for forged in (forged_page_set, forged_sampling):
        with pytest.raises(ParseMetadataError) as captured:
            canonical_metadata_bytes(forged)
        assert captured.value.code == "input_invalid"


def test_canonical_renderer_requires_task8_zero_eligibility(tmp_path: Path) -> None:
    """Catches internally consistent eligibility pairs becoming publishable early."""
    manifest_path, documents = _write_manifest(tmp_path)
    document = documents[0]
    input_path = tmp_path / "case.jsonl"
    _write_jsonl(
        input_path,
        (
            _native_quarantine_records(document)[0],
            _native_case_record(document, sentinel="PRIVATE-ELIGIBILITY"),
        ),
    )
    metadata = build_parse_metadata(
        input_path,
        manifest_path=manifest_path,
        edition_year=2020,
        pages="all",
    )
    forged = metadata.model_copy(
        update={
            "eligibility_counts": {
                "answer_eligible": 1,
                "answer_ineligible": 0,
                "search_eligible": 1,
                "search_ineligible": 0,
            }
        }
    )

    with pytest.raises(ParseMetadataError) as captured:
        canonical_metadata_bytes(forged)

    assert captured.value.code == "input_invalid"
