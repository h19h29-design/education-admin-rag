"""Locked tokenization and page-bounded role chunk construction."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, Protocol, cast

from src.corpus.ids import validate_case_id
from src.corpus.models import Case, Chunk, SourceSpan
from src.corpus.relations import canonical_case_sha256

_LOCK_MAX_BYTES = 1_048_576
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_COMPOSITE_KEYS = {
    "schema_version",
    "language",
    "packages",
    "models",
    "embedding_models",
}
_MODEL_KEYS = {"repo_id", "revision", "files"}
_FILE_KEYS = {"path", "sha256", "size", "source_url"}
_OFFICIAL_REPO_ID = "BAAI/bge-m3"

BGE_M3_REQUIRED_PATHS = (
    "1_Pooling/config.json",
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "pytorch_model.bin",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)

TOKENIZER_REQUIRED_PATHS = (
    "config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


class ChunkingError(ValueError):
    """A value-free model-lock, tokenization, or chunking failure."""


@dataclass(frozen=True, slots=True)
class LockedEmbeddingFile:
    """One exact file in the dense-only SentenceTransformer runtime closure."""

    path: str
    sha256: str
    size: int
    source_url: str


@dataclass(frozen=True, slots=True)
class EmbeddingModelLock:
    """Immutable official BGE-M3 revision and its exact offline file closure."""

    repo_id: str
    revision: str
    files: tuple[LockedEmbeddingFile, ...]

    def canonical_bytes(self) -> bytes:
        """Return the byte-stable identity included in canonical content hashes."""
        payload = {
            "files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": item.size,
                    "source_url": item.source_url,
                }
                for item in self.files
            ],
            "repo_id": self.repo_id,
            "revision": self.revision,
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")

    @property
    def fingerprint_sha256(self) -> str:
        """Return a stable binding for storage and build manifests."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _safe_locked_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _raise(message: str) -> NoReturn:
    raise ChunkingError(message) from None


def validate_embedding_model_lock(payload: object) -> EmbeddingModelLock:
    """Validate only the embedding slice of the reviewed composite model lock."""
    if type(payload) is not dict or set(payload) != _COMPOSITE_KEYS:
        _raise("model lock top-level fields are invalid")
    payload = cast(dict[str, object], payload)
    if payload.get("schema_version") != 1 or payload.get("language") != "korean":
        _raise("model lock top-level policy is invalid")
    embedding_models = payload.get("embedding_models")
    if type(embedding_models) is not list or len(embedding_models) != 1:
        _raise("model lock requires exactly one embedding model")
    model = embedding_models[0]
    if type(model) is not dict or set(model) != _MODEL_KEYS:
        _raise("embedding model fields are invalid")
    model = cast(dict[str, object], model)
    if model.get("repo_id") != _OFFICIAL_REPO_ID:
        _raise("embedding model must use the official repository")
    revision = model.get("revision")
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        _raise("embedding model revision must be an immutable 40-hex commit")
    raw_files = model.get("files")
    if type(raw_files) is not list:
        _raise("embedding model required files are invalid")

    files: list[LockedEmbeddingFile] = []
    for raw_file in raw_files:
        if type(raw_file) is not dict or set(raw_file) != _FILE_KEYS:
            _raise("embedding model file fields are invalid")
        raw_file = cast(dict[str, object], raw_file)
        path = _safe_locked_path(raw_file.get("path"))
        if path is None:
            _raise("embedding model file path is unsafe")
        digest = raw_file.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            _raise("embedding model file SHA-256 is invalid")
        size = raw_file.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            _raise("embedding model file size is invalid")
        expected_url = (
            f"https://huggingface.co/{_OFFICIAL_REPO_ID}/resolve/{revision}/{path}"
        )
        source_url = raw_file.get("source_url")
        if source_url != expected_url:
            _raise("embedding model source URL is not immutable and official")
        files.append(
            LockedEmbeddingFile(
                path=path,
                sha256=digest,
                size=size,
                source_url=source_url,
            )
        )

    if tuple(item.path for item in files) != BGE_M3_REQUIRED_PATHS:
        _raise("embedding model required files do not match the dense runtime closure")
    return EmbeddingModelLock(
        repo_id=_OFFICIAL_REPO_ID,
        revision=revision,
        files=tuple(files),
    )


def _read_bounded_regular_file(path: Path) -> bytes | None:
    descriptor: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _LOCK_MAX_BYTES:
            return None
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def load_embedding_model_lock(path: Path) -> EmbeddingModelLock:
    """Load a bounded canonical JSON lock without retaining rejected values."""
    raw = _read_bounded_regular_file(path)
    if raw is None:
        _raise("cannot load model lock")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey):
        payload = None
    if payload is None:
        _raise("cannot load model lock")
    return validate_embedding_model_lock(payload)


