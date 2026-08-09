from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

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
    StagingError,
    export_review_ready,
    prepare_review_batch,
    prepare_review_corpus_from_artifacts,
    write_review_package,
)
from src.corpus.storage import (
    GENESIS_ISSUANCE_AUTHORITY_SHA256,
    initialize_issuance_registry,
)
from src.ingestion.extract_common import LayoutEvidence
from src.ingestion.manifest import PageNumberingPolicy, PageSizeProfile, SourceDocument
from src.ingestion.parse_common import (
    ParsedCaseCandidate,
    ParseResult,
    ParserLine,
    ParserPage,
    RoleFragment,
)
from src.ingestion.parse_metadata import VerifiedParseRun
from src.ingestion.review import ReviewStore

RELEASE_ID = "corpus-20250808123456-deadbeef"
_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


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


def test_review_package_is_owner_only_no_clobber_and_enqueues_every_case(
    tmp_path: Path,
) -> None:
    result, pages = _parsed()
    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )

    package = write_review_package(tmp_path, release_id=RELEASE_ID, batch=batch)

    assert package == tmp_path / "review"
    assert (package.stat().st_mode & 0o777) == 0o700
    assert ((package / "registry.json").stat().st_mode & 0o777) == 0o600
    documents = json.loads((package / "documents.json").read_text())
    evidence = json.loads((package / "ingestion-evidence.json").read_text())
    assert documents["documents"][0]["doc_id"] == "fixture-2023"
    assert documents["schema_version"] == "sen-qa-review-documents/v1"
    assert evidence["document_page_counts"] == {
        "fixture-2023": {"failed": 0, "quarantined": 0, "succeeded": 1}
    }
    assert evidence["manifest_sha256"] == batch.manifest_sha256
    assert evidence["parser_quarantine_count"] == 0
    assert evidence["schema_version"] == "sen-qa-ingestion-evidence/v1"
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
) -> None:
    result, pages = _parsed()
    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )
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
    document = _document()
    result, pages = _parsed()
    input_root = tmp_path / "raw-pages"
    input_dir = input_root / "ocr-2023"
    input_dir.mkdir(parents=True)
    input_path = input_dir / f"{document.doc_id}.jsonl"
    input_path.write_bytes(b"{}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}\n")
    run = object.__new__(VerifiedParseRun)
    object.__setattr__(run, "document", document)
    object.__setattr__(run, "records", ())
    object.__setattr__(run, "result", result)
    object.__setattr__(run, "pages", pages)
    object.__setattr__(run, "manifest_bytes", b"{}\n")
    object.__setattr__(run, "input_bytes", b"{}\n")
    monkeypatch.setattr(staging_module, "load_manifest", lambda _path: (document,))
    monkeypatch.setattr(
        staging_module, "build_parse_run", lambda *_args, **_kwargs: run
    )

    batch = prepare_review_corpus_from_artifacts(
        input_root,
        manifest_path=manifest_path,
        ingestion_version="ingestion-v1",
        expected_image_digest="sha256:" + "d" * 64,
    )

    assert len(batch.documents) == 1
    assert len(batch.cases) == 1
    assert batch.quarantine_count == 0
    assert batch.envelopes[0].parser_authority_sha256 == batch.parser_authority_sha256


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
) -> None:
    result, pages = _parsed()
    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="ingestion-v1",
    )
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
    assert (attestation.stat().st_mode & 0o777) == 0o600
    with pytest.raises(StagingError, match="review_attestation_exists"):
        export_review_ready(
            package,
            release_id=RELEASE_ID,
            expected_registry_sha256=batch.registry.fingerprint_sha256,
        )


def test_review_ready_package_builds_one_atomic_canonical_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, pages = _parsed()
    batch = prepare_review_batch(
        document=_document(),
        result=result,
        pages=pages,
        parser_authority_sha256=hashlib.sha256(b"parser").hexdigest(),
        raw_authority_sha256=hashlib.sha256(b"raw").hexdigest(),
        ingestion_version="image-" + "e" * 64,
    )
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
