"""Assemble one reviewed package into an atomic canonical corpus bundle."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from src.corpus.build import CanonicalBuildResult, build_canonical_bundle
from src.corpus.chunking import (
    ChunkingError,
    build_chunks,
    load_locked_tokenizer,
    role_source_manifest_bytes,
    tokenizer_contract,
    tokenizer_runtime_fingerprint_sha256,
    verify_role_sources,
)
from src.corpus.models import Case, Document, DocumentPageCounts, IngestionRun
from src.corpus.relations import canonical_case_sha256
from src.corpus.storage import (
    CanonicalStorageBatch,
    IssuedCaseRecord,
    StorageError,
    VerifiedPromotionEnvelope,
    VerifiedReviewDecisionSnapshot,
    load_promotion_envelope,
    load_review_decision_snapshot,
    read_issuance_snapshot,
)
from src.ingestion.review import (
    CanonicalReviewRegistry,
    VerifiedCanonicalReviewRegistry,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_CASES = 100_000


class FinalizationError(ValueError):
    """A value-free review-package finalization failure."""


class _DuplicateKey(ValueError):
    pass


def _raise(code: str) -> NoReturn:
    raise FinalizationError(code) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _read_regular(path: Path, *, max_bytes: int = _MAX_METADATA_BYTES) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(raw) > max_bytes or before_identity != after_identity:
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _json_object(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateKey):
        return None
    return cast(dict[str, object], value) if type(value) is dict else None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _load_attestation(
    package: Path,
    *,
    release_id: str,
    expected_sha256: str,
    expected_registry_sha256: str,
) -> dict[str, object]:
    raw = _read_regular(package / "review-ready.attestation.json")
    if (
        raw is None
        or not _SHA256_RE.fullmatch(expected_sha256)
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)
    ):
        _raise("review_ready_attestation_invalid")
    payload = _json_object(raw)
    expected_fields = {
        "approved_count",
        "candidate_binding_sha256",
        "case_count",
        "documents_sha256",
        "ingestion_evidence_sha256",
        "registry_sha256",
        "rejected_count",
        "release_id",
        "schema_version",
        "snapshot_sha256",
    }
    if (
        payload is None
        or set(payload) != expected_fields
        or payload["schema_version"] != "sen-qa-review-ready-attestation/v1"
        or payload["release_id"] != release_id
        or payload["registry_sha256"] != expected_registry_sha256
        or any(
            not isinstance(payload[key], str)
            or _SHA256_RE.fullmatch(cast(str, payload[key])) is None
            for key in (
                "candidate_binding_sha256",
                "documents_sha256",
                "ingestion_evidence_sha256",
                "snapshot_sha256",
            )
        )
        or any(
            isinstance(payload[key], bool)
            or not isinstance(payload[key], int)
            or cast(int, payload[key]) < 0
            for key in ("approved_count", "case_count", "rejected_count")
        )
        or cast(int, payload["case_count"]) < 1
        or cast(int, payload["case_count"]) > _MAX_CASES
        or cast(int, payload["approved_count"]) + cast(int, payload["rejected_count"])
        != cast(int, payload["case_count"])
        or _canonical_json(payload) + b"\n" != raw
    ):
        _raise("review_ready_attestation_invalid")
    return payload


def _load_documents(package: Path, *, expected_sha256: str) -> tuple[Document, ...]:
    raw = _read_regular(package / "documents.json")
    if raw is None or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_sha256
    ):
        _raise("review_documents_invalid")
    payload = _json_object(raw)
    if (
        payload is None
        or set(payload) != {"documents", "schema_version"}
        or payload["schema_version"] != "sen-qa-review-documents/v1"
        or type(payload["documents"]) is not list
        or not 1 <= len(cast(list[object], payload["documents"])) <= 64
        or _canonical_json(payload) + b"\n" != raw
    ):
        _raise("review_documents_invalid")
    try:
        documents = tuple(
            Document.model_validate(item)
            for item in cast(list[object], payload["documents"])
        )
    except (TypeError, ValueError):
        _raise("review_documents_invalid")
    if tuple(item.doc_id for item in documents) != tuple(
        sorted({item.doc_id for item in documents})
    ):
        _raise("review_documents_invalid")
    return documents


def _load_evidence(
    package: Path,
    *,
    expected_sha256: str,
    documents: tuple[Document, ...],
) -> tuple[dict[str, DocumentPageCounts], str, str, str]:
    raw = _read_regular(package / "ingestion-evidence.json")
    if raw is None or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_sha256
    ):
        _raise("ingestion_evidence_invalid")
    payload = _json_object(raw)
    if (
        payload is None
        or set(payload)
        != {
            "document_page_counts",
            "manifest_sha256",
            "parser_quarantine_count",
            "parser_authority_sha256",
            "raw_authority_sha256",
            "schema_version",
        }
        or payload["schema_version"] != "sen-qa-ingestion-evidence/v1"
        or type(payload["document_page_counts"]) is not dict
        or type(payload["parser_quarantine_count"]) is not int
        or payload["parser_quarantine_count"] != 0
        or any(
            not isinstance(payload[key], str)
            or _SHA256_RE.fullmatch(cast(str, payload[key])) is None
            for key in (
                "manifest_sha256",
                "parser_authority_sha256",
                "raw_authority_sha256",
            )
        )
        or _canonical_json(payload) + b"\n" != raw
    ):
        _raise("ingestion_evidence_invalid")
    try:
        counts = {
            doc_id: DocumentPageCounts.model_validate(item)
            for doc_id, item in cast(
                dict[object, object], payload["document_page_counts"]
            ).items()
            if isinstance(doc_id, str)
        }
    except (TypeError, ValueError):
        _raise("ingestion_evidence_invalid")
    document_by_id = {item.doc_id: item for item in documents}
    if (
        set(counts) != set(document_by_id)
        or len(counts)
        != len(cast(dict[object, object], payload["document_page_counts"]))
        or any(
            count.succeeded + count.quarantined + count.failed
            != document_by_id[doc_id].pdf_page_count
            or count.quarantined != 0
            or count.failed != 0
            for doc_id, count in counts.items()
        )
    ):
        _raise("ingestion_evidence_incomplete")
    return (
        counts,
        cast(str, payload["manifest_sha256"]),
        cast(str, payload["parser_authority_sha256"]),
        cast(str, payload["raw_authority_sha256"]),
    )


def _terminal_cases(
    package: Path,
    *,
    attestation: dict[str, object],
    expected_registry_sha256: str,
    parser_authority_sha256: str,
    raw_authority_sha256: str,
) -> tuple[
    tuple[Case, ...],
    tuple[VerifiedPromotionEnvelope, ...],
    VerifiedCanonicalReviewRegistry,
    VerifiedReviewDecisionSnapshot,
]:
    registry_raw = _read_regular(package / "registry.json")
    snapshot_raw = _read_regular(package / "review-decision-snapshot.json")
    if registry_raw is None or snapshot_raw is None:
        _raise("review_authority_invalid")
    try:
        registry = CanonicalReviewRegistry.from_bytes(
            registry_raw,
            expected_sha256=expected_registry_sha256,
        )
        snapshot = load_review_decision_snapshot(
            snapshot_raw,
            expected_sha256=cast(str, attestation["snapshot_sha256"]),
        )
    except (TypeError, ValueError):
        _raise("review_authority_invalid")
    if snapshot.registry_fingerprint_sha256 != registry.fingerprint_sha256:
        _raise("review_authority_invalid")
    candidate_dir = package / "candidates"
    if not candidate_dir.is_dir() or candidate_dir.is_symlink():
        _raise("review_authority_invalid")
    expected_names = {f"{item.case_id}.json" for item in registry.cases}
    try:
        if {item.name for item in candidate_dir.iterdir()} != expected_names:
            _raise("review_authority_invalid")
    except OSError:
        _raise("review_authority_invalid")
    decisions = {item.case_id: item for item in snapshot.cases}
    references = {item.case_id: item for item in registry.cases}
    if set(decisions) != set(references):
        _raise("review_authority_invalid")
    envelopes: list[VerifiedPromotionEnvelope] = []
    cases: list[Case] = []
    binding: dict[str, str] = {}
    statuses: list[str] = []
    for case_id in sorted(references):
        reference = references[case_id]
        raw = _read_regular(candidate_dir / f"{case_id}.json")
        if raw is None:
            _raise("review_authority_invalid")
        try:
            envelope = load_promotion_envelope(
                raw,
                expected_sha256=reference.content_sha256,
            )
        except (TypeError, ValueError):
            _raise("review_authority_invalid")
        decision = decisions[case_id]
        if (
            envelope.candidate_case.case_id != case_id
            or envelope.fingerprint_sha256 != decision.promotion_envelope_sha256
            or envelope.parser_authority_sha256 != parser_authority_sha256
            or envelope.raw_authority_sha256 != raw_authority_sha256
            or decision.corrections
        ):
            _raise("review_authority_invalid")
        payload = envelope.candidate_case.model_dump(mode="json")
        record = decision.review_record
        payload.update(
            {
                "answer_eligible": record["answer_eligible"],
                "critical_field_review": record["critical_field_review"],
                "review_status": record["review_status"],
                "search_eligible": record["search_eligible"],
            }
        )
        try:
            final_case = Case.model_validate_json(_canonical_json(payload))
        except (TypeError, ValueError):
            _raise("review_authority_invalid")
        cases.append(final_case)
        envelopes.append(envelope)
        binding[case_id] = envelope.fingerprint_sha256
        statuses.append(final_case.review_status)
    if (
        hashlib.sha256(_canonical_json(binding)).hexdigest()
        != attestation["candidate_binding_sha256"]
        or len(cases) != attestation["case_count"]
        or statuses.count("approved") != attestation["approved_count"]
        or statuses.count("rejected") != attestation["rejected_count"]
    ):
        _raise("review_authority_invalid")
    return tuple(cases), tuple(envelopes), registry, snapshot


def _event_times(snapshot: VerifiedReviewDecisionSnapshot) -> tuple[datetime, datetime]:
    try:
        values = tuple(
            datetime.fromisoformat(cast(str, event["occurred_at"]))
            for decision in snapshot.cases
            for event in decision.events
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        _raise("review_authority_invalid")
    if not values or any(
        value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        for value in values
    ):
        _raise("review_authority_invalid")
    return min(values), max(values)


def _run_deltas(
    cases: tuple[Case, ...], records: tuple[IssuedCaseRecord, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    try:
        prior = {item.case_id: item for item in records}
        current = {item.case_id: item for item in cases}
        active = {case_id for case_id, item in prior.items() if item.state == "active"}
        created = tuple(sorted(set(current) - set(prior)))
        deleted = tuple(sorted(active - set(current)))
        changed = tuple(
            sorted(
                case_id
                for case_id in set(current) & active
                if canonical_case_sha256(current[case_id])
                != prior[case_id].current_content_sha256
            )
        )
    except (AttributeError, TypeError, ValueError):
        _raise("issuance_authority_invalid")
    return created, changed, deleted


def _finalize_review_ready_bundle(
    package: Path,
    release_root: Path,
    diagnostics_root: Path,
    issuance_registry_path: Path,
    *,
    release_id: str,
    expected_ready_attestation_sha256: str,
    expected_registry_sha256: str,
    expected_model_lock_sha256: str,
    expected_runtime_fingerprint_sha256: str,
    container_image: str,
    runtime_lock_path: Path,
    indexer_image_digest: str,
    embedding_model_lock: object,
    embedding_model_root: Path,
) -> CanonicalBuildResult:
    if (
        not isinstance(package, Path)
        or not package.is_dir()
        or package.is_symlink()
        or not _RELEASE_RE.fullmatch(release_id)
        or not _SHA256_RE.fullmatch(expected_registry_sha256)
        or not _SHA256_RE.fullmatch(expected_model_lock_sha256)
        or not _SHA256_RE.fullmatch(expected_runtime_fingerprint_sha256)
        or not _IMAGE_RE.fullmatch(container_image)
        or not isinstance(runtime_lock_path, Path)
        or not _IMAGE_RE.fullmatch(indexer_image_digest)
    ):
        _raise("canonical_finalization_invalid")
    runtime_lock_bytes = _read_regular(runtime_lock_path)
    if runtime_lock_bytes is None or not hmac.compare_digest(
        tokenizer_runtime_fingerprint_sha256(
            runtime_lock_bytes,
            indexer_image_digest=indexer_image_digest,
        ),
        expected_runtime_fingerprint_sha256,
    ):
        _raise("tokenizer_runtime_authority_invalid")
    attestation = _load_attestation(
        package,
        release_id=release_id,
        expected_sha256=expected_ready_attestation_sha256,
        expected_registry_sha256=expected_registry_sha256,
    )
    documents = _load_documents(
        package,
        expected_sha256=cast(str, attestation["documents_sha256"]),
    )
    counts, manifest_sha, parser_sha, raw_sha = _load_evidence(
        package,
        expected_sha256=cast(str, attestation["ingestion_evidence_sha256"]),
        documents=documents,
    )
    cases, envelopes, registry, snapshot = _terminal_cases(
        package,
        attestation=attestation,
        expected_registry_sha256=expected_registry_sha256,
        parser_authority_sha256=parser_sha,
        raw_authority_sha256=raw_sha,
    )
    tokenizer = load_locked_tokenizer(
        embedding_model_lock,
        embedding_model_root,
        expected_lock_sha256=expected_model_lock_sha256,
        runtime_fingerprint_sha256=expected_runtime_fingerprint_sha256,
    )
    contract = tokenizer_contract(
        embedding_model_lock,
        expected_lock_sha256=expected_model_lock_sha256,
        runtime_fingerprint_sha256=expected_runtime_fingerprint_sha256,
    )
    envelope_by_id = {item.candidate_case.case_id: item for item in envelopes}
    chunk_sets = []
    chunk_pins: dict[str, str] = {}
    for case in cases:
        if not case.search_eligible:
            continue
        envelope = envelope_by_id[case.case_id]
        role_sha = hashlib.sha256(
            role_source_manifest_bytes(case, envelope.role_sources)
        ).hexdigest()
        role_sources = verify_role_sources(
            case,
            envelope.role_sources,
            expected_authority_sha256=role_sha,
        )
        table_pins = {
            source.source_span_index: source.table_evidence_sha256
            for source in envelope.role_sources
            if source.table_evidence_sha256 is not None
        }
        chunk_set = build_chunks(
            case,
            role_sources,
            tokenizer=tokenizer,
            contract=contract,
            expected_role_authority_sha256=role_sha,
            expected_table_evidence_sha256s=table_pins,
        )
        chunk_sets.append(chunk_set)
        chunk_pins[case.case_id] = chunk_set.binding_sha256
    issuance = read_issuance_snapshot(issuance_registry_path)
    created, changed, deleted = _run_deltas(cases, issuance.records)
    started_at, ended_at = _event_times(snapshot)
    versions = tuple(sorted({item.ingestion_version for item in documents}))
    version_sha = hashlib.sha256(_canonical_json(versions)).hexdigest()
    run = IngestionRun(
        run_id=f"run-{release_id}",
        release_id=release_id,
        started_at=started_at,
        ended_at=ended_at,
        manifest_version=f"sha256:{manifest_sha}",
        source_sha256s=tuple(sorted(item.sha256 for item in documents)),
        extractor_version=f"ingestion-set:{version_sha}",
        ocr_engine_version=(
            f"container:{container_image}"
            if any(item.extraction_method == "ocr" for item in documents)
            else None
        ),
        ocr_model_version=(
            f"container:{container_image}"
            if any(item.extraction_method == "ocr" for item in documents)
            else None
        ),
        container_image=container_image,
        normalizer_version=f"raw-authority:{raw_sha}",
        parser_version=f"parser-authority:{parser_sha}",
        schema_version="sen-qa-canonical-storage/v1",
        document_page_counts=counts,
        created_case_ids=created,
        changed_case_ids=changed,
        deleted_case_ids=deleted,
        approved_by=f"review-snapshot:{snapshot.fingerprint_sha256}",
    )
    batch = CanonicalStorageBatch(
        release_id=release_id,
        documents=documents,
        cases=cases,
        chunk_sets=tuple(chunk_sets),
        law_refs=(),
        relations=(),
        relation_approval_sha256s={},
        ingestion_runs=(run,),
        tokenizer_contract=contract,
        promotion_envelopes=envelopes,
        review_registry=registry,
        review_decision_snapshot=snapshot,
    )
    return build_canonical_bundle(
        release_root,
        diagnostics_root,
        issuance_registry_path,
        batch,
        expected_generation=issuance.head.generation,
        expected_issuance_authority_sha256=issuance.head.authority_sha256,
        expected_predecessor_bundle_sha256=issuance.head.bundle_sha256,
        expected_review_decision_snapshot_sha256=snapshot.fingerprint_sha256,
        expected_registry_sha256=expected_registry_sha256,
        expected_chunk_set_sha256s=chunk_pins,
        expected_relation_approval_sha256s={},
        expected_model_lock_sha256=expected_model_lock_sha256,
        expected_runtime_fingerprint_sha256=expected_runtime_fingerprint_sha256,
        embedding_model_lock=embedding_model_lock,
        embedding_model_root=embedding_model_root,
        bundle_directory_name="canonical",
    )


def finalize_review_ready_bundle(
    package: Path,
    release_root: Path,
    diagnostics_root: Path,
    issuance_registry_path: Path,
    *,
    release_id: str,
    expected_ready_attestation_sha256: str,
    expected_registry_sha256: str,
    expected_model_lock_sha256: str,
    expected_runtime_fingerprint_sha256: str,
    container_image: str,
    runtime_lock_path: Path,
    indexer_image_digest: str,
    embedding_model_lock: object,
    embedding_model_root: Path,
) -> CanonicalBuildResult:
    """Build reviewed canonical storage through one value-free public boundary."""
    code = "canonical_finalization_failed"
    try:
        return _finalize_review_ready_bundle(
            package,
            release_root,
            diagnostics_root,
            issuance_registry_path,
            release_id=release_id,
            expected_ready_attestation_sha256=expected_ready_attestation_sha256,
            expected_registry_sha256=expected_registry_sha256,
            expected_model_lock_sha256=expected_model_lock_sha256,
            expected_runtime_fingerprint_sha256=expected_runtime_fingerprint_sha256,
            container_image=container_image,
            runtime_lock_path=runtime_lock_path,
            indexer_image_digest=indexer_image_digest,
            embedding_model_lock=embedding_model_lock,
            embedding_model_root=embedding_model_root,
        )
    except FinalizationError as error:
        code = str(error)
    except (ChunkingError, OSError, StorageError, TypeError, ValueError):
        pass
    _raise(code)
