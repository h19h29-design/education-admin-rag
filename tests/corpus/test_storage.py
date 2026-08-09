"""Behavior contracts for canonical SQLite and deterministic JSONL storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.corpus.build import BuildError, build_canonical_bundle
from src.corpus.chunking import (
    BGE_M3_REQUIRED_PATHS,
    TOKENIZER_REQUIRED_PATHS,
    RoleSource,
    TokenizerContract,
    VerifiedChunkSet,
    build_chunks,
    role_source_manifest_bytes,
    validate_embedding_model_lock,
    verify_role_sources,
)
from src.corpus.models import (
    Case,
    CaseRelation,
    Document,
    DocumentPageCounts,
    IngestionRun,
    SourceSpan,
)
from src.corpus.relations import canonical_case_sha256
from src.corpus.storage import (
    GENESIS_ISSUANCE_AUTHORITY_SHA256,
    CanonicalStorageBatch,
    StorageError,
    StorageProjectionReceipt,
    VerifiedPromotionEnvelope,
    acquire_issuance_lease,
    connect_canonical_storage,
    export_canonical_jsonl,
    initialize_issuance_registry,
    load_promotion_envelope,
    load_review_decision_snapshot,
    read_issuance_head,
    write_canonical_storage,
)
from src.corpus.storage import (
    canonical_content_sha256 as storage_content_sha256,
)
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
)


def _document() -> Document:
    return Document(
        doc_id="sen-qa-2025",
        edition_year=2025,
        title="2025 교육행정 질의답변집",
        publisher="교육청",
        registration_no=None,
        source_period_start=None,
        source_period_end=None,
        source_filename="sen-qa-2025.pdf",
        sha256="a" * 64,
        pdf_page_count=100,
        extraction_method="ocr",
        source_dpi=300,
        public_url=None,
        redistribution_status="approved",
        access_level="staff",
        page_numbering_rule="pdf_page_index",
        ingestion_version="ingestion-v1",
    )


def _case(case_no: str = "1") -> Case:
    raw = f"질문 {case_no} 답변 {case_no}"
    span = SourceSpan(
        pdf_page_index=int(case_no) + 10,
        page_label=str(int(case_no) + 10),
        bbox=(10.0, 20.0, 500.0, 80.0),
        text_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    return Case(
        case_id=f"senqa-2025-contract-contract-general-{case_no}",
        legacy_ids=(),
        doc_id="sen-qa-2025",
        case_type="qa",
        domain="계약",
        part="계약 일반",
        subtopic=None,
        case_no=case_no,
        title_raw=f"계약 질의 {case_no}",
        title_normalized=f"계약 질의 {case_no}",
        question=f"질문 {case_no}",
        answer=f"답변 {case_no}",
        facts=None,
        basis_text=None,
        law_ref_ids=(),
        source_spans=(span,),
        extraction_source="ocr",
        extraction_confidence=0.99,
        critical_field_review="verified",
        pii_class="none",
        anonymization_status="not_required",
        currency_status="historical_reference",
        search_eligible=True,
        answer_eligible=True,
        review_status="approved",
    )


def _restricted_case(case_no: str = "3") -> Case:
    case = _case(case_no)
    return case.model_copy(
        update={
            "answer_eligible": False,
            "critical_field_review": "pending",
            "pii_class": "restricted",
            "review_status": "rejected",
            "search_eligible": False,
        }
    )


class _FakeTokenizer:
    def __init__(self, contract: TokenizerContract) -> None:
        self.model_name = contract.model_name
        self.revision = contract.revision
        self.model_lock_sha256 = contract.model_lock_sha256
        self.runtime_fingerprint_sha256 = contract.runtime_fingerprint_sha256

    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())

    def detokenize(self, tokens: tuple[str, ...]) -> str:
        return " ".join(tokens)

    def token_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        offsets: list[tuple[int, int]] = []
        cursor = 0
        for token in text.split():
            start = text.index(token, cursor)
            end = start + len(token)
            offsets.append((start, end))
            cursor = end
        return tuple(offsets)


def _contract() -> TokenizerContract:
    return TokenizerContract(
        model_name="BAAI/bge-m3",
        revision="5617a9f61b028005a4858fdac845db406aefb181",
        model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
        runtime_fingerprint_sha256="d" * 64,
    )


def _embedding_cache_bytes(path: str) -> bytes:
    return f"storage-fixture:{path}".encode()


def _embedding_lock_payload() -> dict[str, object]:
    revision = "5617a9f61b028005a4858fdac845db406aefb181"
    return {
        "schema_version": 1,
        "language": "korean",
        "packages": {},
        "models": [],
        "embedding_models": [
            {
                "repo_id": "BAAI/bge-m3",
                "revision": revision,
                "files": [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(
                            _embedding_cache_bytes(path)
                        ).hexdigest(),
                        "size": len(_embedding_cache_bytes(path)),
                        "source_url": (
                            "https://huggingface.co/BAAI/bge-m3/resolve/"
                            f"{revision}/{path}"
                        ),
                    }
                    for path in BGE_M3_REQUIRED_PATHS
                ],
            }
        ],
    }


_EMBEDDING_LOCK = validate_embedding_model_lock(_embedding_lock_payload())


def _write_tokenizer_cache(root: Path) -> None:
    for relative in TOKENIZER_REQUIRED_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_embedding_cache_bytes(relative))


def _verified_chunks(case: Case) -> tuple[VerifiedChunkSet, str]:
    raw = f"질문 {case.case_no} 답변 {case.case_no}"
    sources = (
        RoleSource("question", case.question or "", raw, 0),
        RoleSource("answer", case.answer or "", raw, 0),
    )
    fingerprint = hashlib.sha256(role_source_manifest_bytes(case, sources)).hexdigest()
    verified_sources = verify_role_sources(
        case,
        sources,
        expected_authority_sha256=fingerprint,
    )
    contract = _contract()
    return (
        build_chunks(
            case,
            verified_sources,
            tokenizer=_FakeTokenizer(contract),
            contract=contract,
            expected_role_authority_sha256=fingerprint,
        ),
        fingerprint,
    )


def _run(case: Case, release_id: str, *, approved_by: str) -> IngestionRun:
    return IngestionRun(
        run_id="run-storage-test",
        release_id=release_id,
        started_at=datetime(2025, 8, 9, tzinfo=UTC),
        ended_at=datetime(2025, 8, 9, 0, 1, tzinfo=UTC),
        manifest_version="manifest-v1",
        source_sha256s=("a" * 64,),
        extractor_version="extractor-v1",
        ocr_engine_version="paddleocr-3.7.0",
        ocr_model_version="ocr-lock-v1",
        container_image="sha256:" + "b" * 64,
        normalizer_version="normalizer-v1",
        parser_version="parser-v1",
        schema_version="canonical-v1",
        document_page_counts={
            "sen-qa-2025": DocumentPageCounts(succeeded=100, quarantined=0, failed=0)
        },
        created_case_ids=(case.case_id,),
        approved_by=approved_by,
    )


def _promotion_envelope(
    case: Case,
    *,
    role_authority_sha256: str | None,
    corrections: tuple[dict[str, object], ...] = (),
) -> VerifiedPromotionEnvelope:
    del role_authority_sha256
    candidate = case.model_copy(
        update={
            "answer_eligible": False,
            "critical_field_review": "pending",
            "review_status": "needs_review",
            "search_eligible": False,
        }
    )
    raw_text = f"질문 {case.case_no} 답변 {case.case_no}"
    role_sources = [
        {
            "raw_text": raw_text,
            "role": role,
            "source_span_index": 0,
            "table_evidence_sha256": None,
            "table_header": None,
            "table_header_raw_text": None,
            "table_header_source_span_index": None,
            "text": text,
        }
        for role, text in (("question", case.question), ("answer", case.answer))
        if text is not None
    ]
    raw = (
        json.dumps(
            {
                "candidate_case": candidate.model_dump(mode="json"),
                "corrections": list(corrections),
                "parser_authority_sha256": "7" * 64,
                "raw_authority_sha256": "8" * 64,
                "role_sources": role_sources,
                "schema_version": "sen-qa-promotion-envelope/v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return load_promotion_envelope(
        raw,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _review_snapshot(
    cases: tuple[Case, ...],
    envelopes: tuple[VerifiedPromotionEnvelope, ...],
) -> tuple[object, str, object]:
    envelope_by_case = {item.candidate_case.case_id: item for item in envelopes}
    registry = CanonicalReviewRegistry.create(
        cases=(
            ReviewReference(
                case_id=case.case_id,
                content_sha256=envelope_by_case[case.case_id].fingerprint_sha256,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=case.source_spans[0].pdf_page_index,
                        bbox=case.source_spans[0].bbox,
                        reason_code="human-review-required",
                    ),
                ),
            )
            for case in cases
        )
    )
    registry_raw = registry.to_bytes()
    verified_registry = CanonicalReviewRegistry.from_bytes(
        registry_raw,
        expected_sha256=hashlib.sha256(registry_raw).hexdigest(),
    )
    with (
        tempfile.TemporaryDirectory() as temporary,
        ReviewStore(
            Path(temporary) / "review.sqlite3",
            canonical_registry=verified_registry,
            clock=lambda: datetime(2025, 8, 8, tzinfo=UTC),
        ) as store,
    ):
        for case in cases:
            promotion = envelope_by_case[case.case_id].fingerprint_sha256
            store.enqueue(
                case.case_id,
                content_sha256=promotion,
                reason="human-review-required",
            )
            if case.review_status == "rejected":
                store.reject(
                    case.case_id,
                    reviewer_id="reviewer-reject",
                    reviewed_content_sha256=promotion,
                    reason="invalid_layout",
                )
                continue
            store.verify_critical_fields(
                case.case_id,
                reviewer_id="reviewer-critical",
                reviewed_content_sha256=promotion,
                reason="fields_checked",
            )
            store.approve_search(
                case.case_id,
                reviewer_id="reviewer-critical",
                reviewed_content_sha256=promotion,
                reason="search_checked",
            )
            if case.review_status == "approved":
                store.approve_answer(
                    case.case_id,
                    reviewer_id="reviewer-answer",
                    reviewed_content_sha256=promotion,
                    reason="answer_checked",
                    content_verified=True,
                    basis_verified=True,
                    privacy_verified=True,
                )
        raw = store.export_decision_snapshot()
    fingerprint = hashlib.sha256(raw).hexdigest()
    return (
        load_review_decision_snapshot(raw, expected_sha256=fingerprint),
        fingerprint,
        verified_registry,
    )


def _batch(
    case: Case | None = None,
    *,
    release_id: str = "corpus-20250809000000-1234abcd",
    corrections: tuple[dict[str, object], ...] = (),
) -> CanonicalStorageBatch:
    approved = case or _case()
    chunk_sets: tuple[VerifiedChunkSet, ...] = ()
    role_authority: str | None = None
    if approved.search_eligible:
        chunk_set, role_authority = _verified_chunks(approved)
        chunk_sets = (chunk_set,)
    envelope = _promotion_envelope(
        approved,
        role_authority_sha256=role_authority,
        corrections=corrections,
    )
    review_snapshot, _, review_registry = _review_snapshot(
        (approved,),
        (envelope,),
    )
    return CanonicalStorageBatch(
        release_id=release_id,
        documents=(_document(),),
        cases=(approved,),
        chunk_sets=chunk_sets,
        law_refs=(),
        relations=(),
        relation_approval_sha256s={},
        ingestion_runs=(
            _run(
                approved,
                release_id,
                approved_by=("review-snapshot:" + review_snapshot.fingerprint_sha256),
            ),
        ),
        tokenizer_contract=_contract(),
        promotion_envelopes=(envelope,),
        review_registry=review_registry,
        review_decision_snapshot=review_snapshot,
    )


def _registry(path: Path) -> object:
    initialize_issuance_registry(
        path,
        expected_genesis_sha256=GENESIS_ISSUANCE_AUTHORITY_SHA256,
    )
    return read_issuance_head(path)


def _write(
    database: Path,
    batch: CanonicalStorageBatch,
    *,
    registry_path: Path,
    head: object,
    review_snapshot_sha256: str,
    chunk_set_sha256s: dict[str, str],
) -> None:
    with acquire_issuance_lease(
        registry_path,
        expected_generation=head.generation,  # type: ignore[attr-defined]
        expected_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
        expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
    ) as lease:
        receipt = write_canonical_storage(
            database,
            batch,
            issuance_lease=lease,
            expected_review_decision_snapshot_sha256=review_snapshot_sha256,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_set_sha256s,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
        )
        bundle_path = _materialize_published_bundle(database, receipt, batch)
        bound_receipt = lease.bind_published_bundle(
            receipt,
            bundle_path=bundle_path,
        )
        lease.commit_published_bundle(receipt=bound_receipt)


def _pins(batch: CanonicalStorageBatch) -> tuple[str, dict[str, str]]:
    return (
        batch.review_decision_snapshot.fingerprint_sha256,
        {
            chunk_set.chunks[0].case_id: chunk_set.binding_sha256
            for chunk_set in batch.chunk_sets
        },
    )


def _bundle_manifest_bytes(
    receipt: StorageProjectionReceipt,
    batch: CanonicalStorageBatch,
    *,
    exports: dict[str, str] | None = None,
) -> bytes:
    if exports is None:
        raise AssertionError("published export hashes are required")
    return (
        json.dumps(
            {
                "canonical_content_sha256": storage_content_sha256(exports),
                "database_sha256": receipt.database_sha256,
                "exports": exports,
                "predecessor_bundle_sha256": receipt.predecessor_bundle_sha256,
                "projection_sha256": receipt.projection_sha256,
                "release_id": batch.release_id,
                "schema_version": "sen-qa-canonical-bundle/v1",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _materialize_published_bundle(
    database: Path,
    receipt: StorageProjectionReceipt,
    batch: CanonicalStorageBatch,
) -> Path:
    bundle = database.parent / f".{database.name}.bundle"
    bundle.mkdir()
    shutil.copyfile(database, bundle / "canonical.sqlite3")
    exports = export_canonical_jsonl(database, bundle / "jsonl")
    (bundle / "manifest.json").write_bytes(
        _bundle_manifest_bytes(receipt, batch, exports=exports)
    )
    return bundle


def _build(
    tmp_path: Path,
    batch: CanonicalStorageBatch,
    *,
    registry_name: str,
    output_name: str,
) -> object:
    registry_path = tmp_path / registry_name
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    model_root = tmp_path / f"{output_name}-tokenizer-cache"
    _write_tokenizer_cache(model_root)
    return build_canonical_bundle(
        tmp_path / output_name,
        tmp_path / f"{output_name}-diagnostics",
        registry_path,
        batch,
        expected_generation=head.generation,  # type: ignore[attr-defined]
        expected_issuance_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
        expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
        expected_review_decision_snapshot_sha256=review_pin,
        expected_registry_sha256=batch.review_registry.fingerprint_sha256,
        expected_chunk_set_sha256s=chunk_pins,
        expected_relation_approval_sha256s={},
        expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
        expected_runtime_fingerprint_sha256="d" * 64,
        embedding_model_lock=_EMBEDDING_LOCK,
        embedding_model_root=model_root,
    )


def _replace_run(batch: CanonicalStorageBatch, **changes: object) -> None:
    object.__setattr__(
        batch,
        "ingestion_runs",
        (batch.ingestion_runs[0].model_copy(update=changes),),
    )


def test_storage_schema_enforces_wal_foreign_keys_and_immutable_ids(
    tmp_path: Path,
) -> None:
    """Catches canonical children or issued IDs escaping database constraints."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    database = tmp_path / "canonical.sqlite3"
    batch = _batch()
    review_pin, chunk_pins = _pins(batch)
    _write(
        database,
        batch,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=review_pin,
        chunk_set_sha256s=chunk_pins,
    )

    with connect_canonical_storage(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "documents",
            "cases",
            "source_spans",
            "chunks",
            "chunk_source_spans",
            "law_refs",
            "case_relations",
            "corrections",
            "review_events",
            "issued_case_ids",
            "ingestion_runs",
            "tokenizer_contract",
        }.issubset(tables)

        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "DELETE FROM issued_case_ids WHERE case_id = ?",
                (_case().case_id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO chunks(chunk_id,case_id,role,sequence,payload_json) "
                "VALUES('bad','missing','answer',1,'{}')"
            )

    raw = sqlite3.connect(database)
    try:
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        attacks = (
            (
                "UPDATE issued_case_ids SET state='retired' WHERE case_id=?",
                (_case().case_id,),
            ),
            ("DELETE FROM cases WHERE case_id=?", (_case().case_id,)),
            (
                "INSERT OR REPLACE INTO issued_case_ids VALUES(?,?,?,?,?,?)",
                (
                    _case().case_id,
                    "active",
                    "corpus-20250809000000-1234abcd",
                    "0" * 64,
                    "0" * 64,
                    None,
                ),
            ),
            (
                (
                    "INSERT INTO issued_case_ids VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(case_id) DO UPDATE SET state='active'"
                ),
                (
                    _case().case_id,
                    "active",
                    "corpus-20250809000000-1234abcd",
                    "0" * 64,
                    "0" * 64,
                    None,
                ),
            ),
            (
                (
                    "INSERT INTO chunks(chunk_id,case_id,role,sequence,payload_json) "
                    "VALUES('orphan','missing','answer',1,'{}')"
                ),
                (),
            ),
        )
        for statement, parameters in attacks:
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement, parameters)
    finally:
        raw.close()


