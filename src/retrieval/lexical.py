"""Fail-closed SQLite FTS5 index builder and Korean lexical search."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, cast

from pydantic import ValidationError

from src.corpus.models import Case, Chunk, Document, LawRef
from src.corpus.storage import connect_canonical_storage
from src.retrieval.query import (
    QueryFilters,
    character_ngrams,
    exact_tokens,
    normalize_query,
)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "retrieval.toml"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_INDEX_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SEARCHABLE_CHARACTERS = 16_384
_APPROVED_STATUSES = frozenset(("search_approved", "approved"))
_CASE_TYPES = frozenset(("qa", "audit", "law_index", "credits"))
_SCHEMA_VERSION = 1


class LexicalError(ValueError):
    """A value-free lexical indexing or search failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise LexicalError(code) from None


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    schema_version: int
    index_version: str
    max_results: int
    max_records: int
    weights: tuple[float, float, float, float, float, float]
    fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class LexicalBuildResult:
    release_id: str
    indexed_chunks: int
    skipped_chunks: int
    config_sha256: str


@dataclass(frozen=True, slots=True)
class LexicalHit:
    chunk_id: str
    case_id: str
    doc_id: str
    score: float
    matched_terms: tuple[str, ...]
    review_status: Literal["search_approved", "approved"]
    answer_eligible: bool


@dataclass(frozen=True, slots=True)
class LexicalPlan:
    uses_fts: bool
    full_table_scan: bool
    restricted_candidates: int
    plan_steps: int


def _read_regular_file(path: Path, *, max_bytes: int, code: str) -> bytes:
    descriptor: int | None = None
    failed = False
    data = b""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            failed = True
        else:
            remaining = max_bytes + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(data) > max_bytes or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                failed = True
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        _raise(code)
    return data


def load_retrieval_config(path: Path = _DEFAULT_CONFIG) -> RetrievalConfig:
    data = _read_regular_file(path, max_bytes=_MAX_CONFIG_BYTES, code="config_invalid")
    parsed: object = None
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        pass
    if type(parsed) is not dict:
        _raise("config_invalid")
    root = cast(dict[object, object], parsed)
    if (
        set(root)
        != {
            "schema_version",
            "index_version",
            "max_results",
            "max_records",
            "bm25",
        }
        or type(root.get("bm25")) is not dict
    ):
        _raise("config_invalid")
    bm25 = cast(dict[object, object], root["bm25"])
    columns = (
        "title",
        "question",
        "law_names",
        "exact_tokens",
        "char_ngrams",
        "body",
    )
    if set(bm25) != set(columns):
        _raise("config_invalid")
    schema_version = root.get("schema_version")
    index_version = root.get("index_version")
    max_results = root.get("max_results")
    max_records = root.get("max_records")
    weights = tuple(bm25.get(column) for column in columns)
    if (
        type(schema_version) is not int
        or schema_version != _SCHEMA_VERSION
        or type(index_version) is not str
        or not index_version
        or len(index_version) > 80
        or type(max_results) is not int
        or max_results < 1
        or max_results > 1_000
        or type(max_records) is not int
        or max_records < 1
        or max_records > 1_000_000
    ):
        _raise("config_invalid")
    checked_weights: list[float] = []
    for weight in weights:
        if type(weight) not in {float, int}:
            _raise("config_invalid")
        numeric_weight = cast(float | int, weight)
        if numeric_weight <= 0 or numeric_weight > 100:
            _raise("config_invalid")
        checked_weights.append(float(numeric_weight))
    return RetrievalConfig(
        schema_version=schema_version,
        index_version=index_version,
        max_results=max_results,
        max_records=max_records,
        weights=cast(
            tuple[float, float, float, float, float, float], tuple(checked_weights)
        ),
        fingerprint_sha256=hashlib.sha256(data).hexdigest(),
    )