def _open_cache_root(root: Path) -> int | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = None
    try:
        absolute = Path(os.path.abspath(os.fspath(root)))
        descriptor = os.open(absolute.anchor, flags)
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _cache_entries(
    directory_fd: int,
    *,
    prefix: str = "",
    remaining_entries: int = 32,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        return None
    if len(names) > remaining_entries:
        return None
    files: list[str] = []
    directories: list[str] = []
    budget = remaining_entries - len(names)
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return None
        relative = f"{prefix}/{name}" if prefix else name
        try:
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            return None
        if stat.S_ISREG(details.st_mode):
            files.append(relative)
            continue
        if not stat.S_ISDIR(details.st_mode):
            return None
        try:
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except OSError:
            return None
        try:
            child_entries = _cache_entries(
                child_fd,
                prefix=relative,
                remaining_entries=budget,
            )
        finally:
            os.close(child_fd)
        if child_entries is None:
            return None
        child_files, child_directories = child_entries
        files.extend(child_files)
        directories.append(relative)
        directories.extend(child_directories)
        budget -= len(child_files) + len(child_directories)
        if budget < 0:
            return None
    return tuple(sorted(files)), tuple(sorted(directories))


def _open_relative_file(root_fd: int, relative_path: str) -> int | None:
    parts = PurePosixPath(relative_path).parts
    if not parts:
        return None
    current_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        result = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        return result
    except OSError:
        return None
    finally:
        os.close(current_fd)


def _verify_cache_file(
    root_fd: int, relative_path: str, locked: LockedEmbeddingFile
) -> str | None:
    descriptor: int | None = None
    try:
        descriptor = _open_relative_file(root_fd, relative_path)
        if descriptor is None:
            return "regular file"
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return "regular file"
        if before.st_size != locked.size:
            return "size"
        digest = hashlib.sha256()
        remaining = locked.size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                return "size"
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return "size"
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            return "stability"
        if digest.hexdigest() != locked.sha256:
            return "SHA-256"
        return None
    except OSError:
        return "regular file"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _revalidate_embedding_lock(lock: object) -> EmbeddingModelLock | None:
    if type(lock) is not EmbeddingModelLock or type(lock.files) is not tuple:
        return None
    if any(type(item) is not LockedEmbeddingFile for item in lock.files):
        return None
    payload = {
        "schema_version": 1,
        "language": "korean",
        "packages": {},
        "models": [],
        "embedding_models": [
            {
                "repo_id": lock.repo_id,
                "revision": lock.revision,
                "files": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "size": item.size,
                        "source_url": item.source_url,
                    }
                    for item in lock.files
                ],
            }
        ],
    }
    try:
        return validate_embedding_model_lock(payload)
    except ChunkingError:
        return None


def _expected_directories(paths: tuple[str, ...]) -> tuple[str, ...]:
    directories = {
        parent.as_posix()
        for path in paths
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    return tuple(sorted(directories))


def verify_embedding_cache(
    lock: object,
    model_root: Path,
    *,
    scope: Literal["tokenizer", "full"],
    expected_lock_sha256: str,
) -> None:
    """Require an exact, local-only tokenizer subset or dense model closure."""
    if not isinstance(scope, str) or scope not in {"tokenizer", "full"}:
        _raise("embedding cache verification scope is invalid")
    approved_lock = _revalidate_embedding_lock(lock)
    if approved_lock is None:
        _raise("embedding model lock is invalid")
    if (
        not isinstance(expected_lock_sha256, str)
        or _SHA256_RE.fullmatch(expected_lock_sha256) is None
        or not hmac.compare_digest(
            approved_lock.fingerprint_sha256, expected_lock_sha256
        )
    ):
        _raise("embedding cache does not match the pinned lock fingerprint")
    expected_paths = (
        TOKENIZER_REQUIRED_PATHS if scope == "tokenizer" else BGE_M3_REQUIRED_PATHS
    )
    root_fd = _open_cache_root(model_root)
    if root_fd is None:
        _raise("embedding cache root is not a trusted directory")
    try:
        entries = _cache_entries(root_fd)
        if entries != (
            tuple(sorted(expected_paths)),
            _expected_directories(expected_paths),
        ):
            _raise("embedding cache file set does not match the lock")
        locked_by_path = {item.path: item for item in approved_lock.files}
        for path in expected_paths:
            reason = _verify_cache_file(root_fd, path, locked_by_path[path])
            if reason is not None:
                _raise(f"embedding cache {reason} verification failed")
    finally:
        os.close(root_fd)


def _read_locked_cache_file(
    root_fd: int,
    locked: LockedEmbeddingFile,
    *,
    max_bytes: int,
) -> bytes | None:
    if locked.size > max_bytes:
        return None
    descriptor = _open_relative_file(root_fd, locked.path)
    if descriptor is None:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != locked.size:
            return None
        chunks: list[bytes] = []
        remaining = locked.size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                return None
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None
        after = os.fstat(descriptor)
        if (
            _stat_identity(before) != _stat_identity(after)
            or digest.hexdigest() != locked.sha256
        ):
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


ChunkRole = Literal["question", "answer", "basis", "facts", "table"]


@dataclass(frozen=True, slots=True)
class TokenizerContract:
    """Storage identity derived from a separately pinned embedding model lock."""

    model_name: str
    revision: str
    model_lock_sha256: str
    runtime_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class RoleSource:
    """One normalized role fragment bound to one exact raw source span."""

    role: ChunkRole
    text: str
    raw_text: str
    source_span_index: int
    table_header: str | None = None
    table_header_raw_text: str | None = None
    table_header_source_span_index: int | None = None
    table_evidence_sha256: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRoleSources:
    """Role/source bindings verified against an independently supplied digest."""

    sources: tuple[RoleSource, ...]
    fingerprint_sha256: str
    case_content_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class VerifiedChunkSet:
    """A sealed chunk collection bound to source, tokenizer, and case authorities."""

    chunks: tuple[Chunk, ...]
    case_content_sha256: str
    role_authority_sha256: str
    table_authorities: tuple[tuple[int, str], ...]
    tokenizer_contract: TokenizerContract
    binding_sha256: str

    def __iter__(self) -> Iterator[Chunk]:
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, index: int) -> Chunk:
        return self.chunks[index]


