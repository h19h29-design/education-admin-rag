from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.corpus import staging as staging_module
from src.corpus.models import SourceSpan
from src.corpus.staging import (
    StagingError,
    export_review_ready,
    prepare_resolved_review_corpus,
    prepare_review_corpus,
    write_review_package,
)
from src.ingestion.manifest import SourceManifest
from src.ingestion.ocr_authority import (
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
)
from src.ingestion.parse_common import BoundaryQuarantine, ParseResult, parse_pages
from src.ingestion.parse_metadata import VerifiedParseRun
from src.ingestion.quarantine_review import (
    ResolutionAnnotation,
    ResolutionSourceSpan,
    append_resolution_event,
    create_resolution_draft,
    load_resolution_authority,
)
from src.ingestion.review import ReviewStore
from tests.corpus.test_staging import (
    _AUTHORITY_SOURCE_SHA256,
    RELEASE_ID,
    _authority_document,
    _native_document,
    _native_run,
)
from tests.ingestion.test_verified_parser_annotations import _unlabeled_hierarchy_page


def _load_authority(path: Path, raw: bytes):
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    sha256 = hashlib.sha256(raw).hexdigest()
    return load_resolution_authority(path, expected_sha256=sha256), sha256


def test_resolved_bridge_rebuilds_under_resolution_bound_parser_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _native_run(2022, include_case=True)
    span = SourceSpan.model_validate(
        run.result.cases[0].source_spans[0].model_dump(mode="python")
    )
    quarantine = BoundaryQuarantine(
        location_id="loc-" + "9" * 32,
        page_ids=(span.pdf_page_index,),
        source_spans=(span,),
        span_count=1,
    )
    source_run = object.__new__(type(run))
    for name in ("document", "records", "pages", "manifest_bytes", "input_bytes"):
        object.__setattr__(source_run, name, getattr(run, name))
    object.__setattr__(
        source_run,
        "result",
        ParseResult(cases=run.result.cases, quarantines=(quarantine,)),
    )
    source_batch = prepare_review_corpus(
        (source_run,), ingestion_version="ingestion-v1"
    )
    draft = create_resolution_draft(
        release_id=RELEASE_ID,
        registry_sha256=source_batch.registry.fingerprint_sha256,
        manifest_sha256=source_batch.manifest_sha256,
        raw_authority_sha256=source_batch.raw_authority_sha256,
        parser_authority_sha256=source_batch.parser_authority_sha256,
        parser_quarantines_bytes=source_batch.parser_quarantines_bytes,
        parser_quarantines_sha256=source_batch.parser_quarantines_sha256,
    )
    draft_authority, _ = _load_authority(tmp_path / "draft.json", draft)
    resolved_raw = append_resolution_event(
        draft_authority,
        occurrence_id=draft_authority.resolutions[0].occurrence_id,
        disposition="confirmed_noncase",
        annotations=(),
        actor_id="uid:501:reviewer-a",
        event_id="event-0001",
        occurred_at="2026-08-11T01:00:00Z",
    )
    authority, authority_sha256 = _load_authority(
        tmp_path / "resolved.json", resolved_raw
    )
    resolved_result = ParseResult(cases=run.result.cases)
    monkeypatch.setattr(
        staging_module,
        "reparse_with_resolution",
        lambda *_args, **_kwargs: (resolved_result,),
    )

    batch = prepare_resolved_review_corpus(
        (source_run,),
        (resolved_result,),
        source_batch=source_batch,
        resolution_authority=authority,
        expected_resolution_authority_sha256=authority_sha256,
        ingestion_version="ingestion-v1",
    )

    assert batch.quarantine_count == 0
    assert batch.parser_quarantines_bytes == b""
    assert batch.raw_authority_sha256 == source_batch.raw_authority_sha256
    assert batch.parser_authority_sha256 != source_batch.parser_authority_sha256
    assert batch.resolution_authority_sha256 == authority_sha256
    assert batch.resolution_authority_bytes == resolved_raw
    assert batch.registry.fingerprint_sha256 != source_batch.registry.fingerprint_sha256
    assert batch.cases == source_batch.cases

    nonempty_root = tmp_path / "nonempty-release"
    nonempty_root.mkdir()
    (nonempty_root / "old-review-state").write_bytes(b"must-not-be-carried")
    with pytest.raises(StagingError, match="staging_write_invalid"):
        write_review_package(nonempty_root, release_id=RELEASE_ID, batch=batch)
    assert not (nonempty_root / "review").exists()

    with pytest.raises(StagingError, match="staging_input_invalid"):
        prepare_resolved_review_corpus(
            (source_run,),
            (resolved_result,),
            source_batch=source_batch,
            resolution_authority=authority,
            expected_resolution_authority_sha256="0" * 64,
            ingestion_version="ingestion-v1",
        )
    with pytest.raises(StagingError, match="staging_input_invalid"):
        prepare_resolved_review_corpus(
            (source_run,),
            (ParseResult(),),
            source_batch=source_batch,
            resolution_authority=authority,
            expected_resolution_authority_sha256=authority_sha256,
            ingestion_version="ingestion-v1",
        )
    unresolved_authority, unresolved_sha256 = _load_authority(
        tmp_path / "unresolved.json", draft
    )
    with pytest.raises(StagingError, match="staging_input_invalid"):
        prepare_resolved_review_corpus(
            (source_run,),
            (resolved_result,),
            source_batch=source_batch,
            resolution_authority=unresolved_authority,
            expected_resolution_authority_sha256=unresolved_sha256,
            ingestion_version="ingestion-v1",
        )

    output_root = tmp_path / "new-release"
    output_root.mkdir()
    package = write_review_package(output_root, release_id=RELEASE_ID, batch=batch)
    evidence = json.loads((package / "ingestion-evidence.json").read_bytes())
    summary = json.loads((package / "summary.json").read_bytes())
    sampling = json.loads((package / "sampling-authority.json").read_bytes())
    assert (package / "parser-quarantine-resolutions.json").read_bytes() == resolved_raw
    assert not (package / "parser-quarantines.jsonl").exists()
    assert evidence["schema_version"] == "sen-qa-ingestion-evidence/v4"
    assert evidence["resolution_authority_sha256"] == authority_sha256
    assert evidence["resolved_from_registry_sha256"] == (
        source_batch.registry.fingerprint_sha256
    )
    assert evidence["resolved_from_parser_authority_sha256"] == (
        source_batch.parser_authority_sha256
    )
    assert evidence["resolved_from_parser_quarantines_sha256"] == (
        source_batch.parser_quarantines_sha256
    )
    assert summary["schema_version"] == "sen-qa-review-package/v4"
    assert summary["resolution_authority_sha256"] == authority_sha256
    assert sampling["inventory"]["registry_sha256"] == (
        batch.registry.fingerprint_sha256
    )
    assert sampling["inventory"]["parser_authority_sha256"] == (
        batch.parser_authority_sha256
    )
    with ReviewStore(
        package / "review.sqlite3", canonical_registry=batch.registry
    ) as store:
        assert store.get(batch.cases[0].case_id).review_status == "needs_review"
    valid, _ = staging_module._review_package_authority_sha256(
        package,
        evidence_raw=(package / "ingestion-evidence.json").read_bytes(),
        documents_raw=(package / "documents.json").read_bytes(),
        expected_release_id=RELEASE_ID,
        expected_registry_sha256=batch.registry.fingerprint_sha256,
    )
    assert valid
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
    attestation_path = export_review_ready(
        package,
        release_id=RELEASE_ID,
        expected_registry_sha256=batch.registry.fingerprint_sha256,
    )
    attestation = json.loads(attestation_path.read_bytes())
    assert attestation["schema_version"] == "sen-qa-review-ready-attestation/v3"
    assert attestation["resolution_authority_sha256"] == authority_sha256
    (package / "parser-quarantine-resolutions.json").unlink()
    invalid, _ = staging_module._review_package_authority_sha256(
        package,
        evidence_raw=(package / "ingestion-evidence.json").read_bytes(),
        documents_raw=(package / "documents.json").read_bytes(),
        expected_release_id=RELEASE_ID,
        expected_registry_sha256=batch.registry.fingerprint_sha256,
    )
    assert not invalid