def _bounded_rows(
    connection: sqlite3.Connection, sql: str, max_records: int
) -> list[sqlite3.Row]:
    cursor = connection.execute(sql)
    rows = cursor.fetchmany(max_records + 1)
    if len(rows) > max_records:
        _raise("canonical_source_invalid")
    return cast(list[sqlite3.Row], rows)


def _model_from_json(
    payload: object, model_type: type[Document | Case | Chunk | LawRef]
) -> Document | Case | Chunk | LawRef:
    if type(payload) is not str or len(payload) > 8 * 1024 * 1024:
        _raise("canonical_source_invalid")
    checked: Document | Case | Chunk | LawRef | None = None
    try:
        checked = model_type.model_validate_json(payload)
    except (ValidationError, TypeError, ValueError):
        pass
    if checked is None:
        _raise("canonical_source_invalid")
    return checked


@dataclass(frozen=True, slots=True)
class _IndexRecord:
    chunk_id: str
    case_id: str
    doc_id: str
    edition_year: int
    domain: str
    case_type: str
    access_level: str
    review_status: str
    answer_eligible: bool
    title: str
    question: str
    law_names: str
    exact_tokens_text: str
    exact_tokens_json: str
    char_ngrams_text: str
    body: str


def _canonical_records(
    path: Path, config: RetrievalConfig
) -> tuple[str, tuple[_IndexRecord, ...], int]:
    failed = False
    release_id = ""
    records: list[_IndexRecord] = []
    skipped = 0
    try:
        with connect_canonical_storage(path) as connection:
            connection.row_factory = sqlite3.Row
            meta = connection.execute(
                "SELECT release_id FROM build_meta WHERE singleton=1"
            ).fetchall()
            if len(meta) != 1 or type(meta[0]["release_id"]) is not str:
                _raise("canonical_source_invalid")
            release_id = meta[0]["release_id"]
            document_rows = _bounded_rows(
                connection,
                "SELECT doc_id,payload_json FROM documents ORDER BY doc_id",
                config.max_records,
            )
            case_rows = _bounded_rows(
                connection,
                "SELECT case_id,payload_json FROM cases ORDER BY case_id",
                config.max_records,
            )
            chunk_rows = _bounded_rows(
                connection,
                "SELECT chunk_id,case_id,payload_json FROM chunks ORDER BY chunk_id",
                config.max_records,
            )
            law_rows = _bounded_rows(
                connection,
                "SELECT law_ref_id,case_id,payload_json FROM law_refs ORDER BY law_ref_id",
                config.max_records,
            )
            documents: dict[str, Document] = {}
            for row in document_rows:
                document = cast(
                    Document, _model_from_json(row["payload_json"], Document)
                )
                if row["doc_id"] != document.doc_id or document.doc_id in documents:
                    _raise("canonical_source_invalid")
                documents[document.doc_id] = document
            cases: dict[str, Case] = {}
            for row in case_rows:
                case = cast(Case, _model_from_json(row["payload_json"], Case))
                case_document = documents.get(case.doc_id)
                if (
                    row["case_id"] != case.case_id
                    or case.case_id in cases
                    or case_document is None
                    or case.extraction_source != case_document.extraction_method
                    or any(
                        span.pdf_page_index > case_document.pdf_page_count
                        for span in case.source_spans
                    )
                ):
                    _raise("canonical_source_invalid")
                cases[case.case_id] = case
            law_names_by_case: dict[str, list[str]] = {}
            law_ids_by_case: dict[str, set[str]] = {}
            for row in law_rows:
                law_ref = cast(LawRef, _model_from_json(row["payload_json"], LawRef))
                law_case = cases.get(law_ref.case_id)
                if (
                    row["law_ref_id"] != law_ref.law_ref_id
                    or row["case_id"] != law_ref.case_id
                    or law_case is None
                    or law_ref.source_span not in law_case.source_spans
                ):
                    _raise("canonical_source_invalid")
                law_ids_by_case.setdefault(law_ref.case_id, set()).add(
                    law_ref.law_ref_id
                )
                if law_ref.review_status in _APPROVED_STATUSES:
                    law_names_by_case.setdefault(law_ref.case_id, []).append(
                        law_ref.display_name
                    )
            if any(
                set(case.law_ref_ids) != law_ids_by_case.get(case.case_id, set())
                for case in cases.values()
            ):
                _raise("canonical_source_invalid")
            for row in chunk_rows:
                chunk = cast(Chunk, _model_from_json(row["payload_json"], Chunk))
                selected_case = cases.get(chunk.case_id)
                if (
                    row["chunk_id"] != chunk.chunk_id
                    or row["case_id"] != chunk.case_id
                    or selected_case is None
                    or chunk.pii_class != selected_case.pii_class
                    or chunk.search_eligible != selected_case.search_eligible
                    or chunk.answer_eligible != selected_case.answer_eligible
                    or any(
                        index >= len(selected_case.source_spans)
                        for index in chunk.source_span_indexes
                    )
                ):
                    _raise("canonical_source_invalid")
                case = selected_case
                if (
                    not case.search_eligible
                    or not chunk.search_eligible
                    or case.review_status not in _APPROVED_STATUSES
                    or case.pii_class in {"public_credit", "restricted"}
                ):
                    skipped += 1
                    continue
                document = documents[case.doc_id]
                law_names = " ".join(
                    sorted(set(law_names_by_case.get(case.case_id, ())))
                )
                searchable_text = " ".join(
                    value
                    for value in (
                        case.case_id,
                        case.title_normalized,
                        case.question or "",
                        law_names,
                        chunk.text,
                    )
                    if value
                )
                if len(searchable_text) > _MAX_SEARCHABLE_CHARACTERS:
                    _raise("canonical_source_invalid")
                exact = exact_tokens(searchable_text)
                grams = character_ngrams(searchable_text)
                records.append(
                    _IndexRecord(
                        chunk_id=chunk.chunk_id,
                        case_id=case.case_id,
                        doc_id=case.doc_id,
                        edition_year=document.edition_year,
                        domain=case.domain,
                        case_type=case.case_type,
                        access_level=document.access_level,
                        review_status=case.review_status,
                        answer_eligible=chunk.answer_eligible,
                        title=case.title_normalized,
                        question=case.question or "",
                        law_names=law_names,
                        exact_tokens_text=" ".join(exact),
                        exact_tokens_json=json.dumps(
                            exact, ensure_ascii=True, separators=(",", ":")
                        ),
                        char_ngrams_text=" ".join(grams),
                        body=chunk.text,
                    )
                )
    except LexicalError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        failed = True
    if failed:
        _raise("canonical_source_invalid")
    return release_id, tuple(records), skipped