class Tokenizer(Protocol):
    """Small offline adapter around the exact locked embedding tokenizer."""

    model_name: str
    revision: str
    model_lock_sha256: str
    runtime_fingerprint_sha256: str

    def tokenize(self, text: str) -> tuple[str, ...]: ...

    def detokenize(self, tokens: tuple[str, ...]) -> str: ...

    def token_offsets(self, text: str) -> tuple[tuple[int, int], ...]: ...


class LockedTokenizer:
    """In-memory tokenizer created only from exact locked tokenizer JSON bytes."""

    def __init__(
        self,
        backend: Any,
        *,
        lock: EmbeddingModelLock,
        runtime_fingerprint_sha256: str,
    ) -> None:
        self._backend = backend
        self.model_name = lock.repo_id
        self.revision = lock.revision
        self.model_lock_sha256 = lock.fingerprint_sha256
        self.runtime_fingerprint_sha256 = runtime_fingerprint_sha256

    def tokenize(self, text: str) -> tuple[str, ...]:
        encoding = self._backend.encode(text, add_special_tokens=False)
        return tuple(encoding.tokens)

    def detokenize(self, tokens: tuple[str, ...]) -> str:
        identifiers = [self._backend.token_to_id(token) for token in tokens]
        if any(identifier is None for identifier in identifiers):
            _raise("locked tokenizer returned an unknown token")
        return cast(
            str,
            self._backend.decode(
                cast(list[int], identifiers),
                skip_special_tokens=False,
            ),
        )

    def token_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        encoding = self._backend.encode(text, add_special_tokens=False)
        return tuple(tuple(offset) for offset in encoding.offsets)


def load_locked_tokenizer(
    lock: object,
    model_root: Path,
    *,
    expected_lock_sha256: str,
    runtime_fingerprint_sha256: str,
) -> LockedTokenizer:
    """Load exact tokenizer bytes without reopening a verified pathname."""
    approved = _revalidate_embedding_lock(lock)
    if (
        approved is None
        or not isinstance(expected_lock_sha256, str)
        or _SHA256_RE.fullmatch(expected_lock_sha256) is None
        or not hmac.compare_digest(approved.fingerprint_sha256, expected_lock_sha256)
        or not isinstance(runtime_fingerprint_sha256, str)
        or _SHA256_RE.fullmatch(runtime_fingerprint_sha256) is None
    ):
        _raise("locked tokenizer authority is invalid")
    verify_embedding_cache(
        approved,
        model_root,
        scope="tokenizer",
        expected_lock_sha256=expected_lock_sha256,
    )
    tokenizer_file = next(
        item for item in approved.files if item.path == "tokenizer.json"
    )
    root_fd = _open_cache_root(model_root)
    if root_fd is None:
        _raise("locked tokenizer cache is invalid")
    try:
        raw = _read_locked_cache_file(
            root_fd,
            tokenizer_file,
            max_bytes=32 * 1024 * 1024,
        )
    finally:
        os.close(root_fd)
    backend: object | None = None
    if raw is not None:
        try:
            module = importlib.import_module("tokenizers")
            tokenizer_type = cast(Any, module).Tokenizer
            backend = tokenizer_type.from_str(raw.decode("utf-8"))
        except (ImportError, UnicodeError, RuntimeError, TypeError, ValueError):
            backend = None
    if backend is None:
        _raise("locked tokenizer cache is invalid")
    return LockedTokenizer(
        backend,
        lock=approved,
        runtime_fingerprint_sha256=runtime_fingerprint_sha256,
    )


_ROLE_ORDER: dict[ChunkRole, int] = {
    "question": 0,
    "answer": 1,
    "basis": 2,
    "facts": 3,
    "table": 4,
}
_ROLE_LIMITS: dict[ChunkRole, tuple[int, int]] = {
    "question": (80, 250),
    "answer": (250, 450),
    "basis": (250, 450),
    "facts": (250, 450),
    "table": (250, 450),
}
_MAX_ROLE_SOURCES = 2_048
_MAX_SOURCE_TEXT_CHARS = 1_048_576
_MAX_CASE_SOURCE_CHARS = 8_388_608
_MAX_TOKENS_PER_SOURCE = 200_000
_MAX_CHUNKS_PER_CASE = 50_000
_OVERLAP_RATIO = 0.12


