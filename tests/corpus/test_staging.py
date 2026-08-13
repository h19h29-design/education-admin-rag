from __future__ import annotations

import hashlib
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from src.corpus import staging as staging_module
from src.corpus.chunking import (
    BGE_M3_REQUIRED_PATHS,
    TOKENIZER_REQUIRED_PATHS,
    tokenizer_runtime_fingerprint_sha256,
    validate_embedding_model_lock,
)
from src.corpus.finalize import FinalizationError, finalize_review_ready_bundle
from src.corpus.models import SourceSpan
from src.corpus.staging import (
    PreparedReviewBatch,
    StagingError,
    export_review_ready,
    prepare_review_batch,
    prepare_review_corpus,
    prepare_review_corpus_from_artifacts,
    write_review_package,
)
from src.corpus.storage import (
    GENESIS_ISSUANCE_AUTHORITY_SHA256,
    initialize_issuance_registry,
)
from src.ingestion.extract_common import LayoutEvidence
from src.ingestion.manifest import (
    PageNumberingPolicy,
    PageSizeProfile,
    SourceDocument,
    SourceManifest,
)
from src.ingestion.ocr_authority import (
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
)
from src.ingestion.parse_common import (
    BoundaryQuarantine,
    LayoutSegmentProvenance,
    ParsedCaseCandidate,
    ParseResult,
    ParserLine,
    ParserPage,
    ParserQuarantine,
    RoleFragment,
    UpstreamPageQuarantine,
    _layout_registry_sha256,
)
from src.ingestion.parse_metadata import VerifiedParseRun
from src.ingestion.review import ReviewStore

RELEASE_ID = "corpus-20250808123456-deadbeef"
_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
_AUTHORITY_SOURCE_SHA256 = (
    "9a6a5b3745eb4200c70f9d33395c8b25b5a55fa171036127f2be5791224455bc"
)


def _span(text: str, *, y: float) -> SourceSpan:
    return SourceSpan(
        pdf_page_index=1,
        page_label="1",
        bbox=(10.0, y, 300.0, y + 20.0),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _document() -> SourceDocument:
    return SourceDocument(
        doc_id="fixture-2023",
        edition_year=2023,
        official_title="합성 사례집",
        publisher="합성 교육청",
        registration_no=None,
        source_period_start=None,
        source_period_end=None,
        source_filename="2023.pdf",
        source_relpath="2023.pdf",
        sha256="a" * 64,
        pdf_page_count=1,
        page_size_profiles=(
            PageSizeProfile(
                start_pdf_page=1,
                end_pdf_page=1,
                width_pt=600.0,
                height_pt=800.0,
            ),
        ),
        extraction_method="ocr",
        source_dpi=96,
        render_dpi=300,
        page_numbering=PageNumberingPolicy(
            mode="offset",
            body_start_pdf_page=1,
            body_end_pdf_page=1,
            offset=0,
        ),
        official_public_url=None,
        official_url_status="unverified",
        redistribution_status="unverified",
        access_level="staff",
    )


def _parsed() -> tuple[ParseResult, tuple[ParserPage, ...]]:
    raw_values = ("합성 제목", "합성 질문", "합성 답변")
    spans = tuple(
        _span(text, y=10.0 + index * 30.0) for index, text in enumerate(raw_values)
    )
    lines = tuple(
        ParserLine(
            raw_text=text,
            normalized_text=text,
            bbox=span.bbox,
            confidence=0.99,
            font="fixture",
            size=11.0,
            source_block_index=0,
            source_line_index=index,
            source_span_index=0,
            semantic_hint=None,
            raw_text_sha256=span.text_sha256,
        )
        for index, (text, span) in enumerate(zip(raw_values, spans, strict=True))
    )
    roles: tuple[Literal["title", "question", "answer"], ...] = (
        "title",
        "question",
        "answer",
    )
    fragments = tuple(
        RoleFragment(
            role=role,
            text=text,
            source_span=span,
            confidence=0.99,
        )
        for role, text, span in zip(roles, raw_values, spans, strict=True)
    )
    candidate = ParsedCaseCandidate(
        doc_id="fixture-2023",
        edition_year=2023,
        case_type="qa",
        domain="계약",
        part="계약 일반",
        subtopic=None,
        case_no="1",
        fragments=fragments,
        title="합성 제목",
        question="합성 질문",
        answer="합성 답변",
        facts=None,
        basis_text=None,
        target_text=None,
        situation_text=None,
        source_spans=spans,
        extraction_source="ocr",
        extraction_confidence=0.99,
        layout_segment_id=None,
        layout_segment_provenances=(),
        upstream_review_status="needs_review",
        critical_field_review="unverified",
        review_status="needs_review",
    )
    page = ParserPage(
        doc_id="fixture-2023",
        edition_year=2023,
        extraction_source="ocr",
        source_sha256="a" * 64,
        pdf_page_index=1,
        page_label="1",
        page_width=600.0,
        page_height=800.0,
        render_sha256="b" * 64,
        lines=lines,
        page_role_hint="body",
        upstream_review_status="needs_review",
        critical_review_policy="all-fields-human-verification",
        critical_fields=("title", "question", "answer"),
        layout_evidence=LayoutEvidence(status="not_applicable"),
    )
    return ParseResult(cases=(candidate,)), (page,)


def _authority_document() -> SourceDocument:
    payload = _document().model_dump(mode="python")
    payload.update(
        doc_id="sen-qa-2023",
        source_sha256=_AUTHORITY_SOURCE_SHA256,
    )
    payload["sha256"] = payload.pop("source_sha256")
    return SourceDocument.model_validate(payload)


def _authority_parsed() -> tuple[ParseResult, tuple[ParserPage, ...]]:
    result, pages = _parsed()
    candidate = result.cases[0].model_copy(update={"doc_id": "sen-qa-2023"})
    page = pages[0].model_copy(
        update={
            "doc_id": "sen-qa-2023",
            "source_sha256": _AUTHORITY_SOURCE_SHA256,
        }
    )
    return ParseResult(cases=(candidate,)), (page,)


def _authority_file(tmp_path: Path) -> tuple[Path, bytes, str, str]:
    lock = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint="sha256:" + "2" * 64,
        vision_2025_runtime_fingerprint="sha256:" + "3" * 64,
    )
    raw = canonical_ocr_authority_bytes(lock)
    path = tmp_path / "ocr-authority-lock.json"
    path.write_bytes(raw)
    return path, raw, hashlib.sha256(raw).hexdigest(), lock.self_sha256


