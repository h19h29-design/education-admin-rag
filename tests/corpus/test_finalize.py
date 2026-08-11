"""Finalization boundary tests for mixed OCR authority evidence."""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest

from src.corpus.chunking import tokenizer_runtime_fingerprint_sha256
from src.corpus.finalize import (
    FinalizationError,
    _build_ingestion_run,
    _load_attestation,
    _load_documents,
    _load_evidence,
    finalize_review_ready_bundle,
)
from src.corpus.models import Document
from src.ingestion.ocr_authority import (
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
)
from src.ingestion.quarantine_review import (
    append_resolution_event,
    create_resolution_draft,
    load_resolution_authority,
)

_RELEASE_ID = "corpus-20250808123456-deadbeef"
_REGISTRY_SHA256 = "7" * 64
_SOURCE_BINDINGS = {
    2023: (
        "sen-qa-2023",
        "9a6a5b3745eb4200c70f9d33395c8b25b5a55fa171036127f2be5791224455bc",
    ),
    2024: (
        "sen-qa-2024",
        "fc1494eff8ee3fe9b53606dd5f55468d8ec254b9d2d661fba6c5e4b46daa99ed",
    ),
    2025: (
        "sen-qa-2025",
        "9a1a7b0ebf1346b540c97d9990dd3b43c647ce397322ff0fabe6d2de84c0ce03",
    ),
}


class _Package(NamedTuple):
    root: Path
    documents: tuple[Document, ...]
    documents_sha256: str
    evidence: dict[str, Any]
    evidence_sha256: str
    attestation: dict[str, Any]
    attestation_sha256: str
    lock_sha256: str | None
    lock_self_sha256: str | None


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _write_json(path: Path, payload: object) -> str:
    raw = _canonical_json(payload)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _document(year: int, *, extraction_method: str = "ocr") -> Document:
    if extraction_method == "ocr":
        doc_id, source_sha256 = _SOURCE_BINDINGS[year]
        source_dpi: int | None = {2023: 96, 2024: 150, 2025: 300}[year]
    else:
        doc_id = f"sen-qa-{year}"
        source_sha256 = "a" * 64
        source_dpi = None
    return Document.model_validate(
        {
            "doc_id": doc_id,
            "edition_year": year,
            "title": f"Synthetic {year}",
            "publisher": "Synthetic publisher",
            "registration_no": None,
            "source_period_start": None,
            "source_period_end": None,
            "source_filename": f"{year}.pdf",
            "sha256": source_sha256,
            "pdf_page_count": 1,
            "extraction_method": extraction_method,
            "source_dpi": source_dpi,
            "public_url": None,
            "redistribution_status": "unverified",
            "access_level": "staff",
            "page_numbering_rule": "offset:0",
            "ingestion_version": "ingestion-v2",
        }
    )


def _write_package(tmp_path: Path, *, native_only: bool = False) -> _Package:
    root = tmp_path / "review"
    root.mkdir()
    documents = (
        (_document(2022, extraction_method="native"),)
        if native_only
        else tuple(_document(year) for year in (2023, 2024, 2025))
    )
    documents_payload = {
        "documents": [item.model_dump(mode="json") for item in documents],
        "schema_version": "sen-qa-review-documents/v1",
    }
    documents_sha256 = _write_json(root / "documents.json", documents_payload)
    lock_sha256: str | None = None
    lock_self_sha256: str | None = None
    evidence: dict[str, Any] = {
        "document_page_counts": {
            item.doc_id: {"failed": 0, "quarantined": 0, "succeeded": 1}
            for item in documents
        },
        "manifest_sha256": "1" * 64,
        "parser_quarantine_count": 0,
        "parser_authority_sha256": "2" * 64,
        "raw_authority_sha256": "3" * 64,
        "schema_version": "sen-qa-ingestion-evidence/v1",
    }
    if not native_only:
        lock = build_ocr_authority_lock(
            vision_2024_runtime_fingerprint="sha256:" + "4" * 64,
            vision_2025_runtime_fingerprint="sha256:" + "5" * 64,
        )
        lock_raw = canonical_ocr_authority_bytes(lock)
        (root / "ocr-authority-lock.json").write_bytes(lock_raw)
        lock_sha256 = hashlib.sha256(lock_raw).hexdigest()
        lock_self_sha256 = lock.self_sha256
        evidence.update(
            {
                "ocr_authority_lock_sha256": lock_sha256,
                "ocr_authority_self_sha256": lock_self_sha256,
                "schema_version": "sen-qa-ingestion-evidence/v2",
            }
        )
    evidence_sha256 = _write_json(root / "ingestion-evidence.json", evidence)
    attestation: dict[str, Any] = {
        "approved_count": 1,
        "candidate_binding_sha256": "6" * 64,
        "case_count": 1,
        "documents_sha256": documents_sha256,
        "ingestion_evidence_sha256": evidence_sha256,
        "registry_sha256": _REGISTRY_SHA256,
        "rejected_count": 0,
        "release_id": _RELEASE_ID,
        "schema_version": "sen-qa-review-ready-attestation/v1",
        "snapshot_sha256": "8" * 64,
    }
    if lock_sha256 is not None:
        attestation.update(
            {
                "ocr_authority_lock_sha256": lock_sha256,
                "schema_version": "sen-qa-review-ready-attestation/v2",
            }
        )
    attestation_sha256 = _write_json(
        root / "review-ready.attestation.json", attestation
    )
    return _Package(
        root=root,
        documents=documents,
        documents_sha256=documents_sha256,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
        attestation=attestation,
        attestation_sha256=attestation_sha256,
        lock_sha256=lock_sha256,
        lock_self_sha256=lock_self_sha256,
    )