def tokenizer_runtime_fingerprint_sha256(
    runtime_lock_bytes: bytes,
    *,
    indexer_image_digest: str,
) -> str:
    """Bind tokenizer implementation bytes to the immutable runtime image."""
    if (
        type(runtime_lock_bytes) is not bytes
        or not runtime_lock_bytes
        or len(runtime_lock_bytes) > 16 * 1024 * 1024
        or not isinstance(indexer_image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", indexer_image_digest) is None
    ):
        _raise("tokenizer runtime authority is invalid")
    return hashlib.sha256(
        b"sen-qa-tokenizer-runtime-v1\0"
        + hashlib.sha256(runtime_lock_bytes).digest()
        + b"\0"
        + indexer_image_digest.encode("ascii")
    ).hexdigest()


def tokenizer_contract(
    lock: object,
    *,
    expected_lock_sha256: str,
    runtime_fingerprint_sha256: str,
) -> TokenizerContract:
    """Derive token-count identity only from an externally pinned valid lock."""
    approved = _revalidate_embedding_lock(lock)
    if (
        approved is None
        or not isinstance(expected_lock_sha256, str)
        or _SHA256_RE.fullmatch(expected_lock_sha256) is None
        or not hmac.compare_digest(approved.fingerprint_sha256, expected_lock_sha256)
        or not isinstance(runtime_fingerprint_sha256, str)
        or _SHA256_RE.fullmatch(runtime_fingerprint_sha256) is None
    ):
        _raise("tokenizer contract does not match the pinned model lock")
    return TokenizerContract(
        model_name=approved.repo_id,
        revision=approved.revision,
        model_lock_sha256=expected_lock_sha256,
        runtime_fingerprint_sha256=runtime_fingerprint_sha256,
    )


def _revalidate_contract(value: object) -> TokenizerContract | None:
    if type(value) is not TokenizerContract:
        return None
    if (
        type(value.model_name) is not str
        or value.model_name != _OFFICIAL_REPO_ID
        or type(value.revision) is not str
        or _REVISION_RE.fullmatch(value.revision) is None
        or type(value.model_lock_sha256) is not str
        or _SHA256_RE.fullmatch(value.model_lock_sha256) is None
        or type(value.runtime_fingerprint_sha256) is not str
        or _SHA256_RE.fullmatch(value.runtime_fingerprint_sha256) is None
    ):
        return None
    return TokenizerContract(
        model_name=value.model_name,
        revision=value.revision,
        model_lock_sha256=value.model_lock_sha256,
        runtime_fingerprint_sha256=value.runtime_fingerprint_sha256,
    )


def _revalidate_case(value: object) -> Case | None:
    if type(value) is not Case:
        return None
    fields = value.__dict__
    if set(fields) != set(Case.model_fields) or value.__pydantic_extra__ not in (
        None,
        {},
    ):
        return None
    raw_spans = fields.get("source_spans")
    if type(raw_spans) is not tuple:
        return None
    rebuilt_spans: list[SourceSpan] = []
    for span in raw_spans:
        if (
            type(span) is not SourceSpan
            or set(span.__dict__) != set(SourceSpan.model_fields)
            or span.__pydantic_extra__ not in (None, {})
        ):
            return None
        try:
            rebuilt_spans.append(SourceSpan.model_validate(dict(span.__dict__)))
        except (TypeError, ValueError):
            return None
    candidate_fields = dict(fields)
    candidate_fields["source_spans"] = tuple(rebuilt_spans)
    try:
        approved = Case.model_validate(candidate_fields)
    except (TypeError, ValueError):
        return None
    try:
        validate_case_id(approved.case_id)
    except ValueError:
        return None
    return approved


def _revalidate_role_source(value: object) -> RoleSource | None:
    if type(value) is not RoleSource:
        return None
    if (
        type(value.role) is not str
        or value.role not in _ROLE_ORDER
        or type(value.text) is not str
        or not value.text.strip()
        or len(value.text) > _MAX_SOURCE_TEXT_CHARS
        or type(value.raw_text) is not str
        or not value.raw_text
        or len(value.raw_text) > _MAX_SOURCE_TEXT_CHARS
        or isinstance(value.source_span_index, bool)
        or not isinstance(value.source_span_index, int)
        or value.source_span_index < 0
    ):
        return None
    if value.role == "table":
        if (
            type(value.table_header) is not str
            or not value.table_header.strip()
            or len(value.table_header) > _MAX_SOURCE_TEXT_CHARS
            or type(value.table_header_raw_text) is not str
            or not value.table_header_raw_text
            or len(value.table_header_raw_text) > _MAX_SOURCE_TEXT_CHARS
            or isinstance(value.table_header_source_span_index, bool)
            or not isinstance(value.table_header_source_span_index, int)
            or value.table_header_source_span_index < 0
            or type(value.table_evidence_sha256) is not str
            or _SHA256_RE.fullmatch(value.table_evidence_sha256) is None
        ):
            return None
    elif any(
        item is not None
        for item in (
            value.table_header,
            value.table_header_raw_text,
            value.table_header_source_span_index,
            value.table_evidence_sha256,
        )
    ):
        return None
    return RoleSource(
        role=value.role,
        text=value.text,
        raw_text=value.raw_text,
        source_span_index=value.source_span_index,
        table_header=value.table_header,
        table_header_raw_text=value.table_header_raw_text,
        table_header_source_span_index=value.table_header_source_span_index,
        table_evidence_sha256=value.table_evidence_sha256,
    )


def _text_projection(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _tokenize(tokenizer: Tokenizer, text: str) -> tuple[str, ...]:
    tokens: object | None = None
    try:
        tokens = tokenizer.tokenize(text)
    except Exception:  # noqa: BLE001 - untrusted tokenizer adapter boundary
        tokens = None
    if tokens is None:
        _raise("locked tokenizer failed")
    if (
        type(tokens) is not tuple
        or len(tokens) > _MAX_TOKENS_PER_SOURCE
        or any(type(token) is not str or not token for token in tokens)
    ):
        _raise("locked tokenizer returned invalid tokens")
    return tokens


def _token_offsets(tokenizer: Tokenizer, text: str) -> tuple[tuple[int, int], ...]:
    offsets: object | None = None
    try:
        offsets = tokenizer.token_offsets(text)
    except Exception:  # noqa: BLE001 - untrusted tokenizer adapter boundary
        offsets = None
    if type(offsets) is not tuple:
        _raise("locked tokenizer failed")
    previous_end = 0
    for offset in offsets:
        if (
            type(offset) is not tuple
            or len(offset) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in offset
            )
        ):
            _raise("locked tokenizer returned invalid offsets")
        start, end = offset
        if start < previous_end or start < 0 or end <= start or end > len(text):
            _raise("locked tokenizer returned invalid offsets")
        previous_end = end
    return cast(tuple[tuple[int, int], ...], offsets)


def _tokenizer_identity(tokenizer: Tokenizer) -> tuple[object, ...] | None:
    identity: tuple[object, ...] | None = None
    try:
        identity = (
            tokenizer.model_name,
            tokenizer.revision,
            tokenizer.model_lock_sha256,
            tokenizer.runtime_fingerprint_sha256,
        )
    except Exception:  # noqa: BLE001 - untrusted tokenizer adapter boundary
        identity = None
    return identity


def _embedding_prefix(case: Case, role: ChunkRole) -> str:
    hierarchy = " > ".join(
        value for value in (case.domain, case.part, case.subtopic) if value
    )
    return f"{case.case_id}\n{hierarchy}\n{case.title_normalized}\n{role}\n"


def _chunk_text_windows(
    *,
    tokenizer: Tokenizer,
    prefix: str,
    text: str,
    maximum: int,
) -> tuple[str, ...]:
    prefix_count = len(_tokenize(tokenizer, prefix))
    capacity = maximum - prefix_count
    if capacity < 1:
        _raise("chunk metadata exceeds the role token limit")
    tokens = _tokenize(tokenizer, text)
    offsets = _token_offsets(tokenizer, text)
    if len(tokens) != len(offsets):
        _raise("locked tokenizer token and offset counts differ")
    if len(tokens) <= capacity:
        return (text,)
    overlap = max(1, round(capacity * _OVERLAP_RATIO))
    step = capacity - overlap
    if step < 1:
        _raise("chunk overlap policy is invalid")
    windows: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + capacity, len(tokens))
        windows.append(text[offsets[start][0] : offsets[end - 1][1]])
        if start + capacity >= len(tokens):
            break
        start += step
    return tuple(windows)