def _rewrite_canonical_json(path: Path, payload: object) -> None:
    path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    )


def _authority_bound_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PreparedReviewBatch, bytes, str, str]:
    document = _authority_document()
    result, pages = _authority_parsed()
    input_root = tmp_path / "raw-pages"
    input_dir = input_root / "ocr-2023"
    input_dir.mkdir(parents=True)
    input_path = input_dir / f"{document.doc_id}.jsonl"
    input_path.write_bytes(b"{}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}\n")
    lock_path, lock_bytes, lock_sha256, lock_self_sha256 = _authority_file(tmp_path)
    run = object.__new__(VerifiedParseRun)
    object.__setattr__(run, "document", document)
    object.__setattr__(run, "records", ())
    object.__setattr__(run, "result", result)
    object.__setattr__(run, "pages", pages)
    object.__setattr__(
        run, "manifest_bytes", staging_module._document_manifest_bytes(document)
    )
    object.__setattr__(run, "input_bytes", b"{}\n")
    monkeypatch.setattr(staging_module, "load_manifest", lambda _path: (document,))

    def verified_run(*_args: object, **kwargs: object) -> VerifiedParseRun:
        assert kwargs["expected_image_digest"] is None
        assert kwargs["ocr_authority_lock_path"] == lock_path
        assert kwargs["expected_ocr_authority_lock_sha256"] == lock_sha256
        return run

    monkeypatch.setattr(staging_module, "build_parse_run", verified_run)
    batch = prepare_review_corpus_from_artifacts(
        input_root,
        manifest_path=manifest_path,
        ingestion_version="ingestion-v1",
        expected_image_digest=None,
        ocr_authority_lock_path=lock_path,
        expected_ocr_authority_lock_sha256=lock_sha256,
    )
    return batch, lock_bytes, lock_sha256, lock_self_sha256


def _parser_quarantines() -> tuple[ParserQuarantine, ...]:
    span = _span("PRIVATE-QUARANTINE-SENTINEL", y=10.0)
    return (
        BoundaryQuarantine(
            location_id="loc-" + "1" * 32,
            page_ids=(1,),
            source_spans=(span,),
            span_count=1,
        ),
        UpstreamPageQuarantine(
            location_id="loc-" + "2" * 32,
            reason_code="ocr-adapter-failed",
            page_ids=(1,),
        ),
    )


def _native_batch(
    *,
    ingestion_version: str = "ingestion-v1",
    quarantines: tuple[ParserQuarantine, ...] = (),
) -> PreparedReviewBatch:
    document_payload = _document().model_dump(mode="python")
    document_payload.update(
        doc_id="fixture-2022",
        edition_year=2022,
        extraction_method="native",
        source_dpi=None,
        render_dpi=None,
        native_review_layout_segments=(
            {
                "segment_id": "native-layout-fixture-2022-body-v1",
                "start_pdf_page": 1,
                "end_pdf_page": 1,
                "sampling_policy": "native-layout-sample",
                "policy_version": "native-review-layout-segment-v1",
            },
        ),
    )
    document = SourceDocument.model_validate(document_payload)
    result, pages = _parsed()
    candidate_payload = result.cases[0].model_dump(mode="python")
    candidate_payload.update(
        doc_id="fixture-2022",
        edition_year=2022,
        extraction_source="native",
        critical_field_review="not_applicable",
    )
    candidate = ParsedCaseCandidate.model_validate(candidate_payload)
    page_payload = pages[0].model_dump(mode="python")
    page_payload.update(
        doc_id="fixture-2022",
        edition_year=2022,
        extraction_source="native",
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
        critical_fields=(),
    )
    page = ParserPage.model_validate(page_payload)
    return prepare_review_batch(
        document=document,
        result=ParseResult(cases=(candidate,), quarantines=quarantines),
        pages=(page,),
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version=ingestion_version,
    )


def _native_document(year: int) -> SourceDocument:
    document_payload = _document().model_dump(mode="python")
    document_payload.update(
        doc_id=f"fixture-{year}",
        edition_year=year,
        extraction_method="native",
        source_dpi=None,
        render_dpi=None,
        native_review_layout_segments=(
            {
                "segment_id": f"native-layout-fixture-{year}-body-v1",
                "start_pdf_page": 1,
                "end_pdf_page": 1,
                "sampling_policy": "native-layout-sample",
                "policy_version": "native-review-layout-segment-v1",
            },
        ),
    )
    return SourceDocument.model_validate(document_payload)