def test_issuance_tracks_current_content_and_append_only_release_ledger(
    tmp_path: Path,
) -> None:
    """Catches deltas comparing against first issuance instead of the predecessor."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    first_case = _case()
    first = _batch(first_case, release_id="corpus-20250809000000-11111111")
    first_pin, first_chunks = _pins(first)
    _write(
        tmp_path / "first.sqlite3",
        first,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=first_pin,
        chunk_set_sha256s=first_chunks,
    )

    changed_case = first_case.model_copy(
        update={"title_raw": "변경된 계약 질의", "title_normalized": "변경된 계약 질의"}
    )
    changed = _batch(changed_case, release_id="corpus-20250810000000-22222222")
    _replace_run(
        changed,
        created_case_ids=(),
        changed_case_ids=(changed_case.case_id,),
    )
    changed_pin, changed_chunks = _pins(changed)
    _write(
        tmp_path / "changed.sqlite3",
        changed,
        registry_path=registry_path,
        head=read_issuance_head(registry_path),
        review_snapshot_sha256=changed_pin,
        chunk_set_sha256s=changed_chunks,
    )

    unchanged = _batch(changed_case, release_id="corpus-20250811000000-33333333")
    _replace_run(unchanged, created_case_ids=(), changed_case_ids=())
    unchanged_pin, unchanged_chunks = _pins(unchanged)
    _write(
        tmp_path / "unchanged.sqlite3",
        unchanged,
        registry_path=registry_path,
        head=read_issuance_head(registry_path),
        review_snapshot_sha256=unchanged_pin,
        chunk_set_sha256s=unchanged_chunks,
    )

    final_head = read_issuance_head(registry_path)
    assert final_head.generation == 3
    with sqlite3.connect(registry_path) as connection:
        issued = connection.execute(
            "SELECT first_content_sha256,current_content_sha256 FROM issued_case_ids"
        ).fetchone()
        releases = connection.execute(
            "SELECT generation,release_id FROM issued_releases ORDER BY generation"
        ).fetchall()
    assert issued == (
        canonical_case_sha256(first_case),
        canonical_case_sha256(changed_case),
    )
    assert releases == [
        (1, first.release_id),
        (2, changed.release_id),
        (3, unchanged.release_id),
    ]


def test_issuance_rejects_duplicate_release_before_publication(tmp_path: Path) -> None:
    """Catches the same release identity being rebound to a different bundle."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    first = _batch(release_id="corpus-20250809000000-44444444")
    first_pin, first_chunks = _pins(first)
    _write(
        tmp_path / "first.sqlite3",
        first,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=first_pin,
        chunk_set_sha256s=first_chunks,
    )

    duplicate = _batch(release_id=first.release_id)
    _replace_run(duplicate, created_case_ids=())
    duplicate_pin, duplicate_chunks = _pins(duplicate)
    duplicate_path = tmp_path / "duplicate.sqlite3"
    with pytest.raises(StorageError, match="release"):
        _write(
            duplicate_path,
            duplicate,
            registry_path=registry_path,
            head=read_issuance_head(registry_path),
            review_snapshot_sha256=duplicate_pin,
            chunk_set_sha256s=duplicate_chunks,
        )
    assert not duplicate_path.exists()