def _source_chunk_text(source: RoleSource) -> str:
    if source.role == "table":
        return f"{source.table_header}\n{source.text}"
    return source.text


def _group_chunk_text(sources: tuple[RoleSource, ...]) -> str:
    first = sources[0]
    if first.role == "table":
        return f"{first.table_header}\n" + "\n".join(source.text for source in sources)
    return "\n".join(source.text for source in sources)


def _source_span_indexes(source: RoleSource) -> tuple[int, ...]:
    if source.role == "table":
        return (
            cast(int, source.table_header_source_span_index),
            source.source_span_index,
        )
    return (source.source_span_index,)


def _group_span_indexes(sources: tuple[RoleSource, ...]) -> tuple[int, ...]:
    indexes: list[int] = []
    for source in sources:
        for index in _source_span_indexes(source):
            if index not in indexes:
                indexes.append(index)
    return tuple(indexes)


def _sources_can_group(
    case: Case,
    current: tuple[RoleSource, ...],
    candidate: RoleSource,
) -> bool:
    first = current[0]
    if first.role != candidate.role:
        return False
    first_page = case.source_spans[first.source_span_index].pdf_page_index
    candidate_page = case.source_spans[candidate.source_span_index].pdf_page_index
    if first_page != candidate_page:
        return False
    if first.role != "table":
        return True
    return (
        first.table_header == candidate.table_header
        and first.table_header_raw_text == candidate.table_header_raw_text
        and first.table_header_source_span_index
        == candidate.table_header_source_span_index
        and first.table_evidence_sha256 == candidate.table_evidence_sha256
    )


def _grouped_chunk_units(
    case: Case,
    sources: tuple[RoleSource, ...],
    tokenizer: Tokenizer,
) -> tuple[tuple[ChunkRole, str, tuple[int, ...]], ...]:
    units: list[tuple[ChunkRole, str, tuple[int, ...]]] = []
    group: tuple[RoleSource, ...] = ()

    def flush() -> None:
        nonlocal group
        if not group:
            return
        first = group[0]
        text = _group_chunk_text(group)
        prefix = _embedding_prefix(case, first.role)
        _, maximum = _ROLE_LIMITS[first.role]
        if len(group) == 1:
            windows = _chunk_text_windows(
                tokenizer=tokenizer,
                prefix=prefix,
                text=text,
                maximum=maximum,
            )
        else:
            windows = (text,)
        indexes = _group_span_indexes(group)
        units.extend((first.role, window, indexes) for window in windows)
        group = ()

    for source in sources:
        _, maximum = _ROLE_LIMITS[source.role]
        prefix = _embedding_prefix(case, source.role)
        if group and _sources_can_group(case, group, source):
            candidate_group = (*group, source)
            candidate_text = _group_chunk_text(candidate_group)
            if len(_tokenize(tokenizer, prefix + candidate_text)) <= maximum:
                group = candidate_group
                continue
        flush()
        group = (source,)
        if len(_tokenize(tokenizer, prefix + _source_chunk_text(source))) > maximum:
            flush()
    flush()
    if len(units) > _MAX_CHUNKS_PER_CASE:
        _raise("chunk collection exceeds the per-case bound")
    return tuple(units)


