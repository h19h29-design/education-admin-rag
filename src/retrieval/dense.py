"""Offline BGE-M3 encoding and fail-closed versioned Qdrant indexing."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, NoReturn, Protocol, Self, cast

from pydantic import ValidationError

from src.corpus.chunking import (
    ChunkingError,
    EmbeddingModelLock,
    load_embedding_model_lock,
    verify_embedding_cache,
)
from src.corpus.models import Case, Chunk, Document, SourceSpan
from src.corpus.storage import StorageError, connect_canonical_storage
from src.retrieval.query import AccessLevel, CaseType, QueryError, QueryFilters

_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_APPROVED_STATUSES = frozenset(("search_approved", "approved"))
_MAX_BATCH_SIZE = 256
_MAX_TEXT_CHARACTERS = 16_384
_MAX_POINTS_PER_CALL = 10_000
_MAX_SOURCE_REFERENCES = 4_096
_MAX_DENSE_RECORDS = 100_000
_MAX_DENSE_SERIALIZED_BYTES = 512 * 1024 * 1024
_MAX_DENSE_SNAPSHOT_BYTES = 8 * 1024 * 1024 * 1024
_POINT_NAMESPACE = uuid.UUID("4d8b3e8e-1f41-51c2-88ea-54ddf572304d")
_APPROVED_QDRANT_URLS = frozenset({"http://qdrant:6333", "http://127.0.0.1:6333"})


class DenseError(ValueError):
    """A fixed, value-free dense retrieval failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise DenseError(code) from None


class _EncoderBackend(Protocol):
    def encode(self, texts: list[str], **kwargs: object) -> object: ...


class _CountResult(Protocol):
    count: int


class _SnapshotDescription(Protocol):
    name: str
    checksum: str
    size: int


class _SnapshotStore(Protocol):
    def create_snapshot(
        self, collection_name: str, *, wait: bool = True, **kwargs: object
    ) -> _SnapshotDescription: ...


class _DenseStore(_SnapshotStore, Protocol):
    def close(self, **kwargs: object) -> None: ...

    def collection_exists(self, collection_name: str, **kwargs: object) -> bool: ...

    def get_collection(self, collection_name: str, **kwargs: object) -> object: ...

    def create_collection(
        self, collection_name: str, vectors_config: object, **kwargs: object
    ) -> bool: ...

    def upsert(
        self, collection_name: str, points: list[object], **kwargs: object
    ) -> object: ...

    def count(self, collection_name: str, **kwargs: object) -> _CountResult: ...

    def retrieve(
        self, collection_name: str, ids: list[object], **kwargs: object
    ) -> list[object]: ...

    def query_points(
        self,
        collection_name: str,
        *,
        query_filter: object,
        **kwargs: object,
    ) -> object: ...