_INDEX_SCHEMA = """
CREATE TABLE index_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  schema_version INTEGER NOT NULL,
  index_version TEXT NOT NULL,
  release_id TEXT NOT NULL,
  config_sha256 TEXT NOT NULL
) STRICT;
CREATE TABLE lexical_records(
  row_id INTEGER PRIMARY KEY,
  chunk_id TEXT NOT NULL UNIQUE,
  case_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  edition_year INTEGER NOT NULL,
  domain TEXT NOT NULL,
  case_type TEXT NOT NULL,
  access_level TEXT NOT NULL CHECK(access_level IN ('public','staff')),
  review_status TEXT NOT NULL CHECK(review_status IN ('search_approved','approved')),
  search_eligible INTEGER NOT NULL CHECK(search_eligible=1),
  answer_eligible INTEGER NOT NULL CHECK(answer_eligible IN (0,1)),
  exact_tokens_json TEXT NOT NULL CHECK(json_valid(exact_tokens_json))
) STRICT;
CREATE INDEX lexical_filter_idx ON lexical_records(
  access_level,search_eligible,review_status,edition_year,domain,case_type
);
CREATE VIRTUAL TABLE lexical_fts USING fts5(
  title,question,law_names,exact_tokens,char_ngrams,body,
  content='',tokenize='unicode61 remove_diacritics 0'
);
"""