def _aggregate_matches(case: Case, sources: tuple[RoleSource, ...]) -> bool:
    expected_by_role = {
        "question": case.question,
        "answer": case.answer,
        "basis": case.basis_text,
        "facts": case.facts,
    }
    for role, expected in expected_by_role.items():
        actual = "\n".join(source.text for source in sources if source.role == role)
        if expected is None:
            if actual:
                return False
        elif _text_projection(actual) != _text_projection(expected):
            return False
    return True


def _case_content_sha256(case: Case) -> str:
    return canonical_case_sha256(case)


def _approved_role_sources(case: Case, role_sources: object) -> tuple[RoleSource, ...]:
    if (
        type(role_sources) is not tuple
        or not 1 <= len(role_sources) <= _MAX_ROLE_SOURCES
    ):
        _raise("role source collection is invalid")
    sources = tuple(_revalidate_role_source(item) for item in role_sources)
    if any(item is None for item in sources):
        _raise("role source is invalid")
    approved_sources = cast(tuple[RoleSource, ...], sources)
    if len(set(approved_sources)) != len(approved_sources):
        _raise("role source collection contains duplicates")
    total_characters = sum(
        len(source.text)
        + len(source.raw_text)
        + len(source.table_header or "")
        + len(source.table_header_raw_text or "")
        for source in approved_sources
    )
    if total_characters > _MAX_CASE_SOURCE_CHARS:
        _raise("role source collection exceeds the per-case bound")
    for source in approved_sources:
        if source.source_span_index >= len(case.source_spans):
            _raise("role source is not bound to the canonical case")
        span = case.source_spans[source.source_span_index]
        if (
            hashlib.sha256(source.raw_text.encode("utf-8")).hexdigest()
            != span.text_sha256
        ):
            _raise("role source raw hash does not match its source span")
        if source.role == "table":
            header_index = cast(int, source.table_header_source_span_index)
            if header_index >= len(case.source_spans):
                _raise("role source table header span is invalid")
            header_span = case.source_spans[header_index]
            if (
                header_span.pdf_page_index != span.pdf_page_index
                or hashlib.sha256(
                    cast(str, source.table_header_raw_text).encode("utf-8")
                ).hexdigest()
                != header_span.text_sha256
                or _text_projection(source.table_header or "")
                not in _text_projection(cast(str, source.table_header_raw_text))
                or _text_projection(source.text)
                not in _text_projection(source.raw_text)
            ):
                _raise("role source table evidence does not match raw text")
    if not _aggregate_matches(case, approved_sources):
        _raise("role source aggregate does not match the canonical case")
    return approved_sources