def _native_run(
    year: int,
    *,
    include_case: bool,
    quarantines: tuple[ParserQuarantine, ...] = (),
) -> VerifiedParseRun:
    document = _native_document(year)
    result, pages = _parsed()
    candidate_payload = result.cases[0].model_dump(mode="python")
    candidate_payload.update(
        doc_id=document.doc_id,
        edition_year=year,
        extraction_source="native",
        critical_field_review="not_applicable",
    )
    candidate = ParsedCaseCandidate.model_validate(candidate_payload)
    page_payload = pages[0].model_dump(mode="python")
    page_payload.update(
        doc_id=document.doc_id,
        edition_year=year,
        extraction_source="native",
        upstream_review_status="machine_extracted",
        critical_review_policy="not_applicable",
        critical_fields=(),
    )
    page = ParserPage.model_validate(page_payload)
    run = object.__new__(VerifiedParseRun)
    object.__setattr__(run, "document", document)
    object.__setattr__(run, "records", ())
    object.__setattr__(
        run,
        "result",
        ParseResult(
            cases=(candidate,) if include_case else (),
            quarantines=quarantines,
        ),
    )
    object.__setattr__(run, "pages", (page,))
    manifest = SourceManifest(
        documents=tuple(_native_document(item) for item in range(2020, 2026))
    )
    manifest_bytes = (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    object.__setattr__(run, "manifest_bytes", manifest_bytes)
    object.__setattr__(run, "input_bytes", f"input-{year}\n".encode())
    return run


def test_prepare_review_batch_binds_raw_spans_quality_and_registry() -> None:
    result, pages = _parsed()

    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )

    assert len(batch.cases) == 1
    case = batch.cases[0]
    assert case.review_status == "needs_review"
    assert case.critical_field_review == "pending"
    assert not case.search_eligible and not case.answer_eligible
    assert batch.envelopes[0].candidate_case == case
    assert batch.envelopes[0].role_sources[0].raw_text == "합성 질문"
    assert (
        batch.registry.cases[0].content_sha256 == batch.envelopes[0].fingerprint_sha256
    )
    assert batch.assessments[0].case_id == case.case_id
    assert batch.document_page_counts["fixture-2023"].model_dump() == {
        "failed": 0,
        "quarantined": 0,
        "succeeded": 1,
    }
    assert len(batch.manifest_sha256) == 64
    assert {item.reason_code for item in batch.assessments[0].findings} == {
        "critical-fields-unverified"
    }


def test_native_staging_assigns_only_the_manifest_segment_covering_every_span() -> None:
    """Catches native candidates being grouped without exact manifest coverage."""
    batch = _native_batch()

    binding = batch.sampling_candidates[0]
    assert binding.native_layout_segment is not None
    assert binding.native_layout_segment.segment_id == (
        "native-layout-fixture-2022-body-v1"
    )
    assert binding.doc_id == "fixture-2022"
    assert binding.source_sha256 == "a" * 64


def test_native_staging_rejects_a_case_crossing_manifest_segments() -> None:
    """Catches one case being silently enrolled in two native strata."""
    document_payload = _document().model_dump(mode="python")
    document_payload.update(
        doc_id="fixture-2022",
        edition_year=2022,
        extraction_method="native",
        source_dpi=None,
        render_dpi=None,
        pdf_page_count=2,
        page_size_profiles=(
            {
                "start_pdf_page": 1,
                "end_pdf_page": 2,
                "width_pt": 600.0,
                "height_pt": 800.0,
            },
        ),
        page_numbering={
            "mode": "offset",
            "body_start_pdf_page": 1,
            "body_end_pdf_page": 2,
            "offset": 0,
        },
        native_review_layout_segments=(
            {
                "segment_id": "native-layout-a",
                "start_pdf_page": 1,
                "end_pdf_page": 1,
                "sampling_policy": "native-layout-sample",
                "policy_version": "native-review-layout-segment-v1",
            },
            {
                "segment_id": "native-layout-b",
                "start_pdf_page": 2,
                "end_pdf_page": 2,
                "sampling_policy": "native-layout-sample",
                "policy_version": "native-review-layout-segment-v1",
            },
        ),
    )
    document = SourceDocument.model_validate(document_payload)
    result, _ = _parsed()
    candidate = result.cases[0].model_copy(
        update={
            "doc_id": "fixture-2022",
            "edition_year": 2022,
            "extraction_source": "native",
            "source_spans": (
                result.cases[0].source_spans[0],
                result.cases[0]
                .source_spans[1]
                .model_copy(update={"pdf_page_index": 2}),
            ),
        }
    )

    with pytest.raises(StagingError, match="staging_input_invalid"):
        staging_module.assign_native_review_segment(document, candidate)


def test_writer_rejects_native_segment_drift_from_the_source_manifest(
    tmp_path: Path,
) -> None:
    """Catches a sealed batch relabeling a native stratum after preparation."""
    batch = _native_batch()
    original = batch.sampling_candidates[0]
    assert original.native_layout_segment is not None
    forged_segment = original.native_layout_segment.model_copy(
        update={"segment_id": "native-layout-forged-body-v1"}
    )
    forged = original.__class__(
        reference=original.reference,
        edition_year=original.edition_year,
        extraction_source=original.extraction_source,
        pii_class=original.pii_class,
        review_status=original.review_status,
        layout_segment_provenances=original.layout_segment_provenances,
        native_layout_segment=forged_segment,
        doc_id=original.doc_id,
        source_sha256=original.source_sha256,
    )
    object.__setattr__(batch, "sampling_candidates", (forged,))

    with pytest.raises(StagingError, match="staging_write_invalid"):
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)