def test_issuance_rejects_same_name_noop_trigger_replacement(tmp_path: Path) -> None:
    """Catches schema-name checks accepting a raw-write guard with no protection."""
    registry_path = tmp_path / "issuance.sqlite3"
    _registry(registry_path)
    with sqlite3.connect(registry_path) as connection:
        connection.execute("DROP TRIGGER guard_issued_case_update")
        connection.execute(
            "CREATE TRIGGER guard_issued_case_update BEFORE UPDATE "
            "ON issued_case_ids BEGIN SELECT 1; END"
        )

    with pytest.raises(StorageError, match="schema"):
        read_issuance_head(registry_path)


def test_issuance_receipt_cannot_be_rebound_after_storage_publication(
    tmp_path: Path,
) -> None:
    """Catches committing a projection other than the one written to the bundle."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    batch = _batch(release_id="corpus-20250809000000-55555555")
    review_pin, chunk_pins = _pins(batch)
    with acquire_issuance_lease(
        registry_path,
        expected_generation=head.generation,  # type: ignore[attr-defined]
        expected_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
        expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
    ) as lease:
        database = tmp_path / "canonical.sqlite3"
        receipt = write_canonical_storage(
            database,
            batch,
            issuance_lease=lease,
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
        )
        bundle_path = _materialize_published_bundle(database, receipt, batch)
        object.__setattr__(receipt, "projection_sha256", "0" * 64)
        with pytest.raises(StorageError, match="bundle receipt"):
            lease.bind_published_bundle(
                receipt,
                bundle_path=bundle_path,
            )


def test_issuance_cannot_bind_a_manifest_without_a_published_bundle(
    tmp_path: Path,
) -> None:
    """Catches advancing issuance from caller-authored bytes with no disk bundle."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    batch = _batch(release_id="corpus-20250809000000-55555556")
    review_pin, chunk_pins = _pins(batch)
    with acquire_issuance_lease(
        registry_path,
        expected_generation=head.generation,  # type: ignore[attr-defined]
        expected_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
        expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
    ) as lease:
        receipt = write_canonical_storage(
            tmp_path / "canonical.sqlite3",
            batch,
            issuance_lease=lease,
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
        )
        with pytest.raises(StorageError, match="published bundle"):
            lease.bind_published_bundle(
                receipt,
                bundle_path=tmp_path / "missing-bundle",
            )