def build_lexical_index(
    canonical_database: Path,
    target: Path,
    *,
    config_path: Path = _DEFAULT_CONFIG,
) -> LexicalBuildResult:
    """Build a new FTS index atomically from one closed canonical database."""
    if target.exists() or target.is_symlink():
        _raise("index_target_exists")
    if not target.parent.is_dir() or target.parent.is_symlink():
        _raise("index_target_invalid")
    config = load_retrieval_config(config_path)
    release_id, records, skipped = _canonical_records(canonical_database, config)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    failed = False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_INDEX_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO index_meta VALUES(1,?,?,?,?)",
            (
                config.schema_version,
                config.index_version,
                release_id,
                config.fingerprint_sha256,
            ),
        )
        for row_id, record in enumerate(records, start=1):
            connection.execute(
                "INSERT INTO lexical_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    record.chunk_id,
                    record.case_id,
                    record.doc_id,
                    record.edition_year,
                    record.domain,
                    record.case_type,
                    record.access_level,
                    record.review_status,
                    1,
                    int(record.answer_eligible),
                    record.exact_tokens_json,
                ),
            )
            connection.execute(
                "INSERT INTO lexical_fts(rowid,title,question,law_names,exact_tokens,char_ngrams,body) VALUES(?,?,?,?,?,?,?)",
                (
                    row_id,
                    record.title,
                    record.question,
                    record.law_names,
                    record.exact_tokens_text,
                    record.char_ngrams_text,
                    record.body,
                ),
            )
        connection.execute("COMMIT")
        connection.execute("INSERT INTO lexical_fts(lexical_fts) VALUES('optimize')")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        connection = None
        os.link(temporary, target, follow_symlinks=False)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, sqlite3.Error):
        failed = True
    finally:
        if connection is not None:
            connection.close()
        for candidate in (
            temporary,
            Path(str(temporary) + "-wal"),
            Path(str(temporary) + "-shm"),
        ):
            try:
                candidate.unlink()
            except OSError:
                pass
    if failed:
        _raise("index_build_failed")
    return LexicalBuildResult(
        release_id=release_id,
        indexed_chunks=len(records),
        skipped_chunks=skipped,
        config_sha256=config.fingerprint_sha256,
    )


def _database_path(path: Path) -> Path:
    descriptor: int | None = None
    failed = False
    header = b""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 100
            or metadata.st_size > _MAX_INDEX_BYTES
        ):
            failed = True
        else:
            header = os.read(descriptor, 16)
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed or header != b"SQLite format 3\x00":
        _raise("index_invalid")
    return path.resolve(strict=True)