def test_writer_rejects_ocr_segment_drift_from_candidate_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a sealed OCR case being relabeled into another layout stratum."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    original = batch.sampling_candidates[0]
    registry_sha256 = _layout_registry_sha256(
        detector_version="vision-layout-v1",
        doc_id="sen-qa-2025",
        edition_year=2025,
        sampling_status="sampling_required",
        segment_start_pdf_page=1,
        segment_end_pdf_page=1,
        source_sha256="3" * 64,
    )
    forged_provenance = LayoutSegmentProvenance(
        segment_id="layout-segment-" + "f" * 32,
        segment_key="approved-document-body",
        segment_start_pdf_page=1,
        segment_end_pdf_page=1,
        registry_policy_version="layout-segment-registry-v1",
        registry_sha256=registry_sha256,
        detector_version="vision-layout-v1",
        region_count=0,
        sampling_status="sampling_required",
        doc_id="sen-qa-2025",
        edition_year=2025,
        source_sha256="3" * 64,
        pdf_page_index=1,
        render_sha256="4" * 64,
    )
    forged = original.__class__(
        reference=original.reference,
        edition_year=original.edition_year,
        extraction_source=original.extraction_source,
        pii_class=original.pii_class,
        review_status=original.review_status,
        layout_segment_provenances=(forged_provenance,),
        doc_id=original.doc_id,
        source_sha256=original.source_sha256,
    )
    object.__setattr__(batch, "sampling_candidates", (forged,))

    with pytest.raises(StagingError, match="staging_write_invalid"):
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)


def test_writer_recursively_rejects_sampling_provenance_type_bomb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches model_construct bypasses crossing the package writer boundary."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    original = batch.sampling_candidates[0]
    sentinel = "PRIVATE-SAMPLING-PROVENANCE-SENTINEL"
    malformed = LayoutSegmentProvenance.model_construct(segment_id=sentinel)
    forged = original.__class__(
        reference=original.reference,
        edition_year=original.edition_year,
        extraction_source=original.extraction_source,
        pii_class=original.pii_class,
        review_status=original.review_status,
        layout_segment_provenances=(malformed,),
        doc_id=original.doc_id,
        source_sha256=original.source_sha256,
    )
    object.__setattr__(batch, "sampling_candidates", (forged,))

    with pytest.raises(StagingError) as captured:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    diagnostics = str(captured.value) + repr(captured.value)
    assert str(captured.value) == "staging_write_invalid"
    assert sentinel not in diagnostics
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_review_package_is_owner_only_no_clobber_and_enqueues_every_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, lock_bytes, lock_sha256, lock_self_sha256 = _authority_bound_batch(
        tmp_path,
        monkeypatch,
    )

    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert package == tmp_path / "review"
    assert (package.stat().st_mode & 0o777) == 0o700
    assert ((package / "registry.json").stat().st_mode & 0o777) == 0o600
    documents = json.loads((package / "documents.json").read_text())
    evidence = json.loads((package / "ingestion-evidence.json").read_text())
    assert documents["documents"][0]["doc_id"] == "sen-qa-2023"
    assert documents["schema_version"] == "sen-qa-review-documents/v1"
    assert evidence["document_page_counts"] == {
        "sen-qa-2023": {"failed": 0, "quarantined": 0, "succeeded": 1}
    }
    assert evidence["manifest_sha256"] == batch.manifest_sha256
    assert evidence["parser_quarantine_count"] == 0
    assert evidence["ocr_authority_lock_sha256"] == lock_sha256
    assert evidence["ocr_authority_self_sha256"] == lock_self_sha256
    assert evidence["schema_version"] == "sen-qa-ingestion-evidence/v2"
    assert (package / "ocr-authority-lock.json").read_bytes() == lock_bytes
    assert ((package / "ocr-authority-lock.json").stat().st_mode & 0o777) == 0o600
    summary = json.loads((package / "summary.json").read_text())
    assert summary["ocr_authority_lock_sha256"] == lock_sha256
    assert ((package / "documents.json").stat().st_mode & 0o777) == 0o600
    assert ((package / "ingestion-evidence.json").stat().st_mode & 0o777) == 0o600
    assert (
        (package / "candidates" / f"{batch.cases[0].case_id}.json").stat().st_mode
        & 0o777
    ) == 0o600
    with ReviewStore(package / "review.sqlite3") as store:
        record = store.get(batch.cases[0].case_id)
    assert record.review_status == "needs_review"
    with pytest.raises(StagingError, match="review_package_exists"):
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)


def test_prepare_review_batch_rejects_raw_span_mismatch_without_value_leak() -> None:
    result, pages = _parsed()
    forged = pages[0].model_copy(
        update={
            "lines": (
                pages[0].lines[0].model_copy(update={"raw_text": "PRIVATE_SENTINEL"}),
                *pages[0].lines[1:],
            )
        }
    )

    with pytest.raises(StagingError, match="staging_input_invalid") as captured:
        prepare_review_batch(
            document=_document(),
            result=result,
            pages=(forged,),
            parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
            raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
            ingestion_version="ingestion-v1",
        )

    assert "PRIVATE_SENTINEL" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_review_package_revalidates_sealed_batch_before_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    object.__setattr__(batch, "parser_authority_sha256", "0" * 64)

    with pytest.raises(StagingError, match="staging_write_invalid") as captured:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_managed_artifact_layout_builds_one_corpus_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _authority_document()
    result, pages = _authority_parsed()
    input_root = tmp_path / "raw-pages"
    input_dir = input_root / "ocr-2023"
    input_dir.mkdir(parents=True)
    input_path = input_dir / f"{document.doc_id}.jsonl"
    input_path.write_bytes(b"{}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}\n")
    lock_path, lock_bytes, lock_sha256, lock_self_sha256 = _authority_file(tmp_path)
    run = object.__new__(VerifiedParseRun)
    object.__setattr__(run, "document", document)
    object.__setattr__(run, "records", ())
    object.__setattr__(run, "result", result)
    object.__setattr__(run, "pages", pages)
    object.__setattr__(run, "manifest_bytes", b"{}\n")
    object.__setattr__(run, "input_bytes", b"{}\n")
    monkeypatch.setattr(staging_module, "load_manifest", lambda _path: (document,))
    calls: list[dict[str, object]] = []

    def verified_run(*_args: object, **kwargs: object) -> VerifiedParseRun:
        calls.append(dict(kwargs))
        return run

    monkeypatch.setattr(staging_module, "build_parse_run", verified_run)

    batch = prepare_review_corpus_from_artifacts(
        input_root,
        manifest_path=manifest_path,
        ingestion_version="ingestion-v1",
        expected_image_digest=None,
        ocr_authority_lock_path=lock_path,
        expected_ocr_authority_lock_sha256=lock_sha256,
    )

    assert len(batch.documents) == 1
    assert len(batch.cases) == 1
    assert batch.quarantine_count == 0
    assert batch.envelopes[0].parser_authority_sha256 == batch.parser_authority_sha256
    assert batch.ocr_authority_lock_bytes == lock_bytes
    assert batch.ocr_authority_lock_sha256 == lock_sha256
    assert batch.ocr_authority_self_sha256 == lock_self_sha256
    assert calls == [
        {
            "edition_year": 2023,
            "expected_image_digest": None,
            "expected_ocr_authority_lock_sha256": lock_sha256,
            "manifest_path": manifest_path,
            "ocr_authority_lock_path": lock_path,
            "pages": "1-1",
        }
    ]