def test_real_corrected_annotations_reparse_and_restage_mixed_corpus(
    tmp_path: Path,
) -> None:
    unaffected = _native_run(2022, include_case=True)
    affected_page = _unlabeled_hierarchy_page(reviewed_case_geometry=False).model_copy(
        update={
            "doc_id": "sen-qa-2023",
            "source_sha256": _AUTHORITY_SOURCE_SHA256,
        }
    )
    affected_document = _authority_document()
    manifest_bytes = (
        json.dumps(
            SourceManifest(
                documents=tuple(
                    affected_document if year == 2023 else _native_document(year)
                    for year in range(2020, 2026)
                )
            ).model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    object.__setattr__(unaffected, "manifest_bytes", manifest_bytes)
    assert parse_pages((affected_page,), edition_year=2023).cases == ()
    quarantine_spans = tuple(
        SourceSpan(
            pdf_page_index=affected_page.pdf_page_index,
            page_label=affected_page.page_label,
            bbox=line.bbox,
            text_sha256=line.raw_text_sha256,
        )
        for line in affected_page.lines
    )
    old_affected_result = ParseResult(
        quarantines=(
            BoundaryQuarantine(
                location_id="loc-" + "8" * 32,
                page_ids=(affected_page.pdf_page_index,),
                source_spans=quarantine_spans,
                span_count=len(quarantine_spans),
            ),
        )
    )
    affected = object.__new__(VerifiedParseRun)
    object.__setattr__(affected, "document", affected_document)
    object.__setattr__(affected, "records", ())
    object.__setattr__(affected, "result", old_affected_result)
    object.__setattr__(affected, "pages", (affected_page,))
    object.__setattr__(affected, "manifest_bytes", manifest_bytes)
    object.__setattr__(affected, "input_bytes", b"reviewed-2023-input\n")
    runs = (unaffected, affected)
    source_batch = prepare_review_corpus(runs, ingestion_version="ingestion-v1")
    lock = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint="sha256:" + "2" * 64,
        vision_2025_runtime_fingerprint="sha256:" + "3" * 64,
    )
    lock_raw = canonical_ocr_authority_bytes(lock)
    source_batch = staging_module._bind_ocr_authority(
        source_batch,
        (
            lock,
            lock_raw,
            hashlib.sha256(lock_raw).hexdigest(),
            lock.self_sha256,
        ),
    )
    draft_raw = create_resolution_draft(
        release_id=RELEASE_ID,
        registry_sha256=source_batch.registry.fingerprint_sha256,
        manifest_sha256=source_batch.manifest_sha256,
        raw_authority_sha256=source_batch.raw_authority_sha256,
        parser_authority_sha256=source_batch.parser_authority_sha256,
        parser_quarantines_bytes=source_batch.parser_quarantines_bytes,
        parser_quarantines_sha256=source_batch.parser_quarantines_sha256,
    )
    draft, _ = _load_authority(tmp_path / "real-draft.json", draft_raw)
    occurrence = draft.resolutions[0]
    roles = ("domain", "part", "case_no", "title", "question", "answer")
    annotations = tuple(
        ResolutionAnnotation(
            role=role,
            source_span=ResolutionSourceSpan(
                pdf_page_index=line_page.pdf_page_index,
                page_label=line_page.page_label,
                bbox=line.bbox,
                text_sha256=line.raw_text_sha256,
            ),
        )
        for role, line_page, line in (
            (role, affected_page, line)
            for role, line in zip(roles, affected_page.lines, strict=True)
        )
    )
    assert {item.source_span for item in annotations} == set(occurrence.source_spans)
    resolved_raw = append_resolution_event(
        draft,
        occurrence_id=occurrence.occurrence_id,
        disposition="corrected",
        annotations=annotations,
        actor_id="uid:501:reviewer-a",
        event_id="event-real-0001",
        occurred_at="2026-08-11T02:00:00Z",
    )
    authority, authority_sha256 = _load_authority(
        tmp_path / "real-resolved.json", resolved_raw
    )
    affected_resolved = staging_module.reparse_with_resolution(
        ((affected_page,),),
        authority=authority,
        expected_registry_sha256=source_batch.registry.fingerprint_sha256,
        expected_manifest_sha256=source_batch.manifest_sha256,
        expected_raw_authority_sha256=source_batch.raw_authority_sha256,
        expected_parser_authority_sha256=source_batch.parser_authority_sha256,
        parser_quarantines_bytes=source_batch.parser_quarantines_bytes,
        expected_parser_quarantines_sha256=source_batch.parser_quarantines_sha256,
    )[0]

    batch = prepare_resolved_review_corpus(
        runs,
        (unaffected.result, affected_resolved),
        source_batch=source_batch,
        resolution_authority=authority,
        expected_resolution_authority_sha256=authority_sha256,
        ingestion_version="ingestion-v1",
    )

    assert batch.quarantine_count == 0
    assert {case.doc_id for case in batch.cases} == {
        unaffected.document.doc_id,
        affected_document.doc_id,
    }
    reviewed_case = next(
        case for case in batch.cases if case.doc_id == affected_document.doc_id
    )
    assert reviewed_case.source_spans == affected_resolved.cases[0].source_spans
    assert batch.parser_authority_sha256.startswith(tuple("0123456789abcdef"))
    assert batch.parser_authority_sha256 != source_batch.parser_authority_sha256
    assert batch.registry.fingerprint_sha256 != source_batch.registry.fingerprint_sha256
    output_root = tmp_path / "real-output"
    output_root.mkdir()
    package = write_review_package(output_root, release_id=RELEASE_ID, batch=batch)
    sampling = json.loads((package / "sampling-authority.json").read_bytes())
    assert sampling["inventory"]["parser_authority_sha256"] == (
        batch.parser_authority_sha256
    )
    assert sampling["inventory"]["registry_sha256"] == (
        batch.registry.fingerprint_sha256
    )