@contextmanager
def _connect_index(path: Path, config: RetrievalConfig) -> Iterator[sqlite3.Connection]:
    stable_path = _database_path(path)
    connection: sqlite3.Connection | None = None
    failed = False
    try:
        connection = sqlite3.connect(
            f"file:{stable_path.as_posix()}?mode=ro", uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        meta = connection.execute(
            "SELECT * FROM index_meta WHERE singleton=1"
        ).fetchall()
        if (
            len(meta) != 1
            or meta[0]["schema_version"] != config.schema_version
            or meta[0]["index_version"] != config.index_version
            or meta[0]["config_sha256"] != config.fingerprint_sha256
            or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        ):
            failed = True
    except (OSError, sqlite3.Error):
        failed = True
    if failed or connection is None:
        if connection is not None:
            connection.close()
        _raise("index_invalid")
    try:
        yield connection
    finally:
        connection.close()


class LexicalIndex:
    """Read-only lexical search over an independently built FTS5 database."""

    def __init__(self, path: Path, *, config_path: Path = _DEFAULT_CONFIG) -> None:
        self._path = path
        self._config = load_retrieval_config(config_path)
        with _connect_index(self._path, self._config):
            pass

    @property
    def path(self) -> Path:
        """Return the configured index path without exposing a mutable connection."""
        return self._path

    def _query_sql(self, filters: QueryFilters) -> tuple[str, tuple[object, ...]]:
        clauses = [
            "search_eligible=1",
            "review_status IN ('search_approved','approved')",
        ]
        parameters: list[object] = []
        if filters.access_level == "public":
            clauses.append("access_level='public'")
        else:
            clauses.append("access_level IN ('public','staff')")
        for column, values in (
            ("edition_year", filters.years),
            ("domain", filters.domains),
            ("case_type", filters.case_types),
        ):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                parameters.extend(values)
        sql = f"""
        WITH eligible AS MATERIALIZED (
          SELECT * FROM lexical_records WHERE {" AND ".join(clauses)}
        )
        SELECT e.chunk_id,e.case_id,e.doc_id,e.review_status,e.answer_eligible,
               e.exact_tokens_json,bm25(lexical_fts,?,?,?,?,?,?) AS rank
        FROM lexical_fts JOIN eligible AS e ON e.row_id=lexical_fts.rowid
        WHERE lexical_fts MATCH ?
        ORDER BY rank ASC,e.case_id ASC,e.chunk_id ASC
        LIMIT ?
        """
        return sql, tuple(parameters)

    def search(
        self,
        query: str,
        *,
        filters: QueryFilters | None = None,
        limit: int = 25,
    ) -> tuple[LexicalHit, ...]:
        if type(limit) is not int or limit < 1 or limit > self._config.max_results:
            _raise("search_invalid")
        normalized = normalize_query(query, filters=filters)
        sql, filter_parameters = self._query_sql(normalized.filters)
        parameters = (
            *filter_parameters,
            *self._config.weights,
            normalized.match_expression,
            limit,
        )
        failed = False
        rows: list[sqlite3.Row] = []
        try:
            with _connect_index(self._path, self._config) as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except (sqlite3.Error, TypeError, ValueError):
            failed = True
        if failed:
            _raise("search_failed")
        hits: list[LexicalHit] = []
        for row in rows:
            try:
                stored_exact = json.loads(row["exact_tokens_json"])
            except (json.JSONDecodeError, TypeError):
                _raise("index_invalid")
            if (
                type(stored_exact) is not list
                or any(type(token) is not str for token in stored_exact)
                or row["review_status"] not in _APPROVED_STATUSES
            ):
                _raise("index_invalid")
            matched = tuple(
                token for token in normalized.exact_tokens if token in stored_exact
            )
            hits.append(
                LexicalHit(
                    chunk_id=cast(str, row["chunk_id"]),
                    case_id=cast(str, row["case_id"]),
                    doc_id=cast(str, row["doc_id"]),
                    score=-float(row["rank"]),
                    matched_terms=matched,
                    review_status=cast(
                        Literal["search_approved", "approved"], row["review_status"]
                    ),
                    answer_eligible=bool(row["answer_eligible"]),
                )
            )
        return tuple(hits)

    def inspect_plan(
        self, query: str, *, filters: QueryFilters | None = None
    ) -> LexicalPlan:
        normalized = normalize_query(query, filters=filters)
        sql, filter_parameters = self._query_sql(normalized.filters)
        parameters = (
            *filter_parameters,
            *self._config.weights,
            normalized.match_expression,
            25,
        )
        failed = False
        details: tuple[str, ...] = ()
        restricted_candidates = 0
        try:
            with _connect_index(self._path, self._config) as connection:
                plan_rows = connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, parameters
                ).fetchall()
                details = tuple(str(row[3]) for row in plan_rows)
                restricted_candidates = int(
                    connection.execute(
                        "SELECT count(*) FROM lexical_records WHERE search_eligible<>1 OR review_status NOT IN ('search_approved','approved')"
                    ).fetchone()[0]
                )
        except (sqlite3.Error, TypeError, ValueError):
            failed = True
        if failed:
            _raise("search_failed")
        uses_fts = any("VIRTUAL TABLE" in detail.upper() for detail in details)
        return LexicalPlan(
            uses_fts=uses_fts,
            full_table_scan=not uses_fts,
            restricted_candidates=restricted_candidates,
            plan_steps=len(details),
        )
