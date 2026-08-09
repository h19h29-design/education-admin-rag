"""Atomic canonical bundle construction and semantic reproducibility hashes."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from src.corpus.chunking import (
    ChunkingError,
    tokenizer_contract,
    verify_embedding_cache,
)
from src.corpus.storage import (
    CanonicalStorageBatch,
    StorageError,
    acquire_issuance_lease,
    export_canonical_jsonl,
    write_canonical_storage,
)
from src.corpus.storage import (
    canonical_content_sha256 as _storage_content_sha256,
)

_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")


class BuildError(ValueError):
    """A fixed, value-free canonical build boundary failure."""


def _raise(message: str) -> NoReturn:
    raise BuildError(message) from None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_closed_sqlite_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        try:
            details = os.lstat(sidecar)
        except FileNotFoundError:
            continue
        except OSError:
            _raise("canonical build failed")
        if not stat.S_ISREG(details.st_mode):
            _raise("canonical build failed")
        try:
            sidecar.unlink()
        except OSError:
            _raise("canonical build failed")


def _prepare_trusted_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            details = os.lstat(current)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                _raise("canonical build output root is invalid")
    except OSError:
        _raise("canonical build output root is invalid")


def canonical_content_sha256(export_hashes: dict[str, str]) -> str:
    """Hash semantic tables while excluding run/release/SQLite physical state."""
    try:
        return _storage_content_sha256(export_hashes)
    except StorageError:
        pass
    _raise("canonical content export is incomplete")


@dataclass(frozen=True, slots=True)
class CanonicalBuildResult:
    release_id: str
    bundle_path: Path
    canonical_content_sha256: str
    bundle_sha256: str
    database_sha256: str
    export_sha256s: tuple[tuple[str, str], ...]
    issuance_generation: int
    issuance_authority_sha256: str


def _quarantine_totals(batch: object) -> tuple[int, int] | None:
    if type(batch) is not CanonicalStorageBatch:
        return None
    quarantined = 0
    failed = 0
    try:
        for run in batch.ingestion_runs:
            for counts in run.document_page_counts.values():
                quarantined += counts.quarantined
                failed += counts.failed
    except (AttributeError, TypeError, ValueError):
        return None
    return quarantined, failed


def _write_diagnostic(
    diagnostics_root: Path,
    *,
    release_id: str,
    quarantined_pages: int,
    failed_pages: int,
) -> None:
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    target = diagnostics_root / f"{release_id}.json"
    if target.exists() or target.is_symlink():
        _raise("canonical build diagnostic already exists")
    payload = (
        _canonical_json(
            {
                "failed_pages": failed_pages,
                "quarantined_pages": quarantined_pages,
                "release_id": release_id,
                "schema_version": "sen-qa-build-diagnostic/v1",
                "status": "review_required",
            }
        )
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-",
        dir=diagnostics_root,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    failed = False
    try:
        temporary.unlink()
        _write_file(temporary, payload)
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(
            diagnostics_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        failed = True
        try:
            temporary.unlink()
        except OSError:
            pass
    if failed:
        _raise("canonical build diagnostic failed")


def build_canonical_bundle(
    output_root: Path,
    diagnostics_root: Path,
    issuance_registry_path: Path,
    batch: object,
    *,
    expected_generation: int,
    expected_issuance_authority_sha256: str,
    expected_predecessor_bundle_sha256: str | None,
    expected_review_decision_snapshot_sha256: str,
    expected_registry_sha256: str,
    expected_chunk_set_sha256s: dict[str, str],
    expected_relation_approval_sha256s: dict[str, str],
    expected_model_lock_sha256: str,
    expected_runtime_fingerprint_sha256: str,
    embedding_model_lock: object,
    embedding_model_root: Path,
) -> CanonicalBuildResult:
    """Build, publish, and issue one complete canonical bundle."""
    totals = _quarantine_totals(batch)
    if (
        totals is None
        or type(batch) is not CanonicalStorageBatch
        or not isinstance(batch.release_id, str)
        or _RELEASE_RE.fullmatch(batch.release_id) is None
        or not isinstance(output_root, Path)
        or not isinstance(diagnostics_root, Path)
        or not isinstance(issuance_registry_path, Path)
        or not isinstance(embedding_model_root, Path)
    ):
        _raise("canonical build input is invalid")
    tokenizer_cache_invalid = False
    try:
        verify_embedding_cache(
            embedding_model_lock,
            embedding_model_root,
            scope="tokenizer",
            expected_lock_sha256=expected_model_lock_sha256,
        )
        expected_contract = tokenizer_contract(
            embedding_model_lock,
            expected_lock_sha256=expected_model_lock_sha256,
            runtime_fingerprint_sha256=expected_runtime_fingerprint_sha256,
        )
        if batch.tokenizer_contract != expected_contract:
            tokenizer_cache_invalid = True
    except (AttributeError, ChunkingError):
        tokenizer_cache_invalid = True
    if tokenizer_cache_invalid:
        _raise("canonical build tokenizer cache is invalid")
    _prepare_trusted_root(output_root)
    _prepare_trusted_root(diagnostics_root)
    quarantined_pages, failed_pages = totals
    if quarantined_pages or failed_pages:
        _write_diagnostic(
            diagnostics_root,
            release_id=batch.release_id,
            quarantined_pages=quarantined_pages,
            failed_pages=failed_pages,
        )
        _raise("canonical build requires complete nonquarantined coverage")
    target = output_root / batch.release_id
    if target.exists() or target.is_symlink():
        _raise("canonical bundle target already exists")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{batch.release_id}.tmp-", dir=output_root)
    )
    published = False
    try:
        with acquire_issuance_lease(
            issuance_registry_path,
            expected_generation=expected_generation,
            expected_authority_sha256=expected_issuance_authority_sha256,
            expected_predecessor_bundle_sha256=expected_predecessor_bundle_sha256,
        ) as lease:
            database = temporary / "canonical.sqlite3"
            receipt = write_canonical_storage(
                database,
                batch,
                issuance_lease=lease,
                expected_review_decision_snapshot_sha256=(
                    expected_review_decision_snapshot_sha256
                ),
                expected_registry_sha256=expected_registry_sha256,
                expected_chunk_set_sha256s=expected_chunk_set_sha256s,
                expected_relation_approval_sha256s=(expected_relation_approval_sha256s),
                expected_model_lock_sha256=expected_model_lock_sha256,
                expected_runtime_fingerprint_sha256=(
                    expected_runtime_fingerprint_sha256
                ),
            )
            export_hashes = export_canonical_jsonl(database, temporary / "jsonl")
            _remove_closed_sqlite_sidecars(database)
            content_sha256 = canonical_content_sha256(export_hashes)
            manifest = {
                "canonical_content_sha256": content_sha256,
                "database_sha256": receipt.database_sha256,
                "exports": dict(sorted(export_hashes.items())),
                "predecessor_bundle_sha256": expected_predecessor_bundle_sha256,
                "projection_sha256": receipt.projection_sha256,
                "release_id": batch.release_id,
                "schema_version": "sen-qa-canonical-bundle/v1",
            }
            manifest_bytes = _canonical_json(manifest) + b"\n"
            _write_file(temporary / "manifest.json", manifest_bytes)
            directory_fd = os.open(
                temporary,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary, target)
            published = True
            parent_fd = os.open(
                output_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            bound_receipt = lease.bind_published_bundle(
                receipt,
                bundle_path=target,
            )
            head = lease.commit_published_bundle(receipt=bound_receipt)
            return CanonicalBuildResult(
                release_id=batch.release_id,
                bundle_path=target,
                canonical_content_sha256=content_sha256,
                bundle_sha256=bound_receipt.bundle_sha256,
                database_sha256=receipt.database_sha256,
                export_sha256s=tuple(sorted(export_hashes.items())),
                issuance_generation=head.generation,
                issuance_authority_sha256=head.authority_sha256,
            )
    except (OSError, StorageError, BuildError, ValueError):
        if published:
            shutil.rmtree(target, ignore_errors=True)
        else:
            shutil.rmtree(temporary, ignore_errors=True)
    _raise("canonical build failed")