def test_issuance_rejects_published_bundle_with_tampered_export(
    tmp_path: Path,
) -> None:
    """Catches a manifest remaining valid after a JSONL artifact is replaced."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    batch = _batch(release_id="corpus-20250809000000-55555557")
    review_pin, chunk_pins = _pins(batch)
    database = tmp_path / "canonical.sqlite3"
    with acquire_issuance_lease(
        registry_path,
        expected_generation=head.generation,  # type: ignore[attr-defined]
        expected_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
        expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
    ) as lease:
        receipt = write_canonical_storage(
            database,
            batch,
            issuance_lease=lease,
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
        )
        bundle_path = _materialize_published_bundle(database, receipt, batch)
        with (bundle_path / "jsonl" / "cases.jsonl").open("ab") as handle:
            handle.write(b"{}\n")
        with pytest.raises(StorageError, match="published bundle"):
            lease.bind_published_bundle(receipt, bundle_path=bundle_path)

    assert read_issuance_head(registry_path).generation == 0


def test_review_snapshot_accepts_direct_machine_reject(tmp_path: Path) -> None:
    """Catches rejecting official ReviewStore bytes that do not start with enqueue."""
    case = _restricted_case()
    envelope = _promotion_envelope(case, role_authority_sha256=None)
    registry = CanonicalReviewRegistry.create(
        cases=(
            ReviewReference(
                case_id=case.case_id,
                content_sha256=envelope.fingerprint_sha256,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=case.source_spans[0].pdf_page_index,
                        bbox=case.source_spans[0].bbox,
                        reason_code="human-review-required",
                    ),
                ),
            ),
        )
    )
    raw_registry = registry.to_bytes()
    verified_registry = CanonicalReviewRegistry.from_bytes(
        raw_registry,
        expected_sha256=hashlib.sha256(raw_registry).hexdigest(),
    )
    with ReviewStore(
        tmp_path / "direct-reject.sqlite3",
        canonical_registry=verified_registry,
    ) as store:
        store.register_candidate(
            case.case_id,
            content_sha256=envelope.fingerprint_sha256,
        )
        store.reject(
            case.case_id,
            reviewer_id="reviewer-reject",
            reviewed_content_sha256=envelope.fingerprint_sha256,
            reason="invalid_layout",
            expected_state="machine_extracted",
        )
        raw = store.export_decision_snapshot()

    verified = load_review_decision_snapshot(
        raw,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert verified.cases[0].events[0]["action"] == "reject"
    assert verified.cases[0].review_record["review_status"] == "rejected"


def test_promotion_envelope_rejects_terminal_self_approval() -> None:
    """Catches a terminal Case being used as its own human-review authority."""
    case = _case()
    envelope = _promotion_envelope(case, role_authority_sha256="1" * 64)
    payload = json.loads(envelope.canonical_bytes)
    payload["candidate_case"] = case.model_dump(mode="json")
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    with pytest.raises(StorageError, match="promotion envelope"):
        load_promotion_envelope(
            raw,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_storage_rejects_review_registry_with_unrelated_source_location(
    tmp_path: Path,
) -> None:
    """Catches an approved digest carrying a swapped page or bbox citation anchor."""
    batch = _batch()
    case = batch.cases[0]
    envelope = batch.promotion_envelopes[0]
    bad_registry = CanonicalReviewRegistry.create(
        cases=(
            ReviewReference(
                case_id=case.case_id,
                content_sha256=envelope.fingerprint_sha256,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=case.source_spans[0].pdf_page_index,
                        bbox=(11.0, 20.0, 500.0, 80.0),
                        reason_code="human-review-required",
                    ),
                ),
            ),
        )
    )
    registry_raw = bad_registry.to_bytes()
    verified_registry = CanonicalReviewRegistry.from_bytes(
        registry_raw,
        expected_sha256=hashlib.sha256(registry_raw).hexdigest(),
    )
    with ReviewStore(
        tmp_path / "bad-location-review.sqlite3",
        canonical_registry=verified_registry,
        clock=lambda: datetime(2025, 8, 8, tzinfo=UTC),
    ) as store:
        store.enqueue(
            case.case_id,
            content_sha256=envelope.fingerprint_sha256,
            reason="human-review-required",
        )
        store.verify_critical_fields(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=envelope.fingerprint_sha256,
            reason="fields_checked",
        )
        store.approve_search(
            case.case_id,
            reviewer_id="reviewer-critical",
            reviewed_content_sha256=envelope.fingerprint_sha256,
            reason="search_checked",
        )
        store.approve_answer(
            case.case_id,
            reviewer_id="reviewer-answer",
            reviewed_content_sha256=envelope.fingerprint_sha256,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )
        snapshot_raw = store.export_decision_snapshot()
    snapshot_sha = hashlib.sha256(snapshot_raw).hexdigest()
    object.__setattr__(
        batch,
        "review_decision_snapshot",
        load_review_decision_snapshot(
            snapshot_raw,
            expected_sha256=snapshot_sha,
        ),
    )
    object.__setattr__(batch, "review_registry", verified_registry)
    _replace_run(batch, approved_by=f"review-snapshot:{snapshot_sha}")
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    _, chunk_pins = _pins(batch)
    with pytest.raises(StorageError, match="provenance"):
        _write(
            tmp_path / "canonical.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=snapshot_sha,
            chunk_set_sha256s=chunk_pins,
        )


def test_storage_rejects_plain_relation_and_rolls_back_whole_database(
    tmp_path: Path,
) -> None:
    """Catches forgeable approved relations or partial DBs surviving a failed build."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    case = _case()
    plain = CaseRelation(
        relation_id="relation-" + "0" * 32,
        source_case_id=case.case_id,
        target_case_id=_case("2").case_id,
        relation_type="supersedes",
        confidence=0.9,
        review_status="approved",
    )
    batch = _batch(case)
    review_pin, chunk_pins = _pins(batch)
    object.__setattr__(batch, "relations", (plain,))
    database = tmp_path / "canonical.sqlite3"

    with pytest.raises(StorageError, match="verified relation"):
        _write(
            database,
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )

    assert not database.exists()