def create_qdrant_client(url: str) -> _DenseStore:
    """Create a client only for the reviewed local/internal Qdrant endpoints."""
    if type(url) is not str or url not in _APPROVED_QDRANT_URLS:
        _raise("qdrant_endpoint_invalid")
    client: object = None
    try:
        module = importlib.import_module("qdrant_client")
        client_type = cast(Any, module).QdrantClient
        client = client_type(
            url=url,
            prefer_grpc=False,
            timeout=60,
            cloud_inference=False,
            check_compatibility=True,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        client = None
    if client is None:
        _raise("qdrant_endpoint_invalid")
    return cast(_DenseStore, client)


def _load_sentence_transformer(model_root: Path) -> _EncoderBackend:
    """Load only an already-verified local cache; never resolve a Hub revision."""
    module = importlib.import_module("sentence_transformers")
    sentence_transformer = cast(Any, module).SentenceTransformer
    backend = sentence_transformer(
        str(model_root),
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
    )
    return cast(_EncoderBackend, backend)


class DenseEncoder:
    """A batch encoder created only after exact full-cache verification."""

    def __init__(
        self,
        *,
        backend: _EncoderBackend,
        lock: EmbeddingModelLock,
        batch_size: int,
    ) -> None:
        self._backend = backend
        self._lock = lock
        self._batch_size = batch_size

    @classmethod
    def from_lock(
        cls,
        lock_path: Path,
        *,
        model_root: Path,
        expected_lock_sha256: str,
        batch_size: int = 16,
    ) -> Self:
        """Verify a separately pinned immutable lock and its complete local cache."""
        lock: EmbeddingModelLock | None = None
        try:
            lock = load_embedding_model_lock(lock_path)
        except ChunkingError:
            pass
        if lock is None:
            _raise("immutable_revision_required")
        if (
            type(expected_lock_sha256) is not str
            or _SHA256_RE.fullmatch(expected_lock_sha256) is None
            or expected_lock_sha256 != lock.fingerprint_sha256
            or type(batch_size) is not int
            or batch_size < 1
            or batch_size > _MAX_BATCH_SIZE
        ):
            _raise("embedding_contract_invalid")
        failed = False
        backend: _EncoderBackend | None = None
        try:
            verify_embedding_cache(
                lock,
                model_root,
                scope="full",
                expected_lock_sha256=expected_lock_sha256,
            )
            backend = _load_sentence_transformer(model_root)
        except (
            ChunkingError,
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            failed = True
        if failed or backend is None:
            _raise("embedding_cache_invalid")
        return cls(backend=backend, lock=lock, batch_size=batch_size)

    @property
    def revision(self) -> str:
        return self._lock.revision

    @property
    def embedding_version(self) -> str:
        return f"bge-m3-{self._lock.revision[:8]}-{self._lock.fingerprint_sha256[:8]}"

    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if (
            type(texts) is not tuple
            or len(texts) > _MAX_POINTS_PER_CALL
            or any(
                type(text) is not str or not text or len(text) > _MAX_TEXT_CHARACTERS
                for text in texts
            )
        ):
            _raise("encoding_input_invalid")
        if not texts:
            return ()
        raw: object = None
        failed = False
        try:
            raw = self._backend.encode(
                list(texts),
                batch_size=self._batch_size,
                convert_to_numpy=False,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError):
            failed = True
        if failed or not isinstance(raw, Iterable):
            _raise("encoding_failed")
        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        try:
            raw_vectors = tuple(islice(iter(raw), len(texts) + 1))
            if len(raw_vectors) != len(texts):
                failed = True
            for raw_vector in raw_vectors:
                if not isinstance(raw_vector, Iterable):
                    failed = True
                    break
                raw_values = tuple(islice(iter(raw_vector), 4_097))
                if len(raw_values) > 4_096:
                    failed = True
                    break
                vector = tuple(float(value) for value in raw_values)
                if (
                    not vector
                    or any(not math.isfinite(value) for value in vector)
                    or (dimension is not None and len(vector) != dimension)
                ):
                    failed = True
                    break
                norm = math.sqrt(sum(value * value for value in vector))
                if not math.isfinite(norm) or norm <= 0:
                    failed = True
                    break
                vectors.append(tuple(value / norm for value in vector))
                dimension = len(vector)
        except (MemoryError, OverflowError, TypeError, ValueError):
            failed = True
        if failed or len(vectors) != len(texts):
            _raise("encoding_failed")
        return tuple(vectors)

    def build_points(
        self,
        records: tuple[tuple[object, object, object], ...],
        *,
        corpus_version: str,
    ) -> tuple[DensePoint, ...]:
        """Filter first, then encode exact canonical chunk embedding text."""
        if (
            type(records) is not tuple
            or len(records) > _MAX_POINTS_PER_CALL
            or type(corpus_version) is not str
            or _RELEASE_RE.fullmatch(corpus_version) is None
        ):
            _raise("dense_records_invalid")
        approved: list[tuple[Document, Case, Chunk]] = []
        for record in records:
            if type(record) is not tuple or len(record) != 3:
                _raise("dense_records_invalid")
            document = _revalidate_document(record[0])
            case = _revalidate_case(record[1])
            chunk = _revalidate_chunk(record[2])
            if (
                document is None
                or case is None
                or chunk is None
                or case.doc_id != document.doc_id
                or case.extraction_source != document.extraction_method
                or chunk.case_id != case.case_id
                or chunk.pii_class != case.pii_class
                or chunk.search_eligible != case.search_eligible
                or chunk.answer_eligible != case.answer_eligible
                or any(
                    index >= len(case.source_spans)
                    for index in chunk.source_span_indexes
                )
                or any(
                    case.source_spans[index].pdf_page_index > document.pdf_page_count
                    for index in chunk.source_span_indexes
                )
            ):
                _raise("dense_records_invalid")
            if (
                not case.search_eligible
                or not chunk.search_eligible
                or case.review_status not in _APPROVED_STATUSES
                or case.pii_class in {"public_credit", "restricted"}
            ):
                continue
            approved.append((document, case, chunk))
        if not approved:
            return ()
        vectors = self.encode(tuple(chunk.embedding_text for _, _, chunk in approved))
        return tuple(
            DensePoint.create(
                document=document,
                case=case,
                chunk=chunk,
                vector=vector,
                corpus_version=corpus_version,
                embedding_version=self.embedding_version,
            )
            for (document, case, chunk), vector in zip(approved, vectors, strict=True)
        )


def _fields(
    value: object, model_type: type[Document | Case | Chunk | SourceSpan]
) -> dict[str, object] | None:
    if type(value) is not model_type or type(value.__dict__) is not dict:
        return None
    raw = dict(value.__dict__)
    if set(raw) != set(model_type.model_fields):
        return None
    return raw


def _revalidate_document(value: object) -> Document | None:
    raw = _fields(value, Document)
    if raw is None:
        return None
    try:
        return Document.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_case(value: object) -> Case | None:
    raw = _fields(value, Case)
    if raw is None or type(raw.get("source_spans")) is not tuple:
        return None
    spans: list[dict[str, object]] = []
    for span in cast(tuple[object, ...], raw["source_spans"]):
        checked = _fields(span, SourceSpan)
        if checked is None:
            return None
        spans.append(checked)
    raw["source_spans"] = tuple(spans)
    try:
        return Case.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_chunk(value: object) -> Chunk | None:
    raw = _fields(value, Chunk)
    if raw is None:
        return None
    try:
        return Chunk.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class DensePoint:
    point_id: str
    vector: tuple[float, ...]
    chunk_id: str
    case_id: str
    doc_id: str
    edition_year: int
    domain: str
    part: str
    case_type: str
    access_level: str
    search_eligible: bool
    answer_eligible: bool
    review_status: str
    pii_class: str
    pdf_page_indexes: tuple[int, ...]
    source_span_indexes: tuple[int, ...]
    corpus_version: str
    embedding_version: str

    @classmethod
    def create(
        cls,
        *,
        document: object,
        case: object,
        chunk: object,
        vector: tuple[float, ...],
        corpus_version: str,
        embedding_version: str,
    ) -> Self:
        checked_document = _revalidate_document(document)
        checked_case = _revalidate_case(case)
        checked_chunk = _revalidate_chunk(chunk)
        if (
            checked_document is None
            or checked_case is None
            or checked_chunk is None
            or checked_case.doc_id != checked_document.doc_id
            or checked_chunk.case_id != checked_case.case_id
            or checked_chunk.pii_class != checked_case.pii_class
            or checked_chunk.search_eligible != checked_case.search_eligible
            or checked_chunk.answer_eligible != checked_case.answer_eligible
            or checked_case.extraction_source != checked_document.extraction_method
            or any(
                index >= len(checked_case.source_spans)
                for index in checked_chunk.source_span_indexes
            )
            or type(vector) is not tuple
            or not vector
            or any(
                type(value) is not float or not math.isfinite(value) for value in vector
            )
            or not math.isclose(
                math.sqrt(sum(value * value for value in vector)),
                1.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
            or type(corpus_version) is not str
            or _RELEASE_RE.fullmatch(corpus_version) is None
            or type(embedding_version) is not str
            or _VERSION_RE.fullmatch(embedding_version) is None
        ):
            _raise("dense_point_invalid")
        pages = tuple(
            sorted(
                {
                    checked_case.source_spans[index].pdf_page_index
                    for index in checked_chunk.source_span_indexes
                }
            )
        )
        return cls(
            point_id=str(uuid.uuid5(_POINT_NAMESPACE, checked_chunk.chunk_id)),
            vector=vector,
            chunk_id=checked_chunk.chunk_id,
            case_id=checked_case.case_id,
            doc_id=checked_document.doc_id,
            edition_year=checked_document.edition_year,
            domain=checked_case.domain,
            part=checked_case.part,
            case_type=checked_case.case_type,
            access_level=checked_document.access_level,
            search_eligible=checked_chunk.search_eligible,
            answer_eligible=checked_chunk.answer_eligible,
            review_status=checked_case.review_status,
            pii_class=checked_chunk.pii_class,
            pdf_page_indexes=pages,
            source_span_indexes=checked_chunk.source_span_indexes,
            corpus_version=corpus_version,
            embedding_version=embedding_version,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "case_id": self.case_id,
            "doc_id": self.doc_id,
            "edition_year": self.edition_year,
            "domain": self.domain,
            "part": self.part,
            "case_type": self.case_type,
            "access_level": self.access_level,
            "search_eligible": self.search_eligible,
            "answer_eligible": self.answer_eligible,
            "review_status": self.review_status,
            "pii_class": self.pii_class,
            "pdf_page_indexes": list(self.pdf_page_indexes),
            "source_span_indexes": list(self.source_span_indexes),
            "corpus_version": self.corpus_version,
            "embedding_version": self.embedding_version,
        }


@dataclass(frozen=True, slots=True)
class DenseSearchFilters:
    years: tuple[int, ...]
    domains: tuple[str, ...]
    case_types: tuple[CaseType, ...]
    access_level: AccessLevel

    @classmethod
    def create(
        cls,
        *,
        years: tuple[int, ...] = (),
        domains: tuple[str, ...] = (),
        case_types: tuple[CaseType, ...] = (),
        access_level: AccessLevel = "public",
    ) -> Self:
        checked = QueryFilters.create(
            years=years,
            domains=domains,
            case_types=case_types,
            access_level=access_level,
        )
        return cls(
            years=checked.years,
            domains=checked.domains,
            case_types=checked.case_types,
            access_level=checked.access_level,
        )


@dataclass(frozen=True, slots=True)
class DenseBuildResult:
    collection_name: str
    release_id: str
    embedding_version: str
    point_count: int
    sampled_vector_sha256: str


@dataclass(frozen=True, slots=True)
class DenseSnapshotResult:
    collection_name: str
    sha256: str
    size: int


def _snapshot_http_connection_provider(
    host: str, port: int, timeout: int
) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


def export_dense_snapshot(
    client: _SnapshotStore,
    *,
    qdrant_url: str,
    collection_name: str,
    output: Path,
) -> DenseSnapshotResult:
    """Create and download one immutable candidate snapshot without alias changes."""
    if (
        qdrant_url not in _APPROVED_QDRANT_URLS
        or type(collection_name) is not str
        or re.fullmatch(r"corpus-[0-9]{14}-[0-9a-f]{8}-bge-m3", collection_name) is None
        or not isinstance(output, Path)
        or output.name in {"", ".", ".."}
    ):
        _raise("dense_snapshot_invalid")
    name: object = None
    checksum: object = None
    expected_size: object = None
    try:
        description = client.create_snapshot(collection_name, wait=True)
        name = description.name
        checksum = description.checksum
        expected_size = description.size
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    if (
        type(name) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name) is None
        or type(checksum) is not str
        or _SHA256_RE.fullmatch(checksum) is None
        or type(expected_size) is not int
        or expected_size <= 0
        or expected_size > _MAX_DENSE_SNAPSHOT_BYTES
    ):
        _raise("dense_snapshot_invalid")

    host = "qdrant" if qdrant_url == "http://qdrant:6333" else "127.0.0.1"
    parent_fd = -1
    output_fd = -1
    connection: http.client.HTTPConnection | None = None
    failed = False
    written = 0
    digest = hashlib.sha256()
    try:
        parent_fd = os.open(
            output.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent.st_mode):
            failed = True
        if not failed:
            output_fd = os.open(
                output.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            connection = _snapshot_http_connection_provider(host, 6333, 60)
            connection.request(
                "GET",
                f"/collections/{collection_name}/snapshots/{name}",
            )
            response = connection.getresponse()
            if response.status != 200:
                failed = True
            while not failed:
                chunk = response.read(1_048_576)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size or written > _MAX_DENSE_SNAPSHOT_BYTES:
                    failed = True
                    break
                digest.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    count = os.write(output_fd, remaining)
                    if count <= 0:
                        failed = True
                        break
                    remaining = remaining[count:]
            os.fsync(output_fd)
    except (OSError, RuntimeError, TypeError, ValueError):
        failed = True
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, RuntimeError):
                failed = True
        if output_fd >= 0:
            try:
                os.close(output_fd)
            except OSError:
                failed = True
        if (
            failed
            or written != expected_size
            or not hmac.compare_digest(digest.hexdigest(), checksum)
        ) and parent_fd >= 0:
            try:
                os.unlink(output.name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                failed = True
    if (
        failed
        or written != expected_size
        or not hmac.compare_digest(digest.hexdigest(), checksum)
    ):
        _raise("dense_snapshot_invalid")
    return DenseSnapshotResult(
        collection_name=collection_name,
        sha256=checksum,
        size=written,
    )


@dataclass(frozen=True, slots=True)
class DenseSearchHit:
    point_id: str
    chunk_id: str
    case_id: str
    score: float


def _sample_vector_sha256(
    records: Iterable[tuple[str, Iterable[float]]],
) -> str | None:
    digest = hashlib.sha256(b"sen-qa-dense-vector-sample-v1\x00")
    try:
        for point_id, vector_values in records:
            point_bytes = point_id.encode("ascii")
            vector = tuple(float(value) for value in vector_values)
            digest.update(struct.pack(">I", len(point_bytes)))
            digest.update(point_bytes)
            digest.update(struct.pack(">I", len(vector)))
            for value in vector:
                digest.update(struct.pack(">f", value))
    except (OverflowError, TypeError, UnicodeError, ValueError, struct.error):
        return None
    return digest.hexdigest()


def _qdrant_models() -> Any:
    return importlib.import_module("qdrant_client.models")


def _revalidate_point(
    value: object, *, vector_size: int, release_id: str, embedding_version: str
) -> DensePoint | None:
    if type(value) is not DensePoint:
        return None
    point = value
    if (
        type(point.vector) is not tuple
        or len(point.vector) != vector_size
        or any(
            type(item) is not float or not math.isfinite(item) for item in point.vector
        )
        or not math.isclose(
            math.sqrt(sum(item * item for item in point.vector)),
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        or type(point.chunk_id) is not str
        or not point.chunk_id
        or point.point_id != str(uuid.uuid5(_POINT_NAMESPACE, point.chunk_id))
        or point.corpus_version != release_id
        or point.embedding_version != embedding_version
        or type(point.case_id) is not str
        or not point.case_id
        or type(point.doc_id) is not str
        or not point.doc_id
        or type(point.edition_year) is not int
        or point.edition_year < 1900
        or point.edition_year > 2100
        or type(point.domain) is not str
        or not point.domain
        or type(point.part) is not str
        or not point.part
        or type(point.case_type) is not str
        or point.case_type not in {"qa", "audit", "law_index", "credits"}
        or type(point.access_level) is not str
        or point.access_level not in {"public", "staff"}
        or type(point.search_eligible) is not bool
        or type(point.answer_eligible) is not bool
        or type(point.review_status) is not str
        or point.review_status
        not in {
            "machine_extracted",
            "needs_review",
            "search_approved",
            "approved",
            "rejected",
        }
        or type(point.pii_class) is not str
        or point.pii_class
        not in {
            "none",
            "anonymized_case",
            "quasi_identifier",
            "public_credit",
            "restricted",
        }
        or type(point.pdf_page_indexes) is not tuple
        or not point.pdf_page_indexes
        or any(type(page) is not int or page < 1 for page in point.pdf_page_indexes)
        or tuple(sorted(set(point.pdf_page_indexes))) != point.pdf_page_indexes
        or type(point.source_span_indexes) is not tuple
        or not point.source_span_indexes
        or any(
            type(index) is not int or index < 0 for index in point.source_span_indexes
        )
        or len(set(point.source_span_indexes)) != len(point.source_span_indexes)
        or (point.answer_eligible and not point.search_eligible)
    ):
        return None
    return point


class DenseIndex:
    """A versioned Qdrant collection with mandatory policy filters."""

    def __init__(
        self,
        client: _DenseStore,
        *,
        release_id: str,
        vector_size: int,
        embedding_version: str,
    ) -> None:
        if (
            type(release_id) is not str
            or _RELEASE_RE.fullmatch(release_id) is None
            or type(vector_size) is not int
            or vector_size < 1
            or vector_size > 4_096
            or type(embedding_version) is not str
            or _VERSION_RE.fullmatch(embedding_version) is None
        ):
            _raise("dense_index_invalid")
        self._client = client
        self._release_id = release_id
        self._vector_size = vector_size
        self._embedding_version = embedding_version
        self._point_ids: set[str] = set()
        self._point_vectors: dict[str, tuple[float, ...]] = {}
        self.collection_name = f"{release_id}-bge-m3"

    def _ensure_collection(self) -> None:
        failed = False
        schema_mismatch = False
        try:
            exists = self._client.collection_exists(self.collection_name)
            models = _qdrant_models()
            if not exists:
                created = self._client.create_collection(
                    self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self._vector_size, distance=models.Distance.COSINE
                    ),
                    on_disk_payload=True,
                )
                if created is not True:
                    failed = True
            if not failed:
                info = self._client.get_collection(self.collection_name)
                config = getattr(info, "config", None)
                params = getattr(config, "params", None)
                vectors = getattr(params, "vectors", None)
                vector_size = getattr(vectors, "size", None)
                distance = getattr(vectors, "distance", None)
                distance_value = getattr(distance, "value", distance)
                if (
                    type(vector_size) is not int
                    or vector_size != self._vector_size
                    or distance_value != "Cosine"
                ):
                    schema_mismatch = True
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            failed = True
        if schema_mismatch:
            _raise("collection_schema_mismatch")
        if failed:
            _raise("collection_initialization_failed")

    def upsert(self, points: tuple[DensePoint, ...]) -> int:
        if type(points) is not tuple or len(points) > _MAX_POINTS_PER_CALL:
            _raise("dense_points_invalid")
        self._ensure_collection()
        checked: list[DensePoint] = []
        seen: set[str] = set()
        for value in points:
            point = _revalidate_point(
                value,
                vector_size=self._vector_size,
                release_id=self._release_id,
                embedding_version=self._embedding_version,
            )
            if point is None or point.point_id in seen:
                _raise("dense_points_invalid")
            seen.add(point.point_id)
            if (
                not point.search_eligible
                or point.review_status not in _APPROVED_STATUSES
                or point.pii_class in {"public_credit", "restricted"}
            ):
                continue
            checked.append(point)
        if not checked:
            return 0
        failed = False
        try:
            models = _qdrant_models()
            qdrant_points = [
                models.PointStruct(
                    id=point.point_id,
                    vector=list(point.vector),
                    payload=point.payload,
                )
                for point in checked
            ]
            result = self._client.upsert(
                self.collection_name, qdrant_points, wait=True, ordering="strong"
            )
            status = getattr(result, "status", None)
            if getattr(status, "value", status) != "completed":
                failed = True
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            failed = True
        if failed:
            _raise("upsert_failed")
        self._point_ids.update(point.point_id for point in checked)
        self._point_vectors.update({point.point_id: point.vector for point in checked})
        return len(checked)

    def verify(self, *, expected_eligible_count: int) -> DenseBuildResult:
        if type(expected_eligible_count) is not int or expected_eligible_count < 0:
            _raise("verification_invalid")
        failed = False
        count = -1
        records: list[object] = []
        sample_ids = sorted(self._point_ids)[:16]
        try:
            result = self._client.count(self.collection_name, exact=True)
            raw_count = getattr(result, "count", None)
            if type(raw_count) is not int:
                failed = True
            else:
                count = raw_count
            if sample_ids:
                raw_records = self._client.retrieve(
                    self.collection_name,
                    cast(list[object], sample_ids),
                    with_payload=False,
                    with_vectors=True,
                )
                if type(raw_records) is not list:
                    failed = True
                else:
                    records = raw_records
        except (OSError, RuntimeError, TypeError, ValueError):
            failed = True
        if failed:
            _raise("verification_failed")
        if count != expected_eligible_count:
            _raise("point_count_mismatch")
        if len(records) != len(sample_ids):
            _raise("sample_verification_failed")
        vectors_by_id: dict[str, tuple[float, ...]] = {}
        for record in records:
            record_id = getattr(record, "id", None)
            raw_vector = getattr(record, "vector", None)
            if (
                type(record_id) is not str
                or record_id in vectors_by_id
                or not isinstance(raw_vector, list)
                or len(raw_vector) != self._vector_size
                or any(
                    type(value) not in {float, int} or not math.isfinite(float(value))
                    for value in raw_vector
                )
            ):
                _raise("sample_verification_failed")
            vectors_by_id[record_id] = tuple(float(value) for value in raw_vector)
        if set(vectors_by_id) != set(sample_ids):
            _raise("sample_verification_failed")
        sample_vectors = [
            (point_id, vectors_by_id[point_id]) for point_id in sample_ids
        ]
        expected_sample_vectors = [
            (point_id, self._point_vectors[point_id]) for point_id in sample_ids
        ]
        sampled_vector_sha256 = _sample_vector_sha256(sample_vectors)
        expected_sample_sha256 = _sample_vector_sha256(expected_sample_vectors)
        if (
            sampled_vector_sha256 is None
            or expected_sample_sha256 is None
            or not hmac.compare_digest(sampled_vector_sha256, expected_sample_sha256)
        ):
            _raise("sample_verification_failed")
        return DenseBuildResult(
            collection_name=self.collection_name,
            release_id=self._release_id,
            embedding_version=self._embedding_version,
            point_count=count,
            sampled_vector_sha256=sampled_vector_sha256,
        )

    def _validate_search_payload(
        self,
        raw_point: object,
        *,
        filters: DenseSearchFilters,
    ) -> DenseSearchHit | None:
        payload = getattr(raw_point, "payload", None)
        if type(payload) is not dict:
            return None
        checked = cast(dict[object, object], payload)
        expected_keys = {
            "chunk_id",
            "case_id",
            "doc_id",
            "edition_year",
            "domain",
            "part",
            "case_type",
            "access_level",
            "search_eligible",
            "answer_eligible",
            "review_status",
            "pii_class",
            "pdf_page_indexes",
            "source_span_indexes",
            "corpus_version",
            "embedding_version",
        }
        if set(checked) != expected_keys:
            return None
        chunk_id = checked["chunk_id"]
        case_id = checked["case_id"]
        doc_id = checked["doc_id"]
        edition_year = checked["edition_year"]
        domain = checked["domain"]
        part = checked["part"]
        case_type = checked["case_type"]
        access_level = checked["access_level"]
        search_eligible = checked["search_eligible"]
        answer_eligible = checked["answer_eligible"]
        review_status = checked["review_status"]
        pii_class = checked["pii_class"]
        pdf_pages = checked["pdf_page_indexes"]
        span_indexes = checked["source_span_indexes"]
        point_id = getattr(raw_point, "id", None)
        score = getattr(raw_point, "score", None)
        if (
            type(chunk_id) is not str
            or not chunk_id
            or type(case_id) is not str
            or not case_id
            or type(doc_id) is not str
            or not doc_id
            or type(edition_year) is not int
            or type(domain) is not str
            or not domain
            or type(part) is not str
            or not part
            or type(case_type) is not str
            or case_type not in {"qa", "audit", "law_index", "credits"}
            or type(access_level) is not str
            or access_level not in {"public", "staff"}
            or type(search_eligible) is not bool
            or search_eligible is not True
            or type(answer_eligible) is not bool
            or type(review_status) is not str
            or review_status not in _APPROVED_STATUSES
            or type(pii_class) is not str
            or pii_class not in {"none", "anonymized_case", "quasi_identifier"}
            or type(pdf_pages) is not list
            or not pdf_pages
            or len(pdf_pages) > _MAX_SOURCE_REFERENCES
            or any(type(page) is not int or page < 1 for page in pdf_pages)
            or sorted(set(pdf_pages)) != pdf_pages
            or type(span_indexes) is not list
            or not span_indexes
            or len(span_indexes) > _MAX_SOURCE_REFERENCES
            or any(type(index) is not int or index < 0 for index in span_indexes)
            or len(set(span_indexes)) != len(span_indexes)
            or checked["corpus_version"] != self._release_id
            or checked["embedding_version"] != self._embedding_version
            or type(point_id) is not str
            or point_id != str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))
            or type(score) not in {float, int}
            or not math.isfinite(float(cast(float | int, score)))
            or (filters.years and edition_year not in filters.years)
            or (filters.domains and domain not in filters.domains)
            or (filters.case_types and case_type not in filters.case_types)
            or (filters.access_level == "public" and access_level != "public")
        ):
            return None
        return DenseSearchHit(
            point_id=point_id,
            chunk_id=chunk_id,
            case_id=case_id,
            score=float(cast(float | int, score)),
        )

    def search(
        self,
        vector: tuple[float, ...],
        *,
        filters: DenseSearchFilters,
        limit: int = 25,
    ) -> tuple[DenseSearchHit, ...]:
        if (
            type(filters) is not DenseSearchFilters
            or type(vector) is not tuple
            or len(vector) != self._vector_size
            or any(
                type(value) is not float or not math.isfinite(value) for value in vector
            )
            or not math.isclose(
                math.sqrt(sum(value * value for value in vector)),
                1.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
            or type(limit) is not int
            or limit < 1
            or limit > 100
        ):
            _raise("dense_search_invalid")
        checked_filters: DenseSearchFilters | None = None
        try:
            checked_filters = DenseSearchFilters.create(
                years=filters.years,
                domains=filters.domains,
                case_types=filters.case_types,
                access_level=filters.access_level,
            )
        except QueryError:
            pass
        if checked_filters is None:
            _raise("dense_search_invalid")
        failed = False
        response: object = None
        try:
            models = _qdrant_models()
            must = [
                models.FieldCondition(
                    key="search_eligible", match=models.MatchValue(value=True)
                ),
                models.FieldCondition(
                    key="review_status",
                    match=models.MatchAny(any=["search_approved", "approved"]),
                ),
                models.FieldCondition(
                    key="access_level",
                    match=(
                        models.MatchValue(value="public")
                        if checked_filters.access_level == "public"
                        else models.MatchAny(any=["public", "staff"])
                    ),
                ),
                models.FieldCondition(
                    key="pii_class",
                    match=models.MatchAny(
                        any=["none", "anonymized_case", "quasi_identifier"]
                    ),
                ),
            ]
            for key, values in (
                ("edition_year", checked_filters.years),
                ("domain", checked_filters.domains),
                ("case_type", checked_filters.case_types),
            ):
                if values:
                    must.append(
                        models.FieldCondition(
                            key=key, match=models.MatchAny(any=list(values))
                        )
                    )
            response = self._client.query_points(
                self.collection_name,
                query=list(vector),
                query_filter=models.Filter(must=must),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            failed = True
        if failed or response is None:
            _raise("dense_search_failed")
        raw_points = getattr(response, "points", None)
        if not isinstance(raw_points, (list, tuple)) or len(raw_points) > limit:
            _raise("dense_search_failed")
        hits: list[DenseSearchHit] = []
        for raw_point in raw_points:
            hit = self._validate_search_payload(raw_point, filters=checked_filters)
            if hit is None:
                _raise("dense_search_failed")
            hits.append(hit)
        return tuple(hits)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite number")


def _load_dense_records(
    database: Path,
) -> tuple[tuple[Document, Case, Chunk], ...]:
    records: list[tuple[Document, Case, Chunk]] = []
    failed = False
    expected_count = -1
    total_bytes = 0
    try:
        with connect_canonical_storage(database) as connection:
            count = connection.execute("SELECT count(*) FROM chunks").fetchone()
            if (
                type(count) is not tuple
                or len(count) != 1
                or type(count[0]) is not int
                or count[0] < 1
                or count[0] > _MAX_DENSE_RECORDS
            ):
                failed = True
            else:
                expected_count = count[0]
            if not failed:
                cursor = connection.execute(
                    "SELECT d.payload_json,c.payload_json,ch.payload_json "
                    "FROM chunks AS ch JOIN cases AS c ON c.case_id=ch.case_id "
                    "JOIN documents AS d "
                    "ON d.doc_id=json_extract(c.payload_json,'$.doc_id') "
                    "ORDER BY ch.chunk_id"
                )
                for row in cursor:
                    if type(row) is not tuple or len(row) != 3:
                        failed = True
                        break
                    document_value, case_value, chunk_value = row
                    if any(
                        type(value) is not str
                        or len(value.encode("utf-8")) > _MAX_TEXT_CHARACTERS * 16
                        for value in (document_value, case_value, chunk_value)
                    ):
                        failed = True
                        break
                    raw_document = cast(str, document_value)
                    raw_case = cast(str, case_value)
                    raw_chunk = cast(str, chunk_value)
                    total_bytes += sum(
                        len(raw.encode("utf-8"))
                        for raw in (raw_document, raw_case, raw_chunk)
                    )
                    if (
                        total_bytes > _MAX_DENSE_SERIALIZED_BYTES
                        or len(records) >= expected_count
                    ):
                        failed = True
                        break
                    for raw in (raw_document, raw_case, raw_chunk):
                        decoded = json.loads(
                            raw,
                            object_pairs_hook=_unique_object,
                            parse_constant=_reject_json_constant,
                        )
                        if type(decoded) is not dict:
                            failed = True
                            break
                    if failed:
                        break
                    document = Document.model_validate_json(raw_document)
                    case = Case.model_validate_json(raw_case)
                    chunk = Chunk.model_validate_json(raw_chunk)
                    records.append((document, case, chunk))
                if len(records) != expected_count:
                    failed = True
    except (
        StorageError,
        sqlite3.Error,
        ValidationError,
        UnicodeError,
        TypeError,
        ValueError,
    ):
        failed = True
    if failed or not records:
        _raise("dense_records_invalid")
    return tuple(records)


def build_dense_candidate(
    database: Path,
    *,
    client: _DenseStore,
    encoder: DenseEncoder,
    release_id: str,
) -> DenseBuildResult:
    """Build and verify one versioned dense candidate without alias mutation."""
    if type(encoder) is not DenseEncoder:
        _raise("dense_records_invalid")
    records = _load_dense_records(database)
    index: DenseIndex | None = None
    point_count = 0
    for offset in range(0, len(records), _MAX_BATCH_SIZE):
        points = encoder.build_points(
            records[offset : offset + _MAX_BATCH_SIZE],
            corpus_version=release_id,
        )
        if points and index is None:
            index = DenseIndex(
                client,
                release_id=release_id,
                vector_size=len(points[0].vector),
                embedding_version=encoder.embedding_version,
            )
        if points:
            if index is None or index.upsert(points) != len(points):
                _raise("dense_points_invalid")
            point_count += len(points)
    if index is None or point_count < 1:
        _raise("dense_records_invalid")
    return index.verify(expected_eligible_count=point_count)