def role_source_manifest_bytes(case: object, role_sources: object) -> bytes:
    """Render a value-free role/span authority manifest for external review pinning."""
    approved_case = _revalidate_case(case)
    if approved_case is None:
        _raise("canonical case is invalid")
    approved_sources = _approved_role_sources(approved_case, role_sources)
    payload = {
        "case_content_sha256": _case_content_sha256(approved_case),
        "case_id": approved_case.case_id,
        "sources": [
            {
                "raw_text_sha256": hashlib.sha256(
                    source.raw_text.encode("utf-8")
                ).hexdigest(),
                "role": source.role,
                "source_span_index": source.source_span_index,
                "table_evidence_sha256": source.table_evidence_sha256,
                "table_header_raw_text_sha256": (
                    hashlib.sha256(
                        source.table_header_raw_text.encode("utf-8")
                    ).hexdigest()
                    if source.table_header_raw_text is not None
                    else None
                ),
                "table_header_sha256": (
                    hashlib.sha256(source.table_header.encode("utf-8")).hexdigest()
                    if source.table_header is not None
                    else None
                ),
                "table_header_source_span_index": source.table_header_source_span_index,
                "text_sha256": hashlib.sha256(source.text.encode("utf-8")).hexdigest(),
            }
            for source in approved_sources
        ],
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def verify_role_sources(
    case: object,
    role_sources: object,
    *,
    expected_authority_sha256: str,
) -> VerifiedRoleSources:
    """Bind role/source data to an independently supplied review authority digest."""
    rendered = role_source_manifest_bytes(case, role_sources)
    fingerprint = hashlib.sha256(rendered).hexdigest()
    if (
        not isinstance(expected_authority_sha256, str)
        or _SHA256_RE.fullmatch(expected_authority_sha256) is None
        or not hmac.compare_digest(fingerprint, expected_authority_sha256)
    ):
        _raise("role sources do not match the external role authority")
    approved_case = cast(Case, _revalidate_case(case))
    approved_sources = _approved_role_sources(approved_case, role_sources)
    verified = object.__new__(VerifiedRoleSources)
    object.__setattr__(verified, "sources", approved_sources)
    object.__setattr__(verified, "fingerprint_sha256", fingerprint)
    object.__setattr__(
        verified, "case_content_sha256", _case_content_sha256(approved_case)
    )
    return verified


def _revalidate_chunk(value: object) -> Chunk | None:
    if type(value) is not Chunk:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict or set(fields) != set(Chunk.model_fields):
        return None
    try:
        return Chunk.model_validate(dict(fields))
    except (TypeError, ValueError):
        return None


def _chunk_set_binding_sha256(
    *,
    chunks: tuple[Chunk, ...],
    case_content_sha256: str,
    role_authority_sha256: str,
    table_authorities: tuple[tuple[int, str], ...],
    contract: TokenizerContract,
) -> str:
    payload = {
        "case_content_sha256": case_content_sha256,
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
        "role_authority_sha256": role_authority_sha256,
        "table_authorities": [list(item) for item in table_authorities],
        "tokenizer_contract": {
            "model_lock_sha256": contract.model_lock_sha256,
            "model_name": contract.model_name,
            "revision": contract.revision,
            "runtime_fingerprint_sha256": contract.runtime_fingerprint_sha256,
        },
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"sen-qa-verified-chunk-set-v1\0" + rendered).hexdigest()


def _new_verified_chunk_set(
    *,
    chunks: tuple[Chunk, ...],
    case_content_sha256: str,
    role_authority_sha256: str,
    table_authorities: tuple[tuple[int, str], ...],
    contract: TokenizerContract,
) -> VerifiedChunkSet:
    verified = object.__new__(VerifiedChunkSet)
    object.__setattr__(verified, "chunks", chunks)
    object.__setattr__(verified, "case_content_sha256", case_content_sha256)
    object.__setattr__(verified, "role_authority_sha256", role_authority_sha256)
    object.__setattr__(verified, "table_authorities", table_authorities)
    object.__setattr__(verified, "tokenizer_contract", contract)
    object.__setattr__(
        verified,
        "binding_sha256",
        _chunk_set_binding_sha256(
            chunks=chunks,
            case_content_sha256=case_content_sha256,
            role_authority_sha256=role_authority_sha256,
            table_authorities=table_authorities,
            contract=contract,
        ),
    )
    return verified


def revalidate_verified_chunk_set(
    value: object,
    case: object,
    *,
    contract: object,
    expected_role_authority_sha256: str,
    expected_chunk_set_sha256: str,
    expected_table_evidence_sha256s: dict[int, str] | None = None,
) -> VerifiedChunkSet:
    """Recheck a sealed chunk collection before canonical persistence."""
    if type(value) is not VerifiedChunkSet:
        _raise("verified chunk set is required")
    approved_case = _revalidate_case(case)
    approved_contract = _revalidate_contract(contract)
    if approved_case is None or approved_contract is None:
        _raise("verified chunk set authority is invalid")
    if type(value.chunks) is not tuple or not value.chunks:
        _raise("verified chunk set is invalid")
    checked = tuple(_revalidate_chunk(chunk) for chunk in value.chunks)
    if any(chunk is None for chunk in checked):
        _raise("verified chunk set is invalid")
    chunks = cast(tuple[Chunk, ...], checked)
    if expected_table_evidence_sha256s is None:
        table_authority: dict[int, str] = {}
    elif type(expected_table_evidence_sha256s) is dict:
        table_authority = expected_table_evidence_sha256s
    else:
        _raise("verified chunk set authority is invalid")
    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        for index, digest in table_authority.items()
    ):
        _raise("verified chunk set authority is invalid")
    canonical_table_authorities = tuple(sorted(table_authority.items()))
    case_hash = _case_content_sha256(approved_case)
    if (
        not isinstance(expected_role_authority_sha256, str)
        or _SHA256_RE.fullmatch(expected_role_authority_sha256) is None
        or not isinstance(value.case_content_sha256, str)
        or not hmac.compare_digest(value.case_content_sha256, case_hash)
        or not isinstance(value.role_authority_sha256, str)
        or not hmac.compare_digest(
            value.role_authority_sha256, expected_role_authority_sha256
        )
        or value.table_authorities != canonical_table_authorities
        or _revalidate_contract(value.tokenizer_contract) != approved_contract
        or not isinstance(expected_chunk_set_sha256, str)
        or _SHA256_RE.fullmatch(expected_chunk_set_sha256) is None
    ):
        _raise("verified chunk set authority does not match")
    seen_ids: set[str] = set()
    sequences: dict[ChunkRole, int] = {role: 0 for role in _ROLE_ORDER}
    for chunk in chunks:
        minimum, maximum = _ROLE_LIMITS[chunk.role]
        expected_quality_flags = (
            ("below-target-token-range",) if chunk.token_count < minimum else ()
        )
        if (
            chunk.chunk_id in seen_ids
            or chunk.case_id != approved_case.case_id
            or chunk.pii_class != approved_case.pii_class
            or chunk.search_eligible != approved_case.search_eligible
            or chunk.answer_eligible != approved_case.answer_eligible
            or chunk.sequence != sequences[chunk.role] + 1
            or chunk.chunk_id
            != f"{approved_case.case_id}-{chunk.role}-{chunk.sequence:02d}"
            or chunk.embedding_text
            != _embedding_prefix(approved_case, chunk.role) + chunk.text
            or chunk.token_count > maximum
            or chunk.quality_flags != expected_quality_flags
            or any(
                index >= len(approved_case.source_spans)
                for index in chunk.source_span_indexes
            )
            or len(
                {
                    approved_case.source_spans[index].pdf_page_index
                    for index in chunk.source_span_indexes
                }
            )
            != 1
        ):
            _raise("verified chunk set does not match the canonical case")
        seen_ids.add(chunk.chunk_id)
        sequences[chunk.role] = chunk.sequence
    required_roles = (
        {"facts", "answer"}
        if approved_case.case_type == "audit"
        else {"question", "answer"}
    )
    if not required_roles.issubset({chunk.role for chunk in chunks}):
        _raise("verified chunk set is missing required roles")
    binding = _chunk_set_binding_sha256(
        chunks=chunks,
        case_content_sha256=case_hash,
        role_authority_sha256=expected_role_authority_sha256,
        table_authorities=canonical_table_authorities,
        contract=approved_contract,
    )
    if (
        not isinstance(value.binding_sha256, str)
        or not hmac.compare_digest(value.binding_sha256, binding)
        or not hmac.compare_digest(binding, expected_chunk_set_sha256)
    ):
        _raise("verified chunk set binding is invalid")
    return _new_verified_chunk_set(
        chunks=chunks,
        case_content_sha256=case_hash,
        role_authority_sha256=expected_role_authority_sha256,
        table_authorities=canonical_table_authorities,
        contract=approved_contract,
    )