@pytest.mark.parametrize(
    "case",
    ["legacy-digest", "missing-sha", "wrong-sha", "symlink", "type-bomb"],
)
def test_ocr_artifact_staging_rejects_stale_or_unverified_authority_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Catches a six-year staging run using anything but external lock authority."""
    document = _authority_document()
    input_root = tmp_path / "raw-pages"
    input_dir = input_root / "ocr-2023"
    input_dir.mkdir(parents=True)
    (input_dir / f"{document.doc_id}.jsonl").write_bytes(b"{}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}\n")
    lock_path, _, lock_sha256, _ = _authority_file(tmp_path)
    selected_path: object = lock_path
    selected_sha: object = lock_sha256
    legacy_digest = None
    if case == "legacy-digest":
        selected_path = None
        selected_sha = None
        legacy_digest = "sha256:" + "d" * 64
    elif case == "missing-sha":
        selected_sha = None
    elif case == "wrong-sha":
        selected_sha = "f" * 64
    elif case == "symlink":
        link = tmp_path / "PRIVATE-AUTHORITY-SENTINEL.json"
        link.symlink_to(lock_path)
        selected_path = link
    else:
        selected_path = cast(Any, object())
    monkeypatch.setattr(staging_module, "load_manifest", lambda _path: (document,))

    def parse_bomb(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parser must not run before authority validation")

    monkeypatch.setattr(staging_module, "build_parse_run", parse_bomb)
    with pytest.raises(StagingError, match="staging_input_invalid") as caught:
        prepare_review_corpus_from_artifacts(
            input_root,
            manifest_path=manifest_path,
            ingestion_version="ingestion-v1",
            expected_image_digest=legacy_digest,
            ocr_authority_lock_path=cast(Any, selected_path),
            expected_ocr_authority_lock_sha256=cast(Any, selected_sha),
        )

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "PRIVATE-AUTHORITY-SENTINEL" not in diagnostics


def test_writer_rejects_ocr_unit_batch_without_authority_before_output(
    tmp_path: Path,
) -> None:
    """Catches an unsealed OCR unit batch becoming a production review package."""
    result, pages = _parsed()
    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )

    with pytest.raises(StagingError, match="staging_write_invalid"):
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ocr_authority_lock_bytes", b"{}\n"),
        ("ocr_authority_lock_sha256", "0" * 64),
        ("ocr_authority_self_sha256", "0" * 64),
        ("ocr_authority_lock", None),
    ],
)
def test_writer_recursively_rejects_forged_or_missing_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    """Catches field-level mutation of the sealed OCR authority evidence."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    object.__setattr__(batch, field, replacement)

    with pytest.raises(StagingError, match="staging_write_invalid") as caught:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_writer_rejects_a_replayed_authority_object_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches replacing only the sealed lock object with another valid lock."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    replayed = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint="sha256:" + "4" * 64,
        vision_2025_runtime_fingerprint="sha256:" + "5" * 64,
    )
    object.__setattr__(batch, "ocr_authority_lock", replayed)

    with pytest.raises(StagingError, match="staging_write_invalid") as caught:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_authority_file_write_failure_cleans_the_partial_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a partially written review directory surviving authority I/O failure."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    original = staging_module._write_private

    def fail_authority(path: Path, data: bytes) -> None:
        if path.name == "ocr-authority-lock.json":
            raise OSError("PRIVATE-WRITE-SENTINEL")
        original(path, data)

    monkeypatch.setattr(staging_module, "_write_private", fail_authority)
    with pytest.raises(StagingError, match="staging_write_failed") as caught:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    assert "PRIVATE-WRITE-SENTINEL" not in str(caught.value) + repr(caught.value)


def test_pure_native_package_preserves_legacy_evidence_without_ocr_lock(
    tmp_path: Path,
) -> None:
    """Catches the OCR migration making an unrelated pure-native package invalid."""
    batch = _native_batch()

    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    evidence = json.loads((package / "ingestion-evidence.json").read_text())

    assert evidence["schema_version"] == "sen-qa-ingestion-evidence/v1"
    assert not (package / "parser-quarantines.jsonl").exists()
    assert not (package / "ocr-authority-lock.json").exists()
    sampling_path = package / "sampling-authority.json"
    sampling = json.loads(sampling_path.read_text())
    assert (sampling_path.stat().st_mode & 0o777) == 0o600
    assert sampling["inventory"]["release_id"] == RELEASE_ID
    assert sampling["inventory"]["manifest_sha256"] == batch.manifest_sha256
    assert sampling["inventory"]["segments"][0]["segment_id"] == (
        "native-layout-fixture-2022-body-v1"
    )
    assert sampling["plans"][0]["members"][0]["case_id"] == (batch.cases[0].case_id)