def _reseal_evidence(
    package: _Package, evidence: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    evidence_sha256 = _write_json(package.root / "ingestion-evidence.json", evidence)
    attestation = dict(package.attestation)
    attestation["ingestion_evidence_sha256"] = evidence_sha256
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )
    return attestation, attestation_sha256


def _reseal_document_graph(
    package: _Package,
    document_payload: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    documents_sha256 = _write_json(package.root / "documents.json", document_payload)
    evidence_sha256 = _write_json(package.root / "ingestion-evidence.json", evidence)
    attestation = dict(package.attestation)
    attestation["documents_sha256"] = documents_sha256
    attestation["ingestion_evidence_sha256"] = evidence_sha256
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )
    return attestation, attestation_sha256, documents_sha256, evidence_sha256


def _loaded(package: _Package) -> tuple[dict[str, object], tuple[Document, ...]]:
    attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=package.attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(
        package.root,
        expected_sha256=package.documents_sha256,
    )
    return attestation, documents


def test_v2_evidence_loads_exact_three_year_authority_and_builds_bound_run(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    attestation, documents = _loaded(package)

    evidence = _load_evidence(
        package.root,
        expected_sha256=package.evidence_sha256,
        documents=documents,
        attestation=attestation,
    )
    run = _build_ingestion_run(
        release_id=_RELEASE_ID,
        documents=documents,
        evidence=evidence,
        started_at=datetime(2025, 8, 8, 12, 0, tzinfo=UTC),
        ended_at=datetime(2025, 8, 8, 12, 1, tzinfo=UTC),
        created_case_ids=("case-1",),
        changed_case_ids=(),
        deleted_case_ids=(),
        approved_by="review-snapshot:" + "9" * 64,
        container_image="sha256:" + "a" * 64,
    )

    authority = evidence.ocr_authority
    assert authority is not None
    assert authority.lock_file_sha256 == package.lock_sha256
    assert authority.lock_self_sha256 == package.lock_self_sha256
    assert tuple(
        (
            entry.year,
            entry.doc_id,
            entry.source_sha256,
            entry.record_schema,
            entry.engine,
        )
        for entry in authority.lock.entries
    ) == (
        (
            2023,
            "sen-qa-2023",
            _SOURCE_BINDINGS[2023][1],
            "sen-qa-ocr-page/v2",
            "paddleocr",
        ),
        (
            2024,
            "sen-qa-2024",
            _SOURCE_BINDINGS[2024][1],
            "sen-qa-ocr-page/v3",
            "apple-vision",
        ),
        (
            2025,
            "sen-qa-2025",
            _SOURCE_BINDINGS[2025][1],
            "sen-qa-ocr-page/v3",
            "apple-vision",
        ),
    )
    assert run.ocr_authority_lock_sha256 == package.lock_sha256
    assert run.ocr_authority_self_sha256 == package.lock_self_sha256
    assert run.ocr_engine_version == f"authority-lock:{package.lock_sha256}"
    assert run.ocr_model_version == f"authority-self:{package.lock_self_sha256}"
    assert run.model_validate_json(run.model_dump_json()) == run


def test_truthful_native_v1_evidence_remains_supported(tmp_path: Path) -> None:
    package = _write_package(tmp_path, native_only=True)
    attestation, documents = _loaded(package)

    evidence = _load_evidence(
        package.root,
        expected_sha256=package.evidence_sha256,
        documents=documents,
        attestation=attestation,
    )

    assert evidence.ocr_authority is None


def test_v4_evidence_requires_externally_sealed_resolution_authority(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path, native_only=True)
    quarantine_raw = _canonical_json(
        {
            "doc_id": "sen-qa-2022",
            "edition_year": 2022,
            "location_id": "loc-" + "1" * 32,
            "page_ids": [1],
            "reason_code": "ambiguous_boundary",
            "source_spans": [
                {
                    "bbox": [1.0, 2.0, 3.0, 4.0],
                    "page_label": "1",
                    "pdf_page_index": 1,
                    "text_sha256": "a" * 64,
                }
            ],
            "span_count": 1,
        }
    )
    old_parser_sha256 = "4" * 64
    old_registry_sha256 = "5" * 64
    draft_raw = create_resolution_draft(
        release_id=_RELEASE_ID,
        registry_sha256=old_registry_sha256,
        manifest_sha256=cast(str, package.evidence["manifest_sha256"]),
        raw_authority_sha256=cast(str, package.evidence["raw_authority_sha256"]),
        parser_authority_sha256=old_parser_sha256,
        parser_quarantines_bytes=quarantine_raw,
        parser_quarantines_sha256=hashlib.sha256(quarantine_raw).hexdigest(),
    )
    draft_path = package.root / "draft.json"
    draft_path.write_bytes(draft_raw)
    os.chmod(draft_path, 0o600)
    draft = load_resolution_authority(
        draft_path, expected_sha256=hashlib.sha256(draft_raw).hexdigest()
    )
    resolution_raw = append_resolution_event(
        draft,
        occurrence_id=draft.resolutions[0].occurrence_id,
        disposition="confirmed_noncase",
        annotations=(),
        actor_id="uid:501:reviewer-a",
        event_id="event-0001",
        occurred_at="2026-08-11T01:00:00Z",
    )
    draft_path.unlink()
    resolution_path = package.root / "parser-quarantine-resolutions.json"
    resolution_path.write_bytes(resolution_raw)
    os.chmod(resolution_path, 0o600)
    resolution_sha256 = hashlib.sha256(resolution_raw).hexdigest()
    evidence = dict(package.evidence)
    evidence.update(
        {
            "resolution_authority_sha256": resolution_sha256,
            "resolved_from_parser_authority_sha256": old_parser_sha256,
            "resolved_from_parser_quarantines_sha256": hashlib.sha256(
                quarantine_raw
            ).hexdigest(),
            "resolved_from_registry_sha256": old_registry_sha256,
            "schema_version": "sen-qa-ingestion-evidence/v4",
        }
    )
    attestation, attestation_sha256 = _reseal_evidence(package, evidence)
    attestation.update(
        {
            "resolution_authority_sha256": resolution_sha256,
            "schema_version": "sen-qa-review-ready-attestation/v3",
        }
    )
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )
    loaded_attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(package.root, expected_sha256=package.documents_sha256)

    loaded = _load_evidence(
        package.root,
        expected_sha256=hashlib.sha256(
            (package.root / "ingestion-evidence.json").read_bytes()
        ).hexdigest(),
        documents=documents,
        attestation=loaded_attestation,
    )
    assert loaded.resolution_authority_sha256 == resolution_sha256

    resolution_path.unlink()
    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid") as caught:
        _load_evidence(
            package.root,
            expected_sha256=hashlib.sha256(
                (package.root / "ingestion-evidence.json").read_bytes()
            ).hexdigest(),
            documents=documents,
            attestation=loaded_attestation,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("native_only", [True, False])
def test_ready_v1_v2_evidence_rejects_unattested_parser_quarantine_artifact(
    tmp_path: Path, native_only: bool
) -> None:
    """Catches a quarantine file being appended after ready attestation sealing."""
    package = _write_package(tmp_path, native_only=native_only)
    (package.root / "parser-quarantines.jsonl").write_bytes(
        b'{"reason_code":"ambiguous_boundary"}\n'
    )
    attestation, documents = _loaded(package)

    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid") as caught:
        _load_evidence(
            package.root,
            expected_sha256=package.evidence_sha256,
            documents=documents,
            attestation=attestation,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_ocr_documents_cannot_downgrade_to_v1_evidence(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    evidence = dict(package.evidence)
    evidence.pop("ocr_authority_lock_sha256")
    evidence.pop("ocr_authority_self_sha256")
    evidence["schema_version"] = "sen-qa-ingestion-evidence/v1"
    attestation, attestation_sha256 = _reseal_evidence(package, evidence)
    attestation.pop("ocr_authority_lock_sha256")
    attestation["schema_version"] = "sen-qa-review-ready-attestation/v1"
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )
    loaded_attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(package.root, expected_sha256=package.documents_sha256)

    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid") as caught:
        _load_evidence(
            package.root,
            expected_sha256=hashlib.sha256(
                (package.root / "ingestion-evidence.json").read_bytes()
            ).hexdigest(),
            documents=documents,
            attestation=loaded_attestation,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "wrong-source", "year-replay"]
)
def test_v2_evidence_requires_the_exact_three_cross_bound_ocr_documents(
    tmp_path: Path,
    mutation: str,
) -> None:
    package = _write_package(tmp_path)
    document_payload = json.loads((package.root / "documents.json").read_bytes())
    evidence = dict(package.evidence)
    counts = dict(evidence["document_page_counts"])
    if mutation == "missing":
        document_payload["documents"].pop()
        counts.pop("sen-qa-2025")
    elif mutation == "extra":
        extra = _document(2025).model_dump(mode="json")
        extra.update(
            doc_id="sen-qa-2026",
            edition_year=2026,
            source_filename="2026.pdf",
            sha256="6" * 64,
        )
        document_payload["documents"].append(extra)
        counts["sen-qa-2026"] = {"failed": 0, "quarantined": 0, "succeeded": 1}
    elif mutation == "wrong-source":
        document_payload["documents"][2]["sha256"] = "0" * 64
    else:
        document_payload["documents"][2]["edition_year"] = 2024
    evidence["document_page_counts"] = counts
    _, attestation_sha256, documents_sha256, evidence_sha256 = _reseal_document_graph(
        package, document_payload, evidence
    )
    loaded_attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(package.root, expected_sha256=documents_sha256)

    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid"):
        _load_evidence(
            package.root,
            expected_sha256=evidence_sha256,
            documents=documents,
            attestation=loaded_attestation,
        )


@pytest.mark.parametrize("mutation", ["downgrade", "missing", "extra", "type-bomb"])
def test_ready_attestation_v2_rejects_schema_downgrade_missing_extra_and_types(
    tmp_path: Path,
    mutation: str,
) -> None:
    package = _write_package(tmp_path)
    attestation = dict(package.attestation)
    if mutation == "downgrade":
        attestation["schema_version"] = "sen-qa-review-ready-attestation/v1"
    elif mutation == "missing":
        attestation.pop("ocr_authority_lock_sha256")
    elif mutation == "extra":
        attestation["PRIVATE_ATTESTATION_SENTINEL"] = "PRIVATE_ATTESTATION_SENTINEL"
    else:
        attestation["ocr_authority_lock_sha256"] = {"PRIVATE": True}
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )

    with pytest.raises(FinalizationError) as caught:
        _load_attestation(
            package.root,
            release_id=_RELEASE_ID,
            expected_sha256=attestation_sha256,
            expected_registry_sha256=_REGISTRY_SHA256,
        )

    rendered = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "PRIVATE" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "wrong-self",
        "attestation-mismatch",
        "type-bomb",
    ],
)
def test_v2_evidence_rejects_missing_extra_forged_or_unbound_authority(
    tmp_path: Path, mutation: str
) -> None:
    package = _write_package(tmp_path)
    evidence = dict(package.evidence)
    if mutation == "missing":
        evidence.pop("ocr_authority_self_sha256")
    elif mutation == "extra":
        evidence["PRIVATE_AUTHORITY_SENTINEL"] = "PRIVATE_AUTHORITY_SENTINEL"
    elif mutation == "wrong-self":
        evidence["ocr_authority_self_sha256"] = "0" * 64
    elif mutation == "attestation-mismatch":
        evidence["ocr_authority_lock_sha256"] = "0" * 64
    else:
        evidence["ocr_authority_lock_sha256"] = {"PRIVATE": True}
    attestation, attestation_sha256 = _reseal_evidence(package, evidence)
    loaded_attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(package.root, expected_sha256=package.documents_sha256)

    with pytest.raises(FinalizationError) as caught:
        _load_evidence(
            package.root,
            expected_sha256=attestation["ingestion_evidence_sha256"],
            documents=documents,
            attestation=loaded_attestation,
        )

    rendered = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "PRIVATE" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_v2_evidence_rejects_year_replay_even_when_all_json_hashes_are_resealed(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    lock_path = package.root / "ocr-authority-lock.json"
    lock_payload = json.loads(lock_path.read_bytes())
    lock_payload["entries"][2] = dict(lock_payload["entries"][1])
    lock_payload["entries"][2]["year"] = 2025
    body = {
        "entries": lock_payload["entries"],
        "schema_version": lock_payload["schema_version"],
    }
    lock_payload["self_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    replay_raw = _canonical_json(lock_payload)
    lock_path.write_bytes(replay_raw)
    replay_sha256 = hashlib.sha256(replay_raw).hexdigest()
    evidence = dict(package.evidence)
    evidence["ocr_authority_lock_sha256"] = replay_sha256
    evidence["ocr_authority_self_sha256"] = lock_payload["self_sha256"]
    attestation, _ = _reseal_evidence(package, evidence)
    attestation["ocr_authority_lock_sha256"] = replay_sha256
    attestation_sha256 = _write_json(
        package.root / "review-ready.attestation.json", attestation
    )
    loaded_attestation = _load_attestation(
        package.root,
        release_id=_RELEASE_ID,
        expected_sha256=attestation_sha256,
        expected_registry_sha256=_REGISTRY_SHA256,
    )
    documents = _load_documents(package.root, expected_sha256=package.documents_sha256)

    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid"):
        _load_evidence(
            package.root,
            expected_sha256=attestation["ingestion_evidence_sha256"],
            documents=documents,
            attestation=loaded_attestation,
        )


@pytest.mark.parametrize("path_kind", ["symlink", "oversized", "directory"])
def test_v2_evidence_rejects_authority_path_bombs(
    tmp_path: Path, path_kind: str
) -> None:
    package = _write_package(tmp_path)
    lock_path = package.root / "ocr-authority-lock.json"
    if path_kind == "symlink":
        target = tmp_path / "authority-target.json"
        target.write_bytes(lock_path.read_bytes())
        lock_path.unlink()
        os.symlink(target, lock_path)
    elif path_kind == "oversized":
        lock_path.write_bytes(b"x" * 65_537)
    else:
        lock_path.unlink()
        lock_path.mkdir()
    attestation, documents = _loaded(package)

    with pytest.raises(FinalizationError, match="ingestion_evidence_invalid"):
        _load_evidence(
            package.root,
            expected_sha256=package.evidence_sha256,
            documents=documents,
            attestation=attestation,
        )


def test_attestation_rejects_oversized_integer_without_exception_value_leak(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    private = "9" * 5_000
    raw = (
        (package.root / "review-ready.attestation.json")
        .read_bytes()
        .replace(
            b'"case_count":1',
            b'"case_count":' + private.encode("ascii"),
        )
    )
    (package.root / "review-ready.attestation.json").write_bytes(raw)

    with pytest.raises(FinalizationError) as caught:
        _load_attestation(
            package.root,
            release_id=_RELEASE_ID,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_registry_sha256=_REGISTRY_SHA256,
        )

    rendered = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert private not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_valid_v2_evidence_still_stops_at_the_human_review_authority_gate(
    tmp_path: Path,
) -> None:
    package = _write_package(tmp_path)
    runtime_lock = tmp_path / "uv.lock"
    runtime_lock.write_bytes(b"runtime-lock\n")
    indexer_image_digest = "sha256:" + "b" * 64
    runtime_sha256 = tokenizer_runtime_fingerprint_sha256(
        runtime_lock.read_bytes(),
        indexer_image_digest=indexer_image_digest,
    )

    with pytest.raises(FinalizationError, match="review_authority_invalid") as caught:
        finalize_review_ready_bundle(
            package.root,
            tmp_path / "release",
            tmp_path / "diagnostics",
            tmp_path / "issuance.sqlite3",
            release_id=_RELEASE_ID,
            expected_ready_attestation_sha256=package.attestation_sha256,
            expected_registry_sha256=_REGISTRY_SHA256,
            expected_model_lock_sha256="c" * 64,
            expected_runtime_fingerprint_sha256=runtime_sha256,
            container_image="sha256:" + "d" * 64,
            runtime_lock_path=runtime_lock,
            indexer_image_digest=indexer_image_digest,
            embedding_model_lock=object(),
            embedding_model_root=tmp_path / "models",
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (tmp_path / "release").exists()