def build_chunks(
    case: object,
    role_sources: object,
    *,
    tokenizer: Tokenizer,
    contract: object,
    expected_role_authority_sha256: str,
    expected_table_evidence_sha256s: dict[int, str] | None = None,
) -> VerifiedChunkSet:
    """Build deterministic child chunks from exact, role-bound source fragments."""
    approved_case = _revalidate_case(case)
    approved_contract = _revalidate_contract(contract)
    if approved_case is None:
        _raise("canonical case is invalid")
    if approved_contract is None:
        _raise("tokenizer identity contract is invalid")
    if (
        not approved_case.search_eligible
        or approved_case.review_status not in {"search_approved", "approved"}
        or approved_case.pii_class in {"public_credit", "restricted"}
    ):
        _raise("canonical case is not eligible for chunking")
    if _tokenizer_identity(tokenizer) != (
        approved_contract.model_name,
        approved_contract.revision,
        approved_contract.model_lock_sha256,
        approved_contract.runtime_fingerprint_sha256,
    ):
        _raise("tokenizer identity does not match the contract")
    if type(role_sources) is not VerifiedRoleSources:
        _raise("verified role source authority is required")
    if (
        type(role_sources.sources) is not tuple
        or type(role_sources.fingerprint_sha256) is not str
        or type(role_sources.case_content_sha256) is not str
        or not isinstance(expected_role_authority_sha256, str)
        or _SHA256_RE.fullmatch(expected_role_authority_sha256) is None
    ):
        _raise("verified role source authority is invalid")
    rendered_authority = role_source_manifest_bytes(approved_case, role_sources.sources)
    recomputed_fingerprint = hashlib.sha256(rendered_authority).hexdigest()
    if (
        not hmac.compare_digest(recomputed_fingerprint, role_sources.fingerprint_sha256)
        or not hmac.compare_digest(
            recomputed_fingerprint, expected_role_authority_sha256
        )
        or not hmac.compare_digest(
            _case_content_sha256(approved_case), role_sources.case_content_sha256
        )
    ):
        _raise("verified role source authority does not match the canonical case")
    approved_sources = _approved_role_sources(approved_case, role_sources.sources)
    if expected_table_evidence_sha256s is None:
        table_authority: dict[int, str] = {}
    elif type(expected_table_evidence_sha256s) is dict:
        table_authority = expected_table_evidence_sha256s
    else:
        _raise("table role authority is invalid")
    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        for index, digest in table_authority.items()
    ):
        _raise("table role authority is invalid")
    table_source_indexes = {
        source.source_span_index
        for source in approved_sources
        if source.role == "table"
    }
    if set(table_authority) != table_source_indexes:
        _raise("table role authority is invalid")
    for source in approved_sources:
        if source.role == "table" and not hmac.compare_digest(
            source.table_evidence_sha256 or "",
            table_authority.get(source.source_span_index, ""),
        ):
            _raise("role source table evidence is not externally pinned")

    chunks: list[Chunk] = []
    sequence_by_role = {role: 0 for role in _ROLE_ORDER}
    ordered_sources = tuple(
        source
        for role in _ROLE_ORDER
        for source in approved_sources
        if source.role == role
    )
    for role, window, span_indexes in _grouped_chunk_units(
        approved_case,
        ordered_sources,
        tokenizer,
    ):
        minimum, maximum = _ROLE_LIMITS[role]
        sequence_by_role[role] += 1
        sequence = sequence_by_role[role]
        embedding_text = _embedding_prefix(approved_case, role) + window
        token_count = len(_tokenize(tokenizer, embedding_text))
        if token_count > maximum:
            _raise("chunk exceeds the role token limit")
        quality_flags = ("below-target-token-range",) if token_count < minimum else ()
        chunks.append(
            Chunk(
                chunk_id=f"{approved_case.case_id}-{role}-{sequence:02d}",
                case_id=approved_case.case_id,
                role=role,
                sequence=sequence,
                text=window,
                embedding_text=embedding_text,
                source_span_indexes=span_indexes,
                token_count=token_count,
                quality_flags=quality_flags,
                pii_class=approved_case.pii_class,
                search_eligible=approved_case.search_eligible,
                answer_eligible=approved_case.answer_eligible,
            )
        )
    roles = {chunk.role for chunk in chunks}
    required_roles = (
        {"facts", "answer"}
        if approved_case.case_type == "audit"
        else {"question", "answer"}
    )
    if not required_roles.issubset(roles):
        _raise("role sources are missing required canonical roles")
    canonical_chunks = tuple(chunks)
    return _new_verified_chunk_set(
        chunks=canonical_chunks,
        case_content_sha256=_case_content_sha256(approved_case),
        role_authority_sha256=expected_role_authority_sha256,
        table_authorities=tuple(sorted(table_authority.items())),
        contract=approved_contract,
    )