def test_quarantines_are_canonical_value_free_owner_only_and_authority_bound(
    tmp_path: Path,
) -> None:
    """Catches parser quarantine locations being reduced to an unusable count."""
    batch = _native_batch(quarantines=_parser_quarantines())

    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    artifact = package / "parser-quarantines.jsonl"
    raw = artifact.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert raw == batch.parser_quarantines_bytes
    assert hashlib.sha256(raw).hexdigest() == batch.parser_quarantines_sha256
    assert batch.quarantine_count == 2
    assert (artifact.stat().st_mode & 0o777) == 0o600
    assert rows == [
        {
            "doc_id": "fixture-2022",
            "edition_year": 2022,
            "location_id": "loc-" + "1" * 32,
            "page_ids": [1],
            "reason_code": "ambiguous_boundary",
            "source_spans": [
                {
                    "bbox": [10.0, 10.0, 300.0, 30.0],
                    "page_label": "1",
                    "pdf_page_index": 1,
                    "text_sha256": hashlib.sha256(
                        b"PRIVATE-QUARANTINE-SENTINEL"
                    ).hexdigest(),
                }
            ],
            "span_count": 1,
        },
        {
            "doc_id": "fixture-2022",
            "edition_year": 2022,
            "location_id": "loc-" + "2" * 32,
            "occurrence_count": 1,
            "page_ids": [1],
            "reason_code": "ocr-adapter-failed",
            "source_spans": [],
            "span_count": 0,
        },
    ]
    assert b"PRIVATE-QUARANTINE-SENTINEL" not in raw
    evidence = json.loads((package / "ingestion-evidence.json").read_text())
    summary = json.loads((package / "summary.json").read_text())
    assert evidence["schema_version"] == "sen-qa-ingestion-evidence/v3"
    assert evidence["parser_quarantine_count"] == 2
    assert evidence["parser_quarantines_sha256"] == batch.parser_quarantines_sha256
    assert summary["schema_version"] == "sen-qa-review-package/v3"
    assert summary["quarantine_count"] == 2
    assert summary["parser_quarantines_sha256"] == batch.parser_quarantines_sha256


def test_corpus_retains_quarantine_only_document_without_inventing_a_case(
    tmp_path: Path,
) -> None:
    """Catches annual documents with only parser quarantines disappearing from review."""
    quarantine = BoundaryQuarantine(
        location_id="loc-" + "3" * 32,
        page_ids=(1,),
        source_spans=(_span("PRIVATE-ZERO-CASE-SENTINEL", y=10.0),),
        span_count=1,
    )
    batch = prepare_review_corpus(
        (
            _native_run(2021, include_case=False, quarantines=(quarantine,)),
            _native_run(2022, include_case=True),
        ),
        ingestion_version="ingestion-v1",
    )

    assert [document.doc_id for document in batch.documents] == [
        "fixture-2021",
        "fixture-2022",
    ]
    assert [case.doc_id for case in batch.cases] == ["fixture-2022"]
    assert batch.quarantine_count == 1
    rows = [json.loads(line) for line in batch.parser_quarantines_bytes.splitlines()]
    assert rows[0]["doc_id"] == "fixture-2021"
    assert b"PRIVATE-ZERO-CASE-SENTINEL" not in batch.parser_quarantines_bytes
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    assert (package / "parser-quarantines.jsonl").read_bytes() == (
        batch.parser_quarantines_bytes
    )


