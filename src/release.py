"""Fail-closed release evidence, promotion, and storage policy contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.corpus.ids import make_release_id
from src.corpus.models import Case, IngestionRun
from src.corpus.storage import StorageError, connect_canonical_storage
from src.evaluation.release_report import (
    ReleaseEvaluationReport,
    canonical_release_evaluation_bytes,
)
from src.ingestion.privacy import classify_privacy, scan_text
from src.retrieval.dense import DenseBuildResult
from src.retrieval.lexical import LexicalError, inspect_lexical_index

_RELEASE_RE = re.compile(r"^corpus-\d{14}-[0-9a-f]{8}$")
_COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,190}[a-z0-9]$")
_ALIAS = "education-admin-current"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_ATTESTATION_BYTES = 64 * 1024
_MAX_BACKUP_MANIFEST_BYTES = 64 * 1024
_MAX_BACKUP_FILE_BYTES = 32 * 1024 * 1024 * 1024
BACKUP_MANIFEST_NAME = "bundle-manifest.json"
BACKUP_PAYLOAD_PATHS = (
    "canonical/canonical.sqlite3",
    "qdrant/qdrant.snapshot",
    "source-manifest.json",
    "models.lock.json",
    "evaluation-report.json",
    "blind-labels.age",
)
_CANONICAL_EXPORTS = frozenset(
    f"{name}.jsonl"
    for name in (
        "build_meta",
        "documents",
        "issued_case_ids",
        "cases",
        "source_spans",
        "chunks",
        "chunk_source_spans",
        "law_refs",
        "case_relations",
        "corrections",
        "review_events",
        "ingestion_runs",
        "tokenizer_contract",
        "review_registry",
        "review_registry_locations",
        "case_authorities",
    )
)


class ReleaseError(RuntimeError):
    """A value-free release-orchestration failure."""


def _raise(code: str) -> NoReturn:
    raise ReleaseError(code) from None


class _ReleaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleaseAttestation(_ReleaseModel):
    schema_version: Literal["sen-qa-release-attestation/v1"] = (
        "sen-qa-release-attestation/v1"
    )
    kind: Literal["verification", "restore"]
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReleaseVerificationEvidence(_ReleaseModel):
    schema_version: Literal["sen-qa-release-verification-evidence/v1"]
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    canonical_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lexical_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dense_sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_chunks: int = Field(gt=0, le=1_000_000)
    lexical_chunks: int = Field(ge=0, le=1_000_000)
    dense_points: int = Field(ge=0, le=1_000_000)
    gold_items: int = Field(ge=0, le=200)
    blind_items: int = Field(ge=0, le=60)
    quarantined_pages: int = Field(ge=0, le=10_000)
    failed_pages: int = Field(ge=0, le=10_000)
    provenance_missing: int = Field(ge=0, le=1_000_000)
    privacy_findings_unresolved: int = Field(ge=0, le=1_000_000)
    warm_latency_p95_ms: float = Field(ge=0, le=86_400_000)
    review_gate: bool
    ingestion_gate: bool
    retrieval_gate: bool
    privacy_gate: bool

    @model_validator(mode="after")
    def all_release_gates_are_measured_and_green(self) -> ReleaseVerificationEvidence:
        if (
            self.lexical_chunks != self.eligible_chunks
            or self.dense_points != self.eligible_chunks
            or self.gold_items != 200
            or self.blind_items != 60
            or self.quarantined_pages != 0
            or self.failed_pages != 0
            or self.provenance_missing != 0
            or self.privacy_findings_unresolved != 0
            or self.warm_latency_p95_ms > 3_000.0
            or not all(
                (
                    self.review_gate,
                    self.ingestion_gate,
                    self.retrieval_gate,
                    self.privacy_gate,
                )
            )
        ):
            raise ValueError("release evidence gate failed")
        return self


class IndexReleaseEvidence(_ReleaseModel):
    schema_version: Literal["sen-qa-index-evidence/v1"]
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    canonical_database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lexical_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dense_sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_chunks: int = Field(gt=0, le=1_000_000)
    lexical_chunks: int = Field(gt=0, le=1_000_000)
    dense_points: int = Field(gt=0, le=1_000_000)
    collection_name: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}-bge-m3$")

    @model_validator(mode="after")
    def counts_and_collection_match_release(self) -> IndexReleaseEvidence:
        if (
            self.eligible_chunks != self.lexical_chunks
            or self.eligible_chunks != self.dense_points
            or self.collection_name != f"{self.release_id}-bge-m3"
        ):
            raise ValueError("index release evidence mismatch")
        return self


class _CanonicalBundleEvidence(_ReleaseModel):
    schema_version: Literal["sen-qa-canonical-bundle/v1"]
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_bundle_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    exports: dict[str, str]

    @model_validator(mode="after")
    def exports_are_exact_and_hashed(self) -> _CanonicalBundleEvidence:
        if set(self.exports) != _CANONICAL_EXPORTS or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in self.exports.values()
        ):
            raise ValueError("canonical bundle exports mismatch")
        return self


class PromotionResult(_ReleaseModel):
    promoted: bool
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def status_matches_error(self) -> PromotionResult:
        if self.promoted == (self.error_code is not None):
            raise ValueError("promotion result state is invalid")
        return self


class ReleaseReadiness(_ReleaseModel):
    ready: bool
    release_id: str | None = Field(default=None, pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    collection_name: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,190}[a-z0-9]$"
    )
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def readiness_matches_evidence(self) -> ReleaseReadiness:
        if self.ready != (
            self.error_code is None
            and self.release_id is not None
            and self.collection_name is not None
        ):
            raise ValueError("release readiness is invalid")
        return self


class StoragePolicy(_ReleaseModel):
    schema_version: Literal["sen-qa-storage-policy/v1"]
    ingestion_uid: int = Field(gt=0, le=2_147_483_647)
    search_uid: int = Field(gt=0, le=2_147_483_647)
    evaluator_uid: int = Field(gt=0, le=2_147_483_647)
    reviewer_gid: int = Field(gt=0, le=2_147_483_647)
    source_root: str = Field(min_length=2, max_length=4_096)
    artifact_root: str = Field(min_length=2, max_length=4_096)
    private_eval_root: str = Field(min_length=2, max_length=4_096)

    @model_validator(mode="after")
    def identities_and_roots_are_isolated(self) -> StoragePolicy:
        if len({self.ingestion_uid, self.search_uid, self.evaluator_uid}) != 3:
            raise ValueError("service identities must be distinct")
        raw_roots = (
            self.source_root,
            self.artifact_root,
            self.private_eval_root,
        )
        if any(any(ord(character) < 32 for character in root) for root in raw_roots):
            raise ValueError("storage roots contain control characters")
        roots = tuple(Path(value) for value in raw_roots)
        if any(not root.is_absolute() for root in roots):
            raise ValueError("storage roots must be absolute")
        normalized = tuple(Path(os.path.normpath(root)) for root in roots)
        if any(root == Path(root.anchor) for root in normalized) or any(
            left.is_relative_to(right) or right.is_relative_to(left)
            for index, left in enumerate(normalized)
            for right in normalized[index + 1 :]
        ):
            raise ValueError("storage roots must be disjoint")
        return self


class ReleaseEnvironment(_ReleaseModel):
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    source_root: str = Field(min_length=2, max_length=4_096)
    artifact_root: str = Field(min_length=2, max_length=4_096)
    private_eval_root: str = Field(min_length=2, max_length=4_096)


class ToolArchiveLock(_ReleaseModel):
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){1,2}$")
    archive_url: str = Field(min_length=20, max_length=1_000)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(gt=0, le=50_000_000)
    binary_path: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,200}$")


class BackupToolLock(_ReleaseModel):
    schema_version: Literal["sen-qa-backup-tools/v1"]
    age: ToolArchiveLock
    minisign: ToolArchiveLock

    @model_validator(mode="after")
    def archives_are_immutable_official_linux_assets(self) -> BackupToolLock:
        expected_age_url = (
            "https://github.com/FiloSottile/age/releases/download/"
            f"v{self.age.version}/age-v{self.age.version}-linux-amd64.tar.gz"
        )
        expected_minisign_url = (
            "https://github.com/jedisct1/minisign/releases/download/"
            f"{self.minisign.version}/minisign-{self.minisign.version}-linux.tar.gz"
        )
        if (
            self.age.archive_url != expected_age_url
            or self.age.binary_path != "age/age"
            or self.minisign.archive_url != expected_minisign_url
            or self.minisign.binary_path != "minisign-linux/x86_64/minisign"
        ):
            raise ValueError("backup tool archives are invalid")
        return self


class BackupFileEntry(_ReleaseModel):
    path: str = Field(pattern=r"^[a-z0-9][a-z0-9./-]{0,199}$")
    size: int = Field(gt=0, le=_MAX_BACKUP_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupManifest(_ReleaseModel):
    schema_version: Literal["sen-qa-backup-bundle/v1"] = "sen-qa-backup-bundle/v1"
    release_id: str = Field(pattern=r"^corpus-\d{14}-[0-9a-f]{8}$")
    files: tuple[BackupFileEntry, ...]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AliasBackend(Protocol):
    def current_target(self, alias_name: str) -> str | None: ...

    def compare_and_swap(
        self,
        alias_name: str,
        expected_collection: str,
        new_collection: str,
    ) -> bool: ...


def _canonical_json(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _revalidate_release_model(
    value: object, model_type: type[_ReleaseModel]
) -> _ReleaseModel | None:
    if type(value) is not model_type or type(value.__dict__) is not dict:
        return None
    raw = dict(value.__dict__)
    if set(raw) != set(model_type.model_fields):
        return None
    try:
        return model_type.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def _write_new_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        _raise("release_artifact_exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, path, follow_symlinks=False)
        Path(temporary_name).unlink()
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except ReleaseError:
        raise
    except (OSError, TypeError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary_name).unlink()
        except OSError:
            pass
        _raise("release_artifact_write_failed")


def write_release_attestation(path: Path, attestation: ReleaseAttestation) -> Path:
    """Write a deterministic immutable verification or restore attestation."""
    checked = _revalidate_release_model(attestation, ReleaseAttestation)
    if not isinstance(path, Path) or not isinstance(checked, ReleaseAttestation):
        _raise("attestation_invalid")
    _write_new_file(path, _canonical_json(checked))
    return path


def create_verification_attestation(
    evidence_path: Path,
    *,
    output: Path,
    expected_release_id: str,
) -> ReleaseAttestation:
    """Create an immutable verification attestation from complete green evidence."""
    if (
        not isinstance(evidence_path, Path)
        or not isinstance(output, Path)
        or type(expected_release_id) is not str
        or _RELEASE_RE.fullmatch(expected_release_id) is None
    ):
        _raise("release_evidence_invalid")
    payload = _read_bounded(evidence_path, limit=_MAX_ATTESTATION_BYTES)
    evidence: ReleaseVerificationEvidence | None = None
    try:
        decoded = (
            json.loads(payload, object_pairs_hook=_unique_object)
            if payload is not None
            else None
        )
        evidence = ReleaseVerificationEvidence.model_validate(decoded)
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        pass
    if (
        evidence is None
        or evidence.release_id != expected_release_id
        or _canonical_json(evidence) != payload
    ):
        _raise("release_evidence_invalid")
    attestation = ReleaseAttestation(
        kind="verification",
        release_id=evidence.release_id,
        bundle_sha256=evidence.canonical_bundle_sha256,
    )
    write_release_attestation(output, attestation)
    return attestation


def create_restore_attestation(
    bundle_root: Path,
    restored_root: Path,
    evaluation_report: Path,
    *,
    output: Path,
    expected_release_id: str,
) -> ReleaseAttestation:
    """Attest only a green evaluation of exact, isolated restored bytes."""
    if (
        not all(
            isinstance(path, Path)
            for path in (bundle_root, restored_root, evaluation_report, output)
        )
        or type(expected_release_id) is not str
        or _RELEASE_RE.fullmatch(expected_release_id) is None
    ):
        _raise("restore_evidence_invalid")
    manifest = verify_backup_manifest(bundle_root)
    evaluation = _load_exact_evaluation(evaluation_report)
    try:
        nodes = {path.name for path in restored_root.iterdir()}
    except OSError:
        nodes = set()
    canonical = _backup_file_digest(restored_root, "canonical.sqlite3")
    qdrant = _backup_file_digest(restored_root, "qdrant.snapshot")
    expected = {entry.path: entry for entry in manifest.files}
    if (
        manifest.release_id != expected_release_id
        or evaluation is None
        or evaluation.release_id != expected_release_id
        or not evaluation.ingestion_gate
        or not evaluation.retrieval_gate
        or nodes != {"canonical.sqlite3", "qdrant.snapshot"}
        or canonical is None
        or qdrant is None
        or canonical.size != expected["canonical/canonical.sqlite3"].size
        or qdrant.size != expected["qdrant/qdrant.snapshot"].size
        or not hmac.compare_digest(
            canonical.sha256,
            expected["canonical/canonical.sqlite3"].sha256,
        )
        or not hmac.compare_digest(
            qdrant.sha256,
            expected["qdrant/qdrant.snapshot"].sha256,
        )
        or not hmac.compare_digest(
            evaluation.canonical_database_sha256,
            canonical.sha256,
        )
    ):
        _raise("restore_evidence_invalid")
    attestation = ReleaseAttestation(
        kind="restore",
        release_id=expected_release_id,
        bundle_sha256=manifest.bundle_sha256,
    )
    write_release_attestation(output, attestation)
    return attestation


def _checked_existing_roots(roots: tuple[Path, Path, Path]) -> tuple[Path, ...] | None:
    checked: list[Path] = []
    try:
        for root in roots:
            absolute = Path(os.path.abspath(root))
            if absolute == Path(absolute.anchor) or any(
                ord(character) < 32 or character in {":", ","}
                for character in str(absolute)
            ):
                return None
            details = os.lstat(absolute)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                return None
            checked.append(absolute)
    except OSError:
        return None
    if len(set(checked)) != len(checked):
        return None
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(checked)
        for right in checked[index + 1 :]
    ):
        return None
    return tuple(checked)


def start_release_environment(
    *,
    source_root: Path,
    artifact_root: Path,
    private_eval_root: Path,
    env_file: Path,
    released_at: datetime,
    git_sha: str,
) -> ReleaseEnvironment:
    """Create one immutable, minimal, shell-readable active release envelope."""
    if (
        not all(
            isinstance(path, Path)
            for path in (source_root, artifact_root, private_eval_root, env_file)
        )
        or type(released_at) is not datetime
        or type(git_sha) is not str
    ):
        _raise("release_environment_invalid")
    roots = _checked_existing_roots((source_root, artifact_root, private_eval_root))
    if roots is None:
        _raise("release_environment_invalid")
    checked_source, checked_artifact, checked_private = roots
    checked_env = Path(os.path.abspath(env_file))
    if (
        checked_env.parent != checked_artifact
        or checked_env.name != "active-release.env"
    ):
        _raise("release_environment_invalid")
    release_id: str | None = None
    try:
        if released_at.tzinfo is None or released_at.utcoffset() != UTC.utcoffset(
            released_at
        ):
            raise ValueError
        release_id = make_release_id(released_at, git_sha)
    except (TypeError, ValueError):
        pass
    if release_id is None:
        _raise("release_environment_invalid")
    environment = ReleaseEnvironment(
        release_id=release_id,
        source_root=str(checked_source),
        artifact_root=str(checked_artifact),
        private_eval_root=str(checked_private),
    )
    lines = (
        ("SEN_QA_RELEASE_ID", environment.release_id),
        ("SEN_QA_SOURCE_ROOT", environment.source_root),
        ("SEN_QA_ARTIFACT_ROOT", environment.artifact_root),
        ("SEN_QA_PRIVATE_EVAL_ROOT", environment.private_eval_root),
    )
    payload = "".join(f"{key}={shlex.quote(value)}\n" for key, value in lines).encode(
        "utf-8"
    )
    failed = False
    try:
        _write_new_file(checked_env, payload)
    except ReleaseError:
        failed = True
    if failed:
        _raise("release_environment_invalid")
    return environment


def _read_bounded(path: Path, *, limit: int) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= limit:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            return None
        payload = b"".join(chunks)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return payload


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _load_attestation(path: Path) -> ReleaseAttestation | None:
    payload = _read_bounded(path, limit=_MAX_ATTESTATION_BYTES)
    if payload is None:
        return None
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        checked = ReleaseAttestation.model_validate(decoded)
        if _canonical_json(checked) != payload:
            return None
        return checked
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        return None


def load_storage_policy(path: Path) -> StoragePolicy:
    """Load strict service identities and disjoint NAS roots from TOML."""
    if not isinstance(path, Path):
        _raise("storage_policy_invalid")
    payload = _read_bounded(path, limit=_MAX_CONFIG_BYTES)
    policy: StoragePolicy | None = None
    try:
        decoded = (
            tomllib.loads(payload.decode("utf-8")) if payload is not None else None
        )
        policy = StoragePolicy.model_validate(decoded)
    except (
        UnicodeError,
        tomllib.TOMLDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        pass
    if policy is None:
        _raise("storage_policy_invalid")
    return policy


def load_backup_tool_lock(path: Path) -> BackupToolLock:
    """Load exact Linux/amd64 age and minisign archive metadata."""
    if not isinstance(path, Path):
        _raise("backup_tool_lock_invalid")
    payload = _read_bounded(path, limit=_MAX_CONFIG_BYTES)
    lock: BackupToolLock | None = None
    try:
        decoded = (
            json.loads(payload, object_pairs_hook=_unique_object)
            if payload is not None
            else None
        )
        lock = BackupToolLock.model_validate(decoded)
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        pass
    if lock is None:
        _raise("backup_tool_lock_invalid")
    return lock


def _backup_expected_nodes(*, include_manifest: bool) -> frozenset[str]:
    files = set(BACKUP_PAYLOAD_PATHS)
    if include_manifest:
        files.add(BACKUP_MANIFEST_NAME)
    directories = {
        str(parent)
        for name in files
        for parent in Path(name).parents
        if str(parent) != "."
    }
    return frozenset(files | directories)


def _backup_nodes(root: Path) -> frozenset[str] | None:
    try:
        details = os.lstat(root)
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            return None
        nodes: set[str] = set()
        for candidate in root.rglob("*"):
            relative = candidate.relative_to(root).as_posix()
            candidate_details = os.lstat(candidate)
            if stat.S_ISLNK(candidate_details.st_mode) or not (
                stat.S_ISDIR(candidate_details.st_mode)
                or stat.S_ISREG(candidate_details.st_mode)
            ):
                return None
            nodes.add(relative)
        return frozenset(nodes)
    except (OSError, ValueError):
        return None


def _backup_file_digest(root: Path, relative: str) -> BackupFileEntry | None:
    descriptors: list[int] = []
    try:
        current = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(current)
        parts = Path(relative).parts
        for component in parts[:-1]:
            current = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            descriptors.append(current)
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current,
        )
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= _MAX_BACKUP_FILE_BYTES
        ):
            return None
        digest = hashlib.sha256()
        total = 0
        while total <= _MAX_BACKUP_FILE_BYTES:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(file_descriptor)
        if (
            total != before.st_size
            or total > _MAX_BACKUP_FILE_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            return None
        return BackupFileEntry(
            path=relative,
            size=total,
            sha256=digest.hexdigest(),
        )
    except (OSError, ValidationError, TypeError, ValueError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_regular_path(path: Path) -> tuple[int, os.stat_result] | None:
    descriptors: list[int] = []
    retained = -1
    try:
        absolute = Path(os.path.abspath(path))
        current = os.open(
            absolute.anchor,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        descriptors.append(current)
        for component in absolute.parts[1:-1]:
            current = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            descriptors.append(current)
        retained = os.open(
            absolute.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current,
        )
        details = os.fstat(retained)
        if (
            not stat.S_ISREG(details.st_mode)
            or not 1 <= details.st_size <= _MAX_BACKUP_FILE_BYTES
        ):
            os.close(retained)
            retained = -1
            return None
        return retained, details
    except OSError:
        if retained >= 0:
            try:
                os.close(retained)
            except OSError:
                pass
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _stable_file_sha256(path: Path) -> str | None:
    opened = _open_regular_path(path)
    if opened is None:
        return None
    descriptor, before = opened
    digest = hashlib.sha256()
    total = 0
    failed = False
    try:
        while total <= _MAX_BACKUP_FILE_BYTES:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != before.st_size
            or total > _MAX_BACKUP_FILE_BYTES
            or os.read(descriptor, 1)
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            failed = True
    except OSError:
        failed = True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    return None if failed else digest.hexdigest()


def create_index_release_evidence(
    *,
    canonical_database: Path,
    lexical_index: Path,
    dense_result: DenseBuildResult,
    output: Path,
    expected_release_id: str,
) -> IndexReleaseEvidence:
    """Bind physical canonical/lexical bytes to one verified dense candidate."""
    if (
        not all(
            isinstance(path, Path)
            for path in (canonical_database, lexical_index, output)
        )
        or type(expected_release_id) is not str
        or _RELEASE_RE.fullmatch(expected_release_id) is None
        or type(dense_result) is not DenseBuildResult
    ):
        _raise("index_evidence_invalid")
    canonical_sha256 = _stable_file_sha256(canonical_database)
    lexical_sha256 = _stable_file_sha256(lexical_index)
    failed = False
    canonical_release: object = None
    lexical_metadata = None
    try:
        with connect_canonical_storage(canonical_database) as connection:
            row = connection.execute(
                "SELECT release_id FROM build_meta WHERE singleton=1"
            ).fetchone()
            canonical_release = row[0] if type(row) is tuple and len(row) == 1 else None
        lexical_metadata = inspect_lexical_index(lexical_index)
    except (StorageError, LexicalError, OSError, sqlite3.Error, TypeError, ValueError):
        failed = True
    if (
        failed
        or canonical_sha256 is None
        or lexical_sha256 is None
        or _stable_file_sha256(canonical_database) != canonical_sha256
        or _stable_file_sha256(lexical_index) != lexical_sha256
        or canonical_release != expected_release_id
        or lexical_metadata is None
        or lexical_metadata.release_id != expected_release_id
        or dense_result.release_id != expected_release_id
        or dense_result.collection_name != f"{expected_release_id}-bge-m3"
        or type(dense_result.embedding_version) is not str
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", dense_result.embedding_version
        )
        is None
        or type(dense_result.point_count) is not int
        or dense_result.point_count != lexical_metadata.indexed_chunks
        or type(dense_result.sampled_vector_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", dense_result.sampled_vector_sha256) is None
    ):
        _raise("index_evidence_invalid")
    try:
        evidence = IndexReleaseEvidence(
            schema_version="sen-qa-index-evidence/v1",
            release_id=expected_release_id,
            canonical_database_sha256=canonical_sha256,
            lexical_index_sha256=lexical_sha256,
            dense_sample_sha256=dense_result.sampled_vector_sha256,
            eligible_chunks=lexical_metadata.indexed_chunks,
            lexical_chunks=lexical_metadata.indexed_chunks,
            dense_points=dense_result.point_count,
            collection_name=dense_result.collection_name,
        )
    except (ValidationError, TypeError, ValueError):
        _raise("index_evidence_invalid")
    _write_new_file(output, _canonical_json(evidence))
    return evidence


_EvidenceModelT = TypeVar("_EvidenceModelT", bound=_ReleaseModel)


def _load_exact_evidence_model(
    path: Path, model_type: type[_EvidenceModelT]
) -> tuple[_EvidenceModelT, bytes] | None:
    payload = _read_bounded(path, limit=_MAX_ATTESTATION_BYTES)
    if payload is None:
        return None
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        if type(decoded) is not dict:
            return None
        checked = model_type.model_validate_json(payload)
        if _canonical_json(checked) != payload:
            return None
        return checked, payload
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        return None


def _load_exact_evaluation(path: Path) -> ReleaseEvaluationReport | None:
    payload = _read_bounded(path, limit=_MAX_ATTESTATION_BYTES)
    if payload is None:
        return None
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        if type(decoded) is not dict:
            return None
        checked = ReleaseEvaluationReport.model_validate_json(payload)
        if canonical_release_evaluation_bytes(checked) != payload:
            return None
        return checked
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        return None


def _case_privacy_matches(case: Case) -> bool:
    if not case.search_eligible:
        return True
    text = "\n".join(
        value
        for value in (
            case.title_raw,
            case.title_normalized,
            case.question,
            case.answer,
            case.facts,
            case.basis_text,
        )
        if value is not None
    )
    try:
        findings = scan_text(
            text,
            location_id=f"case-{case.case_id}:canonical",
            case_type=case.case_type,
        )
        decision = classify_privacy(
            findings,
            case_type=case.case_type,
            audit_masked=case.pii_class == "anonymized_case",
            proposed_search_eligible=case.search_eligible,
            proposed_answer_eligible=case.answer_eligible,
        )
    except (TypeError, ValueError):
        return False
    return (
        decision.pii_class == case.pii_class
        and decision.search_eligible == case.search_eligible
        and decision.answer_eligible == case.answer_eligible
    )


def assemble_release_verification_evidence(
    *,
    canonical_manifest: Path,
    canonical_database: Path,
    lexical_index: Path,
    index_evidence_path: Path,
    evaluation_report: Path,
    output: Path,
    expected_release_id: str,
) -> ReleaseVerificationEvidence:
    """Derive release gates from exact canonical, index, and evaluation artifacts."""
    if (
        type(expected_release_id) is not str
        or _RELEASE_RE.fullmatch(expected_release_id) is None
        or not all(
            isinstance(path, Path)
            for path in (
                canonical_manifest,
                canonical_database,
                lexical_index,
                index_evidence_path,
                evaluation_report,
                output,
            )
        )
    ):
        _raise("release_evidence_invalid")
    loaded_manifest = _load_exact_evidence_model(
        canonical_manifest, _CanonicalBundleEvidence
    )
    loaded_index = _load_exact_evidence_model(index_evidence_path, IndexReleaseEvidence)
    evaluation = _load_exact_evaluation(evaluation_report)
    database_sha256 = _stable_file_sha256(canonical_database)
    lexical_sha256 = _stable_file_sha256(lexical_index)
    if loaded_manifest is None or loaded_index is None or evaluation is None:
        _raise("release_evidence_invalid")
    manifest, manifest_bytes = loaded_manifest
    index_evidence, _ = loaded_index
    bundle_sha256 = hashlib.sha256(
        b"sen-qa-canonical-bundle-v1\0" + manifest_bytes
    ).hexdigest()
    failed = False
    cases: tuple[Case, ...] = ()
    runs: tuple[IngestionRun, ...] = ()
    chunk_count = -1
    review_location_cases = -1
    case_authorities = -1
    registry_count = -1
    build_release: object = None
    try:
        with connect_canonical_storage(canonical_database) as connection:
            connection.execute("BEGIN")
            build_row = connection.execute(
                "SELECT release_id FROM build_meta WHERE singleton=1"
            ).fetchone()
            build_release = (
                build_row[0]
                if type(build_row) is tuple and len(build_row) == 1
                else None
            )
            raw_cases = connection.execute(
                "SELECT payload_json FROM cases ORDER BY case_id"
            ).fetchall()
            raw_runs = connection.execute(
                "SELECT payload_json FROM ingestion_runs ORDER BY run_id"
            ).fetchall()
            cases = tuple(Case.model_validate_json(row[0]) for row in raw_cases)
            runs = tuple(IngestionRun.model_validate_json(row[0]) for row in raw_runs)
            chunk_count = cast(
                int, connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            )
            review_location_cases = cast(
                int,
                connection.execute(
                    "SELECT count(DISTINCT case_id) FROM review_registry_locations"
                ).fetchone()[0],
            )
            case_authorities = cast(
                int,
                connection.execute("SELECT count(*) FROM case_authorities").fetchone()[
                    0
                ],
            )
            registry_count = cast(
                int,
                connection.execute("SELECT count(*) FROM review_registry").fetchone()[
                    0
                ],
            )
            connection.execute("ROLLBACK")
    except (
        StorageError,
        sqlite3.Error,
        ValidationError,
        TypeError,
        ValueError,
    ):
        failed = True
    quarantined_pages = sum(
        counts.quarantined
        for run in runs
        for counts in run.document_page_counts.values()
    )
    failed_pages = sum(
        counts.failed for run in runs for counts in run.document_page_counts.values()
    )
    hybrid = next(
        (item for item in evaluation.retrieval if item.system == "hybrid"), None
    )
    if (
        failed
        or database_sha256 is None
        or lexical_sha256 is None
        or manifest.release_id != expected_release_id
        or index_evidence.release_id != expected_release_id
        or evaluation.release_id != expected_release_id
        or evaluation.canonical_database_sha256 != database_sha256
        or build_release != expected_release_id
        or manifest.database_sha256 != database_sha256
        or index_evidence.canonical_database_sha256 != database_sha256
        or index_evidence.lexical_index_sha256 != lexical_sha256
        or chunk_count != index_evidence.eligible_chunks
        or not cases
        or len(runs) != 1
        or runs[0].release_id != expected_release_id
        or runs[0].ended_at is None
        or not isinstance(runs[0].approved_by, str)
        or re.fullmatch(r"review-snapshot:[0-9a-f]{64}", runs[0].approved_by) is None
        or any(
            case.review_status not in {"search_approved", "approved", "rejected"}
            for case in cases
        )
        or any(
            case.search_eligible
            and case.pii_class
            not in {
                "none",
                "anonymized_case",
                "quasi_identifier",
            }
            for case in cases
        )
        or any(not _case_privacy_matches(case) for case in cases)
        or review_location_cases != len(cases)
        or case_authorities != len(cases)
        or registry_count != 1
        or hybrid is None
        or len(evaluation.retrieval) != 4
        or {item.system for item in evaluation.retrieval}
        != {"substring", "lexical", "dense", "hybrid"}
    ):
        _raise("release_evidence_invalid")
    try:
        evidence = ReleaseVerificationEvidence(
            schema_version="sen-qa-release-verification-evidence/v1",
            release_id=expected_release_id,
            canonical_bundle_sha256=bundle_sha256,
            canonical_content_sha256=manifest.canonical_content_sha256,
            lexical_index_sha256=index_evidence.lexical_index_sha256,
            dense_sample_sha256=index_evidence.dense_sample_sha256,
            eligible_chunks=index_evidence.eligible_chunks,
            lexical_chunks=index_evidence.lexical_chunks,
            dense_points=index_evidence.dense_points,
            gold_items=evaluation.gold_items,
            blind_items=evaluation.blind_items,
            quarantined_pages=quarantined_pages,
            failed_pages=failed_pages,
            provenance_missing=evaluation.ingestion.provenance_missing_count,
            privacy_findings_unresolved=0,
            warm_latency_p95_ms=hybrid.warm_latency_p95_ms.total_ms,
            review_gate=True,
            ingestion_gate=evaluation.ingestion_gate,
            retrieval_gate=evaluation.retrieval_gate,
            privacy_gate=True,
        )
    except (ValidationError, TypeError, ValueError):
        _raise("release_evidence_invalid")
    _write_new_file(output, _canonical_json(evidence))
    return evidence


def _copy_open_file(descriptor: int, before: os.stat_result, target: Path) -> bool:
    total = 0
    failed = False
    try:
        with target.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            while total <= _MAX_BACKUP_FILE_BYTES:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        after = os.fstat(descriptor)
        if (
            total != before.st_size
            or total > _MAX_BACKUP_FILE_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            failed = True
    except OSError:
        failed = True
    return not failed


def prepare_backup_payload(
    output: Path,
    *,
    canonical_database: Path,
    qdrant_snapshot: Path,
    source_manifest: Path,
    model_lock: Path,
    evaluation_report: Path,
) -> Path:
    """Stage exact public artifacts and an online SQLite backup in a new root."""
    inputs = (
        canonical_database,
        qdrant_snapshot,
        source_manifest,
        model_lock,
        evaluation_report,
    )
    if (
        not isinstance(output, Path)
        or any(not isinstance(path, Path) for path in inputs)
        or output.exists()
        or output.is_symlink()
    ):
        _raise("backup_payload_invalid")
    opened = tuple(_open_regular_path(path) for path in inputs)
    if any(item is None for item in opened):
        for item in opened:
            if item is not None:
                os.close(item[0])
        _raise("backup_payload_invalid")
    checked = tuple(item for item in opened if item is not None)
    staging: Path | None = None
    failed = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.mkdir(mode=0o700)
        staging = output
        (staging / "canonical").mkdir(mode=0o700)
        (staging / "qdrant").mkdir(mode=0o700)

        _database_descriptor, database_details = checked[0]
        anchor = staging / ".canonical-source.sqlite3"
        os.link(canonical_database, anchor, follow_symlinks=False)
        anchor_details = os.lstat(anchor)
        if not stat.S_ISREG(anchor_details.st_mode) or (
            anchor_details.st_dev,
            anchor_details.st_ino,
        ) != (database_details.st_dev, database_details.st_ino):
            failed = True
        if not failed:
            destination = staging / "canonical/canonical.sqlite3"
            with (
                sqlite3.connect(f"file:{anchor}?mode=ro", uri=True) as source,
                sqlite3.connect(destination) as target_connection,
            ):
                source.backup(target_connection)
                if target_connection.execute("PRAGMA integrity_check").fetchone() != (
                    "ok",
                ):
                    failed = True
            destination.chmod(0o600)
            anchor.unlink()

        copies = (
            (checked[1], staging / "qdrant/qdrant.snapshot"),
            (checked[2], staging / "source-manifest.json"),
            (checked[3], staging / "models.lock.json"),
            (checked[4], staging / "evaluation-report.json"),
        )
        for (descriptor, details), target in copies:
            if failed or not _copy_open_file(descriptor, details, target):
                failed = True
                break
        if not failed:
            directory = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            staging = None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        failed = True
    finally:
        for descriptor, _details in checked:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if failed:
        _raise("backup_payload_invalid")
    return output


def _backup_manifest_digest(release_id: str, files: tuple[BackupFileEntry, ...]) -> str:
    payload = {
        "files": [entry.model_dump(mode="json") for entry in files],
        "release_id": release_id,
        "schema_version": "sen-qa-backup-bundle-content/v1",
    }
    return hashlib.sha256(
        b"sen-qa-backup-bundle-v1\0"
        + json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def create_backup_manifest(root: Path, *, release_id: str) -> BackupManifest:
    """Create the one immutable manifest for an exact encrypted backup payload."""
    if (
        not isinstance(root, Path)
        or type(release_id) is not str
        or _RELEASE_RE.fullmatch(release_id) is None
        or _backup_nodes(root) != _backup_expected_nodes(include_manifest=False)
    ):
        _raise("backup_bundle_invalid")
    entries = tuple(_backup_file_digest(root, path) for path in BACKUP_PAYLOAD_PATHS)
    if any(entry is None for entry in entries):
        _raise("backup_bundle_invalid")
    checked_entries = tuple(entry for entry in entries if entry is not None)
    manifest = BackupManifest(
        release_id=release_id,
        files=checked_entries,
        bundle_sha256=_backup_manifest_digest(release_id, checked_entries),
    )
    _write_new_file(root / BACKUP_MANIFEST_NAME, _canonical_json(manifest))
    return manifest


def verify_backup_manifest(root: Path) -> BackupManifest:
    """Rehash every declared backup artifact and reject extras or substitutions."""
    if not isinstance(root, Path) or _backup_nodes(root) != _backup_expected_nodes(
        include_manifest=True
    ):
        _raise("backup_bundle_invalid")
    payload = _read_bounded(
        root / BACKUP_MANIFEST_NAME, limit=_MAX_BACKUP_MANIFEST_BYTES
    )
    manifest: BackupManifest | None = None
    try:
        decoded = (
            json.loads(payload, object_pairs_hook=_unique_object)
            if payload is not None
            else None
        )
        if type(decoded) is not dict or type(decoded.get("files")) is not list:
            raise TypeError
        decoded["files"] = tuple(decoded["files"])
        manifest = BackupManifest.model_validate(decoded)
    except (
        json.JSONDecodeError,
        _DuplicateKey,
        ValidationError,
        TypeError,
        ValueError,
    ):
        pass
    if (
        manifest is None
        or _canonical_json(manifest) != payload
        or tuple(entry.path for entry in manifest.files) != BACKUP_PAYLOAD_PATHS
    ):
        _raise("backup_bundle_invalid")
    actual = tuple(_backup_file_digest(root, path) for path in BACKUP_PAYLOAD_PATHS)
    if (
        any(entry is None for entry in actual)
        or tuple(entry for entry in actual if entry is not None) != manifest.files
        or _backup_manifest_digest(manifest.release_id, manifest.files)
        != manifest.bundle_sha256
    ):
        _raise("backup_bundle_invalid")
    return manifest


def materialize_backup_restore(bundle_root: Path, output: Path) -> Path:
    """Copy verified canonical/Qdrant bytes into a new isolated restore root."""
    if (
        not isinstance(bundle_root, Path)
        or not isinstance(output, Path)
        or output.exists()
        or output.is_symlink()
    ):
        _raise("backup_restore_invalid")
    manifest = verify_backup_manifest(bundle_root)
    selected = (
        ("canonical/canonical.sqlite3", "canonical.sqlite3"),
        ("qdrant/qdrant.snapshot", "qdrant.snapshot"),
    )
    expected = {entry.path: entry for entry in manifest.files}
    opened = tuple(_open_regular_path(bundle_root / source) for source, _ in selected)
    if any(item is None for item in opened):
        for item in opened:
            if item is not None:
                os.close(item[0])
        _raise("backup_restore_invalid")
    checked = tuple(item for item in opened if item is not None)
    created = False
    failed = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.mkdir(mode=0o700)
        created = True
        for (source_name, target_name), (descriptor, details) in zip(
            selected, checked, strict=True
        ):
            target = output / target_name
            if not _copy_open_file(descriptor, details, target):
                failed = True
                break
            actual = _backup_file_digest(output, target_name)
            pinned = expected[source_name]
            if (
                actual is None
                or actual.size != pinned.size
                or not hmac.compare_digest(actual.sha256, pinned.sha256)
            ):
                failed = True
                break
        if not failed:
            directory = os.open(output, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except (OSError, TypeError, ValueError):
        failed = True
    finally:
        for descriptor, _details in checked:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if failed:
        if created:
            shutil.rmtree(output, ignore_errors=True)
        _raise("backup_restore_invalid")
    return output


def _load_current_manifest(path: Path) -> dict[str, str] | None:
    payload = _read_bounded(path, limit=_MAX_ATTESTATION_BYTES)
    try:
        decoded = (
            json.loads(payload, object_pairs_hook=_unique_object)
            if payload is not None
            else None
        )
    except (json.JSONDecodeError, _DuplicateKey, TypeError, ValueError):
        return None
    if (
        type(decoded) is not dict
        or set(decoded) != {"schema_version", "release_id", "collection_name"}
        or decoded.get("schema_version") != "sen-qa-current-release/v1"
        or type(decoded.get("release_id")) is not str
        or _RELEASE_RE.fullmatch(decoded["release_id"]) is None
        or type(decoded.get("collection_name")) is not str
        or _COLLECTION_RE.fullmatch(decoded["collection_name"]) is None
    ):
        return None
    return decoded


def _promotion_failure(code: str) -> PromotionResult:
    return PromotionResult(promoted=False, error_code=code)


def reconcile_release_state(
    *, release_root: Path, alias_backend: AliasBackend
) -> ReleaseReadiness:
    """Fail search readiness unless current.json and the live alias agree exactly."""
    if not isinstance(release_root, Path):
        return ReleaseReadiness(ready=False, error_code="current_manifest_invalid")
    current = _load_current_manifest(release_root / "current.json")
    if current is None:
        return ReleaseReadiness(ready=False, error_code="current_manifest_invalid")
    alias_target: str | None = None
    try:
        alias_target = alias_backend.current_target(_ALIAS)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    if alias_target != current["collection_name"]:
        return ReleaseReadiness(ready=False, error_code="alias_manifest_mismatch")
    return ReleaseReadiness(
        ready=True,
        release_id=current["release_id"],
        collection_name=current["collection_name"],
    )


def promote_release(
    *,
    release_root: Path,
    release_id: str,
    candidate_collection: str,
    expected_current_collection: str,
    alias_backend: AliasBackend,
    verification_attestation: Path,
    restore_attestation: Path,
    all_release_gates: bool,
) -> PromotionResult:
    """CAS the dense alias, then atomically replace the current release manifest."""
    if (
        not isinstance(release_root, Path)
        or type(release_id) is not str
        or _RELEASE_RE.fullmatch(release_id) is None
        or type(candidate_collection) is not str
        or _COLLECTION_RE.fullmatch(candidate_collection) is None
        or type(expected_current_collection) is not str
        or _COLLECTION_RE.fullmatch(expected_current_collection) is None
        or not isinstance(verification_attestation, Path)
        or not isinstance(restore_attestation, Path)
        or type(all_release_gates) is not bool
    ):
        return _promotion_failure("promotion_input_invalid")
    if not all_release_gates:
        return _promotion_failure("release_gate_failed")
    verification = _load_attestation(verification_attestation)
    restore = _load_attestation(restore_attestation)
    if (
        verification is None
        or restore is None
        or verification.kind != "verification"
        or restore.kind != "restore"
        or verification.release_id != release_id
        or restore.release_id != release_id
        or verification.bundle_sha256 != restore.bundle_sha256
    ):
        return _promotion_failure("attestation_mismatch")
    current_path = release_root / "current.json"
    current = _load_current_manifest(current_path)
    if current is None or current["collection_name"] != expected_current_collection:
        return _promotion_failure("current_manifest_invalid")
    try:
        if alias_backend.current_target(_ALIAS) != expected_current_collection:
            return _promotion_failure("alias_compare_failed")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _promotion_failure("alias_compare_failed")
    pending = release_root / f"pending-{release_id}.json"
    payload = (
        json.dumps(
            {
                "collection_name": candidate_collection,
                "release_id": release_id,
                "schema_version": "sen-qa-current-release/v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    try:
        with pending.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return _promotion_failure("pending_manifest_failed")
    try:
        swapped = alias_backend.compare_and_swap(
            _ALIAS,
            expected_current_collection,
            candidate_collection,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        swapped = False
    if swapped is not True:
        try:
            pending.unlink()
        except OSError:
            pass
        return _promotion_failure("alias_update_failed")
    try:
        os.replace(pending, current_path)
    except OSError:
        try:
            rolled_back = alias_backend.compare_and_swap(
                _ALIAS,
                candidate_collection,
                expected_current_collection,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            rolled_back = False
        return _promotion_failure(
            "manifest_replace_failed"
            if rolled_back is True
            else "alias_rollback_failed"
        )
    return PromotionResult(promoted=True, error_code=None)