def test_storage_rejects_unverified_review_and_plain_chunk_rows(
    tmp_path: Path,
) -> None:
    """Catches arbitrary digests or caller-built chunks crossing the final boundary."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    batch = _batch()
    review_pin, chunk_pins = _pins(batch)
    object.__setattr__(batch, "review_decision_snapshot", "f" * 64)

    with pytest.raises(StorageError, match="review decision snapshot"):
        _write(
            tmp_path / "unreviewed.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )


@pytest.mark.parametrize(
    "mutation",
    ("missing-run", "missing-document", "bad-page-total", "bad-source", "bad-delta"),
)
def test_ingestion_authority_requires_exact_nonquarantined_coverage(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches omission-based quarantine and release-delta fail-open paths."""
    batch = _batch()
    if mutation == "missing-run":
        object.__setattr__(batch, "ingestion_runs", ())
    elif mutation == "missing-document":
        _replace_run(batch, document_page_counts={})
    elif mutation == "bad-page-total":
        _replace_run(
            batch,
            document_page_counts={
                _document().doc_id: DocumentPageCounts(
                    succeeded=_document().pdf_page_count - 1,
                    quarantined=0,
                    failed=0,
                )
            },
        )
    elif mutation == "bad-source":
        _replace_run(batch, source_sha256s=("b" * 64,))
    else:
        _replace_run(batch, created_case_ids=())

    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    with pytest.raises(StorageError, match="ingestion run"):
        _write(
            tmp_path / "canonical.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )


def test_source_span_must_fit_owning_document_page_range(tmp_path: Path) -> None:
    """Catches a source citation that points beyond its owning PDF."""
    case = _case()
    invalid_span = case.source_spans[0].model_copy(update={"pdf_page_index": 101})
    invalid_case = case.model_copy(update={"source_spans": (invalid_span,)})
    batch = _batch(invalid_case)
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)

    with pytest.raises(StorageError, match="source span page"):
        _write(
            tmp_path / "canonical.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )


def test_rejected_restricted_case_is_retained_without_chunks(tmp_path: Path) -> None:
    """Catches policy-excluded records being lost or accidentally chunked."""
    batch = _batch(_restricted_case())
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    _write(
        tmp_path / "canonical.sqlite3",
        batch,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=review_pin,
        chunk_set_sha256s=chunk_pins,
    )

    with connect_canonical_storage(tmp_path / "canonical.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_review_snapshot_pin_rejects_mutation_and_incomplete_event_chain() -> None:
    """Catches self-pinned review state or a missing approval event."""
    case = _case()
    envelope = _promotion_envelope(case, role_authority_sha256="1" * 64)
    snapshot, fingerprint, _ = _review_snapshot((case,), (envelope,))
    payload = json.loads(snapshot.canonical_bytes)
    payload["cases"][0]["events"].pop()
    mutated = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()

    with pytest.raises(StorageError, match="review decision snapshot"):
        load_review_decision_snapshot(mutated, expected_sha256=fingerprint)
    with pytest.raises(StorageError, match="review decision snapshot"):
        load_review_decision_snapshot(
            mutated,
            expected_sha256=hashlib.sha256(mutated).hexdigest(),
        )


def test_hand_authored_correction_is_rejected_without_separate_authority(
    tmp_path: Path,
) -> None:
    """Catches a caller smuggling a self-asserted correction through review bytes."""
    batch = _batch()
    snapshot_payload = json.loads(batch.review_decision_snapshot.canonical_bytes)
    case = batch.cases[0]
    correction = {
        "after_sha256": hashlib.sha256((case.answer or "").encode()).hexdigest(),
        "before_sha256": hashlib.sha256("초안 답변".encode()).hexdigest(),
        "case_id": case.case_id,
        "corrected_at": "2025-08-08T00:00:30.000000Z",
        "reason_code": "answer_checked",
        "reviewer_id": "reviewer-answer",
        "sequence": 1,
        "target_field": "answer",
    }
    correction["correction_id"] = (
        "correction-"
        + hashlib.sha256(
            b"sen-qa-correction-envelope-v1\0"
            + json.dumps(
                correction,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()[:32]
    )
    snapshot_payload["cases"][0]["corrections"] = [correction]
    raw = (
        json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    snapshot_sha = hashlib.sha256(raw).hexdigest()
    object.__setattr__(
        batch,
        "review_decision_snapshot",
        load_review_decision_snapshot(raw, expected_sha256=snapshot_sha),
    )
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    _, chunk_pins = _pins(batch)
    with pytest.raises(StorageError, match="review decision"):
        _write(
            tmp_path / "canonical.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=snapshot_sha,
            chunk_set_sha256s=chunk_pins,
        )
    assert not (tmp_path / "canonical.sqlite3").exists()


def test_verified_promotion_correction_is_persisted_with_envelope_binding(
    tmp_path: Path,
) -> None:
    """Catches dropping a separately pinned pre-review correction at storage."""
    case = _case()
    correction: dict[str, object] = {
        "after_sha256": hashlib.sha256((case.answer or "").encode()).hexdigest(),
        "before_sha256": hashlib.sha256("초안 답변".encode()).hexdigest(),
        "case_id": case.case_id,
        "corrected_at": "2025-08-08T00:00:30.000000Z",
        "reason_code": "answer_checked",
        "reviewer_id": "reviewer-answer",
        "sequence": 1,
        "target_field": "answer",
    }
    correction["correction_id"] = (
        "correction-"
        + hashlib.sha256(
            b"sen-qa-correction-envelope-v1\0"
            + json.dumps(
                correction,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()[:32]
    )
    batch = _batch(corrections=(correction,))
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    database = tmp_path / "canonical.sqlite3"

    _write(
        database,
        batch,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=review_pin,
        chunk_set_sha256s=chunk_pins,
    )

    with connect_canonical_storage(database) as connection:
        stored = connection.execute(
            "SELECT case_id,promotion_envelope_sha256,payload_json FROM corrections"
        ).fetchone()
    assert stored is not None
    assert stored[0] == case.case_id
    assert stored[1] == batch.promotion_envelopes[0].fingerprint_sha256
    payload = json.loads(stored[2])
    assert payload["correction_id"] == correction["correction_id"]
    assert "초안 답변" not in stored[2]


def test_plain_chunk_rows_cannot_replace_verified_chunk_set(tmp_path: Path) -> None:
    batch = _batch()
    review_pin, chunk_pins = _pins(batch)
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    object.__setattr__(batch, "chunk_sets", (batch.chunk_sets[0].chunks,))
    with pytest.raises(StorageError, match="verified chunk set"):
        _write(
            tmp_path / "plain-chunks.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )


def test_jsonl_export_is_sorted_deterministic_and_atomic_on_failure(
    tmp_path: Path,
) -> None:
    """Catches SQLite row order, clocks, or partial files changing canonical exports."""
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    database = tmp_path / "canonical.sqlite3"
    batch = _batch()
    review_pin, chunk_pins = _pins(batch)
    _write(
        database,
        batch,
        registry_path=registry_path,
        head=head,
        review_snapshot_sha256=review_pin,
        chunk_set_sha256s=chunk_pins,
    )

    first = tmp_path / "jsonl-a"
    second = tmp_path / "jsonl-b"
    export_canonical_jsonl(database, first)
    export_canonical_jsonl(database, second)
    first_files = {path.name: path.read_bytes() for path in first.iterdir()}
    second_files = {path.name: path.read_bytes() for path in second.iterdir()}

    assert first_files == second_files
    assert all(data.endswith(b"\n") or data == b"" for data in first_files.values())
    assert all(b"\r" not in data for data in first_files.values())
    exported_span = json.loads(first_files["source_spans.jsonl"].splitlines()[0])
    assert exported_span["case_id"] == batch.cases[0].case_id
    assert exported_span["span_index"] == 0
    assert "payload" in exported_span

    failed = tmp_path / "jsonl-failed"
    with pytest.raises(StorageError, match="export failed"):
        export_canonical_jsonl(database, failed, fault_after_records=1)
    assert not failed.exists()
    assert {path.name: path.read_bytes() for path in first.iterdir()} == first_files


def test_read_boundary_does_not_create_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(StorageError), connect_canonical_storage(missing):
        pass
    assert not missing.exists()


def test_registry_and_export_reject_symlink_paths(tmp_path: Path) -> None:
    registry_link = tmp_path / "registry-link.sqlite3"
    registry_target = tmp_path / "registry-target.sqlite3"
    registry_link.symlink_to(registry_target)
    with pytest.raises(StorageError):
        initialize_issuance_registry(
            registry_link,
            expected_genesis_sha256=GENESIS_ISSUANCE_AUTHORITY_SHA256,
        )
    assert not registry_target.exists()

    registry = tmp_path / "issuance.sqlite3"
    head = _registry(registry)
    database = tmp_path / "canonical.sqlite3"
    batch = _batch()
    review_pin, chunk_pins = _pins(batch)
    _write(
        database,
        batch,
        registry_path=registry,
        head=head,
        review_snapshot_sha256=review_pin,
        chunk_set_sha256s=chunk_pins,
    )
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(StorageError):
        export_canonical_jsonl(database, linked_parent / "jsonl")
    assert not (real_parent / "jsonl").exists()


def test_source_filename_must_be_a_value_free_basename(tmp_path: Path) -> None:
    batch = _batch()
    sentinel = "PRIVATE-NAS-PATH-SENTINEL"
    unsafe = batch.documents[0].model_copy(
        update={"source_filename": f"/volume/private/{sentinel}.pdf"}
    )
    object.__setattr__(batch, "documents", (unsafe,))
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    with pytest.raises(StorageError) as captured:
        _write(
            tmp_path / "canonical.sqlite3",
            batch,
            registry_path=registry_path,
            head=head,
            review_snapshot_sha256=review_pin,
            chunk_set_sha256s=chunk_pins,
        )
    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_build_rejects_unverified_tokenizer_cache_before_publication(
    tmp_path: Path,
) -> None:
    """Catches a corpus build trusting tokenizer identity strings without files."""
    batch = _batch(release_id="corpus-20250812000000-65656565")
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    empty_model_root = tmp_path / "empty-tokenizer-cache"
    empty_model_root.mkdir()
    output_root = tmp_path / "build"

    with pytest.raises(BuildError, match="tokenizer cache") as captured:
        build_canonical_bundle(
            output_root,
            tmp_path / "diagnostics",
            registry_path,
            batch,
            expected_generation=head.generation,  # type: ignore[attr-defined]
            expected_issuance_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
            expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
            embedding_model_lock=_EMBEDDING_LOCK,
            embedding_model_root=empty_model_root,
        )

    assert not output_root.exists()
    assert read_issuance_head(registry_path).generation == 0
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_same_inputs_produce_same_canonical_content_hash(tmp_path: Path) -> None:
    """Catches paths, SQLite layout, or issuance state entering semantic identity."""
    batch = _batch(release_id="corpus-20250812000000-66666666")
    first = _build(
        tmp_path,
        batch,
        registry_name="issuance-a.sqlite3",
        output_name="build-a",
    )
    second = _build(
        tmp_path,
        batch,
        registry_name="issuance-b.sqlite3",
        output_name="build-b",
    )

    assert first.canonical_content_sha256 == second.canonical_content_sha256  # type: ignore[attr-defined]
    assert first.export_sha256s == second.export_sha256s  # type: ignore[attr-defined]
    assert (first.bundle_path / "manifest.json").read_bytes() == (  # type: ignore[attr-defined]
        second.bundle_path / "manifest.json"  # type: ignore[attr-defined]
    ).read_bytes()


def test_quarantine_blocks_bundle_and_preserves_value_free_diagnostic(
    tmp_path: Path,
) -> None:
    """Catches a partial document being reported as a successful release."""
    batch = _batch(release_id="corpus-20250812000000-77777777")
    _replace_run(
        batch,
        document_page_counts={
            _document().doc_id: DocumentPageCounts(
                succeeded=99,
                quarantined=1,
                failed=0,
            )
        },
    )
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    output_root = tmp_path / "build"
    diagnostics_root = tmp_path / "diagnostics"
    model_root = tmp_path / "tokenizer-cache"
    _write_tokenizer_cache(model_root)

    with pytest.raises(BuildError, match="nonquarantined"):
        build_canonical_bundle(
            output_root,
            diagnostics_root,
            registry_path,
            batch,
            expected_generation=head.generation,  # type: ignore[attr-defined]
            expected_issuance_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
            expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
            embedding_model_lock=_EMBEDDING_LOCK,
            embedding_model_root=model_root,
        )

    assert not (output_root / batch.release_id).exists()
    assert read_issuance_head(registry_path).generation == 0
    diagnostic = json.loads(
        (diagnostics_root / f"{batch.release_id}.json").read_bytes()
    )
    assert diagnostic == {
        "failed_pages": 0,
        "quarantined_pages": 1,
        "release_id": batch.release_id,
        "schema_version": "sen-qa-build-diagnostic/v1",
        "status": "review_required",
    }


def test_build_rejects_symlink_output_root_before_writing(tmp_path: Path) -> None:
    """Catches bundle publication escaping through a redirected output parent."""
    batch = _batch(release_id="corpus-20250812000000-88888888")
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)
    model_root = tmp_path / "tokenizer-cache"
    _write_tokenizer_cache(model_root)

    with pytest.raises(BuildError, match="output root"):
        build_canonical_bundle(
            linked_output,
            tmp_path / "diagnostics",
            registry_path,
            batch,
            expected_generation=head.generation,  # type: ignore[attr-defined]
            expected_issuance_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
            expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s={},
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
            embedding_model_lock=_EMBEDDING_LOCK,
            embedding_model_root=model_root,
        )

    assert list(real_output.iterdir()) == []
    assert read_issuance_head(registry_path).generation == 0


def test_build_failure_does_not_retain_private_values_or_exception_chain(
    tmp_path: Path,
) -> None:
    """Catches nested storage diagnostics leaking source-adjacent build inputs."""
    batch = _batch(release_id="corpus-20250812000000-99999999")
    registry_path = tmp_path / "issuance.sqlite3"
    head = _registry(registry_path)
    review_pin, chunk_pins = _pins(batch)
    sentinel = "PRIVATE_RUNTIME_SENTINEL"
    model_root = tmp_path / "tokenizer-cache"
    _write_tokenizer_cache(model_root)

    with pytest.raises(BuildError, match="build failed") as captured:
        build_canonical_bundle(
            tmp_path / "build",
            tmp_path / "diagnostics",
            registry_path,
            batch,
            expected_generation=head.generation,  # type: ignore[attr-defined]
            expected_issuance_authority_sha256=head.authority_sha256,  # type: ignore[attr-defined]
            expected_predecessor_bundle_sha256=head.bundle_sha256,  # type: ignore[attr-defined]
            expected_review_decision_snapshot_sha256=review_pin,
            expected_registry_sha256=batch.review_registry.fingerprint_sha256,
            expected_chunk_set_sha256s=chunk_pins,
            expected_relation_approval_sha256s=sentinel,  # type: ignore[arg-type]
            expected_model_lock_sha256=_EMBEDDING_LOCK.fingerprint_sha256,
            expected_runtime_fingerprint_sha256="d" * 64,
            embedding_model_lock=_EMBEDDING_LOCK,
            embedding_model_root=model_root,
        )

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert read_issuance_head(registry_path).generation == 0