def test_quarantine_artifact_preserves_repeated_exact_parser_rows() -> None:
    """Catches canonical sealing silently deduplicating parser output."""
    quarantine = _parser_quarantines()[0]

    batch = _native_batch(quarantines=(quarantine, quarantine))

    lines = batch.parser_quarantines_bytes.splitlines(keepends=True)
    assert batch.quarantine_count == 2
    assert len(lines) == 2
    assert lines[0] == lines[1]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("parser_quarantines_bytes", b'{"raw_text":"PRIVATE-VALUE"}\n'),
        ("parser_quarantines_sha256", "0" * 64),
        ("quarantine_count", 1),
    ],
)
def test_writer_recursively_rejects_forged_quarantine_authority_value_free(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    """Catches sealed quarantine bytes, digest, or count being changed independently."""
    batch = _native_batch(quarantines=_parser_quarantines())
    object.__setattr__(batch, field, replacement)

    with pytest.raises(StagingError, match="staging_write_invalid") as caught:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    diagnostics = str(caught.value) + repr(caught.value)
    assert "PRIVATE" not in diagnostics
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_quarantine_artifact_write_failure_cleans_partial_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a partial review package surviving quarantine artifact I/O failure."""
    batch = _native_batch(quarantines=_parser_quarantines())
    original = staging_module._write_private

    def fail_quarantine(path: Path, data: bytes) -> None:
        if path.name == "parser-quarantines.jsonl":
            raise OSError("PRIVATE-WRITE-SENTINEL")
        original(path, data)

    monkeypatch.setattr(staging_module, "_write_private", fail_quarantine)
    with pytest.raises(StagingError, match="staging_write_failed") as caught:
        write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert not (tmp_path / "review").exists()
    assert "PRIVATE-WRITE-SENTINEL" not in str(caught.value) + repr(caught.value)


def test_review_export_validates_quarantine_artifact_then_stays_not_ready(
    tmp_path: Path,
) -> None:
    """Catches parser quarantines being mistaken for terminal human review."""
    batch = _native_batch(quarantines=_parser_quarantines())
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    with pytest.raises(StagingError, match="review_not_ready") as caught:
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (package / "review-ready.attestation.json").exists()


@pytest.mark.parametrize("case", ["missing", "tampered", "mode", "forged-sha"])
def test_review_export_rejects_invalid_quarantine_artifact_before_not_ready(
    tmp_path: Path,
    case: str,
) -> None:
    """Catches missing or forged quarantine authority being hidden by fail-closed state."""
    batch = _native_batch(quarantines=_parser_quarantines())
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    artifact = package / "parser-quarantines.jsonl"
    if case == "missing":
        artifact.unlink()
    elif case == "tampered":
        artifact.write_bytes(artifact.read_bytes() + b'{"raw_text":"PRIVATE"}\n')
    elif case == "mode":
        artifact.chmod(0o644)
    else:
        evidence_path = package / "ingestion-evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["parser_quarantines_sha256"] = "0" * 64
        _rewrite_canonical_json(evidence_path, evidence)

    with pytest.raises(StagingError, match="review_export_invalid") as caught:
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "PRIVATE" not in diagnostics
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (package / "review-ready.attestation.json").exists()


def test_target_fragment_is_bound_into_question_authority() -> None:
    result, pages = _parsed()
    target_text = "합성 대상"
    target_span = _span(target_text, y=35.0)
    target_line = ParserLine(
        raw_text=target_text,
        normalized_text=target_text,
        bbox=target_span.bbox,
        confidence=0.99,
        font="fixture",
        size=11.0,
        source_block_index=0,
        source_line_index=9,
        source_span_index=0,
        semantic_hint=None,
        raw_text_sha256=target_span.text_sha256,
    )
    target_fragment = RoleFragment(
        role="target",
        text=target_text,
        source_span=target_span,
        confidence=0.99,
    )
    candidate = result.cases[0]
    updated_candidate = candidate.model_copy(
        update={
            "fragments": (
                candidate.fragments[0],
                target_fragment,
                *candidate.fragments[1:],
            ),
            "source_spans": (
                candidate.source_spans[0],
                target_span,
                *candidate.source_spans[1:],
            ),
            "target_text": target_text,
        }
    )
    updated_page = pages[0].model_copy(
        update={
            "lines": tuple(
                sorted(
                    (*pages[0].lines, target_line),
                    key=lambda line: (line.bbox[1], line.bbox[0]),
                )
            )
        }
    )

    batch = prepare_review_batch(
        document=_document(),
        result=ParseResult(cases=(updated_candidate,)),
        pages=(updated_page,),
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )

    assert batch.cases[0].question == "합성 대상\n합성 질문"
    assert [source.role for source in batch.envelopes[0].role_sources[:2]] == [
        "question",
        "question",
    ]


def test_review_ready_export_requires_terminal_store_and_binds_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, _, lock_sha256, _ = _authority_bound_batch(tmp_path, monkeypatch)
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    with pytest.raises(StagingError, match="review_not_ready"):
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )
    case = batch.cases[0]
    content_sha256 = batch.envelopes[0].fingerprint_sha256
    with ReviewStore(package / "review.sqlite3") as store:
        store.verify_critical_fields(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=content_sha256,
            reason="fields_checked",
        )
        store.approve_search(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=content_sha256,
            reason="search_checked",
        )
        store.approve_answer(
            case.case_id,
            reviewer_id="reviewer-answer",
            reviewed_content_sha256=content_sha256,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )

    attestation = export_review_ready(
        package,
        release_id=RELEASE_ID,
        expected_registry_sha256=batch.registry.fingerprint_sha256,
    )

    assert attestation == package / "review-ready.attestation.json"
    assert (package / "review-decision-snapshot.json").is_file()
    attestation_payload = json.loads(attestation.read_text())
    assert (
        attestation_payload["documents_sha256"]
        == hashlib.sha256((package / "documents.json").read_bytes()).hexdigest()
    )
    assert (
        attestation_payload["ingestion_evidence_sha256"]
        == hashlib.sha256(
            (package / "ingestion-evidence.json").read_bytes()
        ).hexdigest()
    )
    assert attestation_payload["ocr_authority_lock_sha256"] == lock_sha256
    assert (attestation.stat().st_mode & 0o777) == 0o600
    with pytest.raises(StagingError, match="review_attestation_exists"):
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )


def test_review_export_rejects_missing_authority_file_before_store_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches package tampering that removes the lock after staging."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    (package / "ocr-authority-lock.json").unlink()

    with pytest.raises(StagingError, match="review_export_invalid") as caught:
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (package / "review-ready.attestation.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "downgrade",
        "missing",
        "extra",
        "wrong-file-sha",
        "wrong-self-sha",
        "type-bomb",
    ],
)
def test_review_export_rejects_forged_authority_evidence_before_store_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Catches schema and field tampering in the v2 authority evidence."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    evidence_path = package / "ingestion-evidence.json"
    evidence = json.loads(evidence_path.read_text())
    if case == "downgrade":
        evidence["schema_version"] = "sen-qa-ingestion-evidence/v1"
    elif case == "missing":
        evidence.pop("ocr_authority_lock_sha256")
    elif case == "extra":
        evidence["unexpected_authority"] = "PRIVATE-AUTHORITY-SENTINEL"
    elif case == "wrong-file-sha":
        evidence["ocr_authority_lock_sha256"] = "0" * 64
    elif case == "wrong-self-sha":
        evidence["ocr_authority_self_sha256"] = "0" * 64
    else:
        evidence["ocr_authority_lock_sha256"] = {"value": "PRIVATE-SENTINEL"}
    _rewrite_canonical_json(evidence_path, evidence)

    with pytest.raises(StagingError, match="review_export_invalid") as caught:
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "PRIVATE" not in diagnostics
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (package / "review-ready.attestation.json").exists()


def test_review_export_rejects_a_valid_lock_and_evidence_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches replacing both lock and evidence without the sealed package summary."""
    batch, _, _, _ = _authority_bound_batch(tmp_path, monkeypatch)
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    replayed = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint="sha256:" + "4" * 64,
        vision_2025_runtime_fingerprint="sha256:" + "5" * 64,
    )
    replayed_raw = canonical_ocr_authority_bytes(replayed)
    (package / "ocr-authority-lock.json").write_bytes(replayed_raw)
    evidence_path = package / "ingestion-evidence.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["ocr_authority_lock_sha256"] = hashlib.sha256(replayed_raw).hexdigest()
    evidence["ocr_authority_self_sha256"] = replayed.self_sha256
    _rewrite_canonical_json(evidence_path, evidence)

    with pytest.raises(StagingError, match="review_export_invalid") as caught:
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (package / "review-ready.attestation.json").exists()


def test_review_ready_package_builds_one_atomic_canonical_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _native_batch(ingestion_version="image-" + "e" * 64)
    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)
    case = batch.cases[0]
    envelope_sha256 = batch.envelopes[0].fingerprint_sha256
    with ReviewStore(package / "review.sqlite3") as store:
        store.verify_critical_fields(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=envelope_sha256,
            reason="fields_checked",
        )
        store.approve_search(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=envelope_sha256,
            reason="search_checked",
        )
        store.approve_answer(
            case.case_id,
            reviewer_id="reviewer-answer",
            reviewed_content_sha256=envelope_sha256,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )
    attestation = export_review_ready(
        package,
        release_id=RELEASE_ID,
        expected_registry_sha256=batch.registry.fingerprint_sha256,
    )

    def cache_bytes(path: str) -> bytes:
        return f"fixture:{path}".encode()

    lock = validate_embedding_model_lock(
        {
            "schema_version": 1,
            "language": "korean",
            "packages": {},
            "models": [],
            "embedding_models": [
                {
                    "repo_id": "BAAI/bge-m3",
                    "revision": _REVISION,
                    "files": [
                        {
                            "path": path,
                            "sha256": hashlib.sha256(cache_bytes(path)).hexdigest(),
                            "size": len(cache_bytes(path)),
                            "source_url": (
                                "https://huggingface.co/BAAI/bge-m3/resolve/"
                                f"{_REVISION}/{path}"
                            ),
                        }
                        for path in BGE_M3_REQUIRED_PATHS
                    ],
                }
            ],
        }
    )
    cache = tmp_path / "tokenizer-cache"
    for path in TOKENIZER_REQUIRED_PATHS:
        target = cache / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cache_bytes(path))

    class Backend:
        def encode(self, text: str, *, add_special_tokens: bool) -> object:
            assert not add_special_tokens
            return SimpleNamespace(
                tokens=tuple(text),
                offsets=tuple((index, index + 1) for index in range(len(text))),
            )

        def token_to_id(self, token: str) -> int:
            return ord(token)

        def decode(self, ids: list[int], *, skip_special_tokens: bool) -> str:
            assert not skip_special_tokens
            return "".join(chr(item) for item in ids)

    class TokenizerFactory:
        @staticmethod
        def from_str(_raw: str) -> Backend:
            return Backend()

    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        SimpleNamespace(Tokenizer=TokenizerFactory),
    )
    issuance = tmp_path / "issuance" / "registry.sqlite3"
    issuance.parent.mkdir(mode=0o700)
    initialize_issuance_registry(
        issuance,
        expected_genesis_sha256=GENESIS_ISSUANCE_AUTHORITY_SHA256,
    )
    runtime_lock = tmp_path / "uv.lock"
    runtime_lock.write_bytes(b"runtime-lock\n")
    indexer_image_digest = "sha256:" + "2" * 64
    runtime_sha256 = tokenizer_runtime_fingerprint_sha256(
        runtime_lock.read_bytes(),
        indexer_image_digest=indexer_image_digest,
    )

    with pytest.raises(
        FinalizationError, match="review_ready_attestation_invalid"
    ) as rejected:
        finalize_review_ready_bundle(
            package,
            tmp_path / "release",
            tmp_path / "diagnostics",
            issuance,
            release_id=RELEASE_ID,
            expected_ready_attestation_sha256="0" * 64,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
            expected_model_lock_sha256=lock.fingerprint_sha256,
            expected_runtime_fingerprint_sha256=runtime_sha256,
            container_image="sha256:" + "e" * 64,
            runtime_lock_path=runtime_lock,
            indexer_image_digest=indexer_image_digest,
            embedding_model_lock=lock,
            embedding_model_root=cache,
        )
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert not (tmp_path / "release" / "canonical").exists()

    built = finalize_review_ready_bundle(
        package,
        tmp_path / "release",
        tmp_path / "diagnostics",
        issuance,
        release_id=RELEASE_ID,
        expected_ready_attestation_sha256=hashlib.sha256(
            attestation.read_bytes()
        ).hexdigest(),
        expected_registry_sha256=batch.registry.fingerprint_sha256,
        expected_model_lock_sha256=lock.fingerprint_sha256,
        expected_runtime_fingerprint_sha256=runtime_sha256,
        container_image="sha256:" + "e" * 64,
        runtime_lock_path=runtime_lock,
        indexer_image_digest=indexer_image_digest,
        embedding_model_lock=lock,
        embedding_model_root=cache,
    )

    assert built.bundle_path == tmp_path / "release" / "canonical"
    assert (built.bundle_path / "canonical.sqlite3").is_file()
    assert (built.bundle_path / "manifest.json").is_file()
    assert built.issuance_generation == 1
